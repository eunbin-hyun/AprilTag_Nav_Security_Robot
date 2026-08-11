"""로그인 기반 층 — 비밀번호 해시 · 세션 · 역할 판정 · 명부 쓰기 방어.

계약 정본은 `docs/로그인_설계초안_2026-08-01.md`(사용자 확정본)의 §1·§2·§4·§7이다.
여기 있는 건 전부 그 문서를 코드로 옮긴 것이고, 벗어나는 판단은 안 한다.

## ⭐ 제1 불변 — `AUTH_REQUIRE_LOGIN=false`면 아무것도 안 바뀐다

역할 게이트 셋(`require_agent`·`require_leader`·`require_admin`)은 플래그가 꺼져 있으면
**무조건 통과**한다. 세션 쿠키가 없으면 DB를 아예 안 건드리고 None을 돌려준다 —
마이그레이션 전 배포에서도 안 터진다. 쿠키가 있으면 누군지만 알려주고 판정은 안 한다.
그래서 켜기 전 구간에 기존 시험·기존 화면 거동이 글자 그대로 남는다(§6.2 1단계).

## 왜 JWT가 아니라 세션인가 (§1.1)

강등·해제가 **다음 요청부터** 먹혀야 한다. JWT는 만료 전까지 살아 있어서 관리자를
요원으로 내려도 그 사람이 쥔 토큰은 계속 관리자다. 무효화 목록을 붙이면 결국 서버
조회라 세션과 같아진다. 그래서 불투명 토큰 + DB 조회로 간다.

`resolve_session`이 매 요청 **계정 행까지 같이** 읽는 이유가 그것이다. 세션만 보고
말면 `is_active=false`로 해제한 사람이 세션 만료까지 계속 돌아다닌다.

## 새 의존성 0개 (§1.3·§2.6)

해시는 stdlib `hashlib.scrypt`, 토큰은 `secrets`, 저장은 이미 있는 Postgres다.
requirements.txt에 passlib·bcrypt·argon2가 하나도 없다(실측). argon2-cffi가 더 좋지만
C 확장이라 운영 이미지 빌드가 같이 움직이고, 계정 6개 규모엔 scrypt로 충분하다.

## 시각을 함수 하나로 모은 이유

만료·유휴·잠금이 전부 `_now()` 하나를 본다. 시험이 12시간을 실제로 기다릴 수 없어서
그 함수만 감으면 되게 뒀다(`app/credit/state_machine.py`의 `_wall_now`와 같은 수법).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import collections
import contextlib
import dataclasses
import datetime as dt
import hashlib
import hmac
import logging
import re
import secrets
from functools import lru_cache

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models import AppUser, AuthSession

logger = logging.getLogger("c207.auth")

# ── 역할 사다리 (§2.5·§7.3) ────────────────────────────────────────────────
# 전선에 싣는 값은 이 소문자 영문 셋뿐이다. 한글 표시는 화면 몫이고 숫자는 안 내보낸다 —
# 화면이 숫자를 비교하기 시작하면 판정이 두 자리로 갈라진다.
# ⭐ 교육생(2026-08-08 사용자 요청). 명부 출입자의 대부분이 이 급이다 — 계정으로 로그인해도
#   제일 낮은 문턱(agent) 아래라 관제 탭이 안 열리고, 자기 비밀번호 변경 같은 "본인" 창구만
#   쓸 수 있다.
ROLE_TRAINEE = "trainee"
ROLE_AGENT = "agent"
ROLE_LEADER = "leader"
ROLE_ADMIN = "admin"
# ⭐ 총괄 관리자(2026-08-05 사용자 확정 · 프론트 20차). `admin` 위에 한 급을 얹어
#   "자기보다 아랫급만 고친다"는 규칙 하나로 명부 권한을 정리한다 — 예전에는 관리자가
#   그 검사를 통째로 비켜가서 본인 항목 특례를 따로 둬야 했다.
ROLE_OWNER = "owner"
# ⚠ 제일 낮은 급이 0이 아니라 1인 이유 — `RANK.get(x, 0)` 기본값(모르는 역할)이 어느 실재
#   급과도 같아지면 안 된다. 0이 곧 교육생이면 역할 칸이 깨진 계정이 교육생 문턱을 지나간다.
#   화면(index.html)도 같은 이유로 1부터 세지만 **숫자는 서로 다르다** — 화면 ROLE_RANK는
#   trainee=1·agent=2…owner=5로 여기와 열쇠·차례가 같고 값도 지금은 겹치는데, 두 사다리는
#   각자 자기 표를 쓴다(전선에는 숫자가 아니라 낱말만 오간다). 비교는 낱말→숫자를 각 쪽이
#   자기 표로 하므로 값이 갈려도 판정은 안 깨진다.
RANK: dict[str, int] = {
    ROLE_TRAINEE: 1, ROLE_AGENT: 2, ROLE_LEADER: 3, ROLE_ADMIN: 4, ROLE_OWNER: 5,
}

# 세션 쿠키 이름. 설계가 값까지 못박은 계약이라 설정으로 안 뺀다(§1.2).
#
# ⭐ https 배포에서는 `__Host-` 접두를 붙인 쪽을 쓴다. 운영 환경에서는
# 형제 팀 호스트(i15a101.p.ssafy.io 등)가 `Domain=.p.ssafy.io` 쿠키를 구우면 우리 요청에도
# 같은 이름으로 실려 세션 고정을 걸 수 있다. `__Host-` 접두가 붙은 쿠키는 브라우저가
# **Secure + Path=/ + Domain 없음**일 때만 저장하고, 그래서 상위 도메인에서 절대 못 굽는다.
#
# ⚠ 그 셋이 필수라 http로 도는 로컬 개발에서는 아예 못 쓴다(브라우저가 저장 자체를 거부).
# 그래서 이름을 `SESSION_COOKIE_SECURE` 하나로 가른다 — 심을 때는 설정에 맞는 이름 하나,
# **읽을 때는 두 이름 다** 받는다. 롤아웃 중 이미 붙어 있던 옛 이름 쿠키가 그 자리에서
# 로그아웃되지 않게 하려는 것이다.
SESSION_COOKIE_NAME = "c207_session"
SESSION_COOKIE_NAME_SECURE = "__Host-c207_session"

# 아이디 규칙(§8 확정 A). 계정을 **만드는** 창구가 이걸로 막는다 — 로그인 입력은 안 막는다.
# 규칙 밖 아이디에 422를 주면 "그런 아이디는 없다"를 형식으로 알려 주는 셈이라, 로그인은
# 무엇이 들어와도 조회에 실패해 401로 떨어지는 쪽이 맞다(계정 열거 방지).
USERNAME_RE = re.compile(r"^[a-z0-9]{3,20}$")

# ── scrypt 파라미터 (§2.6) ────────────────────────────────────────────────
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16
# 128 * n * r = 16MiB가 필요하다. OpenSSL 기본 상한(32MiB)에 걸릴 여지를 없애려고 명시한다.
_SCRYPT_MAXMEM = 64 * 1024 * 1024
# 저장 형식. 파라미터를 같이 실어서 나중에 세기를 올려도 기존 행이 안 깨진다.
_HASH_SCHEME = "scrypt"


def _now() -> dt.datetime:
    """지금(UTC aware). 만료·유휴·잠금이 전부 이 한 자리를 본다."""
    return dt.datetime.now(dt.timezone.utc)


# ── 비밀번호 (§2.6) ────────────────────────────────────────────────────────

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _derive(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=dklen,
        maxmem=_SCRYPT_MAXMEM,
    )


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$dklen$salt$hash` 한 줄. salt는 매번 새로 뜬다."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = _derive(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN)
    return "$".join(
        [_HASH_SCHEME, str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P),
         str(_SCRYPT_DKLEN), _b64(salt), _b64(dk)]
    )


def verify_password(password: str, stored: str) -> bool:
    """맞나. 저장값이 깨졌거나 모르는 형식이면 예외 없이 False다.

    ⚠ 대조는 **바이트로 바꿔서** 한다. `hmac.compare_digest`는 str을 받으면 두 값이 다
    ASCII일 때만 돌고 아니면 TypeError를 던진다 — 이 저장소에 이미 그 함정 기록이 있다
    (`app/security.py` 머리). 여기선 base64 문자열이라 늘 ASCII지만, 파생 바이트끼리
    견주면 그 조건 자체를 안 만든다.
    """
    if not password or not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 7 or parts[0] != _HASH_SCHEME:
        logger.error("알 수 없는 비밀번호 해시 형식 — 검증을 거절한다")
        return False
    try:
        n, r, p, dklen = (int(v) for v in parts[1:5])
        salt = base64.b64decode(parts[5], validate=True)
        expected = base64.b64decode(parts[6], validate=True)
    except (ValueError, binascii.Error):
        logger.error("비밀번호 해시 파싱 실패 — 검증을 거절한다")
        return False
    try:
        actual = _derive(password, salt, n, r, p, dklen)
    except ValueError:
        # 저장된 파라미터가 이 런타임에서 못 돌 값일 때(예: 상한 밖 n).
        logger.error("저장된 scrypt 파라미터로 파생할 수 없다 — 검증을 거절한다")
        return False
    return hmac.compare_digest(actual, expected)


@lru_cache(maxsize=1)
def dummy_password_hash() -> str:
    """없는 아이디로 들어온 로그인이 한 번 검증하고 갈 더미 해시(계정 열거 방지).

    아이디가 없다고 곧장 401을 내면 **응답 시간**이 "그 아이디는 있다/없다"를 알려 준다.
    임포트 때가 아니라 첫 실패 때 만든다 — 이 해시 한 장에 16MiB scrypt 한 번이 들고,
    로그인을 한 번도 안 쓰는 프로세스(시험 대부분)가 그 값을 낼 이유가 없다.
    """
    return hash_password(secrets.token_urlsafe(32))


# ── 이벤트 루프를 안 막는 판 (2026-08-02 백지 검토 F4) ─────────────────────
#
# 위 셋은 16MiB scrypt를 **동기로** 돌린다. 한 번에 수백 밀리초가 들고 그동안 GIL을 쥔
# 채 이벤트 루프가 통째로 선다. 이 앱은 uvicorn 워커가 1개라(app/main.py 머리) 그 시간이
# 곧 서버 전체의 정지다 — 로봇 WS 하트비트도, 출동 대기 창 타이머(5초)도 같이 멈춘다.
# 로그인 몇 번이면 로봇 커넥션이 스테일로 끊긴다.
#
# 그래서 **async 자리에서는 아래 셋만 쓴다.** hashlib.scrypt는 C 구현이라 파생 동안 GIL을
# 놓는다 — 스레드로 넘기면 루프가 그대로 돈다.
#
# ⚠ 동기 진입점(위 셋)을 남겨 두는 이유는 부르는 자리가 async가 아닌 데가 있어서다
# (`tools/seed_users.py`, 시험의 계정 심기). 시그니처를 안 바꾼다.

async def hash_password_async(password: str) -> str:
    """`hash_password`를 스레드에서. async 창구는 이쪽을 쓴다."""
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, stored: str) -> bool:
    """`verify_password`를 스레드에서. async 창구는 이쪽을 쓴다."""
    return await asyncio.to_thread(verify_password, password, stored)


async def dummy_password_hash_async() -> str:
    """`dummy_password_hash`를 스레드에서. 첫 호출의 scrypt 한 번도 루프 밖으로 나간다.

    lru_cache라 값은 프로세스에 한 장뿐이다. 두 요청이 동시에 처음 부르면 파생이 두 번
    돌 수 있는데, 나온 값 중 하나가 캐시에 남고 둘 다 쓸 수 있는 더미라 문제가 없다.
    """
    return await asyncio.to_thread(dummy_password_hash)


# ── 로그인 실패 잠금 (§8 결정 3 — 계정당 5회·60초) ─────────────────────────
# 인메모리다. 이 앱은 uvicorn 워커 1개 전제라(app/main.py 머리) 프로세스 하나에 다 모인다.
# DB에 두면 실패마다 쓰기가 생기는데, 잠금은 재기동 뒤에 풀려도 되는 값이라 그 값을
# 안 낸다. ⚠ 워커를 늘리는 날 이 상태도 같이 옮겨야 한다.


# ⭐ 열쇠를 **공격자가 정한다**(로그인 본문의 아이디 문자열). 무제한 dict면 없는 아이디로
# 실패를 뿌리는 것만으로 서버 메모리가 계속 자란다 — 인증도 필요 없는 창구다. 그래서 두
# 겹으로 막는다(2026-08-02 백지 검토 F6).
#   ① TTL — 마지막 실패로부터 이만큼 지났고 지금 잠겨 있지도 않은 항목은 버린다.
#      값이 잠금 시간(60초)보다 훨씬 길어야 한다. 세던 수를 너무 일찍 버리면 천천히 두드리는
#      공격이 영원히 5회에 안 닿아 잠금이 뜻을 잃는다.
#   ② 상한 — 항목 수가 이 값을 넘으면 오래 안 건드린 것부터 버린다.
#      ⚠ 상한에 닿았다고 **새 실패를 안 세면 그게 곧 잠금 무력화다**(쓰레기 아이디로 표를
#      채워 놓고 진짜 아이디를 두드리면 된다). 그래서 자리는 늘 비워서 새 항목을 넣는다.
#      버릴 후보는 **잠기지 않은 항목을 먼저** 고른다 — 살아 있는 잠금을 밀어내는 게 공격
#      목표라, 오래됨만 보고 버리면 표를 채워 남의 잠금을 푸는 길이 열린다.
_FAIL_ENTRY_TTL_SEC = 3600.0
_FAIL_ENTRY_MAX = 4096


@dataclasses.dataclass
class _FailState:
    count: int = 0
    locked_until: dt.datetime | None = None
    # 마지막으로 이 항목을 건드린 시각. TTL 청소가 이 값을 본다.
    touched_at: dt.datetime | None = None

    def is_locked(self, now: dt.datetime) -> bool:
        return self.locked_until is not None and self.locked_until > now

    def is_stale(self, now: dt.datetime) -> bool:
        """지금 잠겨 있지도 않고 마지막 실패도 오래됐나. 그러면 버려도 된다."""
        if self.is_locked(now):
            return False
        if self.touched_at is None:
            return True
        return (now - self.touched_at).total_seconds() >= _FAIL_ENTRY_TTL_SEC


# 삽입·갱신 순서를 들고 있어야 "오래된 것부터" 버릴 수 있어서 OrderedDict다.
_login_failures: collections.OrderedDict[str, _FailState] = collections.OrderedDict()


def _prune_login_failures(now: dt.datetime) -> None:
    """TTL 지난 항목을 걷고, 그래도 상한이면 오래된 것부터 버려 자리를 하나 비운다.

    항목 수가 상한(4096)이라 훑는 비용이 무시할 수준이고, 부르는 자리도 로그인 실패
    하나뿐이라(그 앞에 scrypt 한 번이 있다) 매번 훑어도 된다.
    """
    for key in [k for k, state in _login_failures.items() if state.is_stale(now)]:
        del _login_failures[key]

    while len(_login_failures) >= _FAIL_ENTRY_MAX:
        # 잠기지 않은 항목 중 가장 오래된 것부터. 없으면(전부 잠김) 가장 오래된 잠금을 버린다.
        victim = next(
            (k for k, state in _login_failures.items() if not state.is_locked(now)),
            next(iter(_login_failures)),
        )
        del _login_failures[victim]
        logger.warning("로그인 실패 표가 상한(%d)에 닿아 오래된 항목을 버린다", _FAIL_ENTRY_MAX)


def normalize_username(username: str) -> str:
    """아이디를 대조용 한 모양으로. 규칙이 소문자+숫자라(§8 확정 A) 대소문자를 접는다.

    잠금 열쇠도 이 값이다 — 안 접으면 `Admin`·`ADMIN`으로 갈아 가며 잠금을 피해 간다.
    """
    return (username or "").strip().lower()


def login_lock_remaining(username: str) -> float:
    """이 아이디가 잠겨 있으면 남은 초, 아니면 0. 지난 잠금은 여기서 걷힌다."""
    state = _login_failures.get(normalize_username(username))
    if state is None or state.locked_until is None:
        return 0.0
    remaining = (state.locked_until - _now()).total_seconds()
    if remaining <= 0:
        state.locked_until = None
        return 0.0
    return remaining


def record_login_failure(username: str) -> None:
    """실패 한 번. 상한에 닿으면 잠그고 세던 수는 0으로 되돌린다.

    수를 0으로 되돌리는 이유 — 잠금이 풀린 뒤 실패 한 번에 바로 다시 잠기면 오타 한 번이
    사실상 영구 잠금이 된다. 시연 중에 그게 제일 나쁘다.
    """
    settings = get_settings()
    now = _now()
    key = normalize_username(username)
    _prune_login_failures(now)
    state = _login_failures.get(key)
    if state is None:
        state = _FailState()
        _login_failures[key] = state
    else:
        # 가장 최근에 건드린 항목이 뒤로 간다 — "오래된 것부터 버린다"가 성립하는 근거다.
        _login_failures.move_to_end(key)
    state.count += 1
    state.touched_at = now
    if state.count >= settings.login_fail_max_attempts:
        state.count = 0
        state.locked_until = _now() + dt.timedelta(seconds=settings.login_fail_lock_sec)
        logger.warning("로그인 실패 상한 도달 — 계정 %r을 %.0f초 잠근다", key,
                       settings.login_fail_lock_sec)


def clear_login_failures(username: str) -> None:
    """로그인이 성공했으니 이 아이디의 실패 기록을 버린다."""
    _login_failures.pop(normalize_username(username), None)


# ── 진행 중 시도 (2026-08-02 백지 검토 3차 X77) ────────────────────────────
#
# 잠금 검사와 `record_login_failure` 사이에는 scrypt 한 번(수백 밀리초)이 놓인다. 그 창에
# 병렬로 밀어 넣은 요청 N개가 **전부 첫 검사를 통과**해서, 계약상 5회 잠금이 한 번에 N회
# 추측을 허용한다. 그래서 "이미 센 실패"만 보지 말고 **아직 결과가 안 난 시도**를 같이 센다.
#
# ⚠ 워커 1개·이벤트 루프 하나 전제다(위 잠금 설명과 같은 자리). 검사와 자리 잡기 사이에
# await이 없어야 원자성이 성립한다 — `login_attempt`를 검사 **직후** 여는 게 그 계약이다.
# 열쇠는 실패 장부와 같은 정규화 아이디고, 항목은 0이 되는 순간 지워서 표가 안 자란다.
_login_inflight: dict[str, int] = {}


@contextlib.contextmanager
def login_attempt(username: str):
    """이 블록이 도는 동안 그 아이디의 '진행 중 시도'로 하나 센다.

    실패로 끝나든 성공하든 예외로 빠지든 finally에서 되돌려 놓는다 — 안 되돌리면 그 아이디가
    영영 시도 한 자리를 잃고, 그게 쌓이면 정상 로그인이 429로 막힌다.
    """
    key = normalize_username(username)
    _login_inflight[key] = _login_inflight.get(key, 0) + 1
    try:
        yield
    finally:
        left = _login_inflight.get(key, 0) - 1
        if left > 0:
            _login_inflight[key] = left
        else:
            _login_inflight.pop(key, None)


def login_attempt_blocked(username: str) -> float:
    """지금 이 아이디로 시도를 더 받아도 되나. 안 되면 남은 초(>0), 되면 0.

    두 가지를 본다.
      ① 이미 잠겼나 — `login_lock_remaining` 그대로다.
      ② 이미 센 실패에 **진행 중 시도**를 더하면 상한을 넘나 — 넘으면 아직 잠긴 게 아니라
         "지금 도는 시도가 자리를 다 채웠다"라서, 곧 결과가 나오므로 1초만 물린다.
    ②가 없으면 병렬 요청이 잠금을 통째로 우회한다(X77).
    """
    remaining = login_lock_remaining(username)
    if remaining > 0:
        return remaining
    key = normalize_username(username)
    inflight = _login_inflight.get(key, 0)
    if inflight <= 0:
        return 0.0
    state = _login_failures.get(key)
    counted = state.count if state is not None else 0
    if counted + inflight < get_settings().login_fail_max_attempts:
        return 0.0
    logger.warning("계정 %r의 동시 로그인 시도가 상한을 채웠다 — 잠시 물린다", key)
    return 1.0


def reset_auth_state() -> None:
    """인메모리 잠금 상태를 비운다. 시험 위생용(DB 밖 상태라 TRUNCATE로 안 지워진다)."""
    _login_failures.clear()
    _login_inflight.clear()


# ── 세션 (§1.2~§1.4) ───────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class AuthActor:
    """지금 요청을 보낸 사람. 판정에 쓰는 급은 `role` 하나뿐이다(§2.5)."""

    id: int
    username: str
    display_name: str
    role: str


def token_digest(token: str) -> str:
    """DB에 저장하고 조회에 쓰는 값. **원문이 아니라 sha256**이다(§1.3).

    덤프 한 번에 살아 있는 세션이 통째로 털리는 걸 막는다. 유니크 인덱스라 비용은 같다.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_session(
    db: AsyncSession, user: AppUser, user_agent: str | None = None
) -> str:
    """세션 한 장을 발급하고 **원문 토큰**을 돌려준다. DB엔 해시만 남는다.

    원문은 이 반환값 말고는 어디에도 없다 — 쿠키에 실어 보내고 나면 서버는 못 되살린다.
    """
    settings = get_settings()
    token = secrets.token_urlsafe(32)
    now = _now()
    db.add(
        AuthSession(
            token_hash=token_digest(token),
            user_id=user.id,
            created_at=now,
            expires_at=now + dt.timedelta(seconds=settings.session_absolute_ttl_sec),
            last_seen_at=now,
            # DB 칸이 200자다. 브라우저 UA는 그보다 길 수 있어 자른다(값이 길다고 INSERT가
            # 통째로 터지면 로그인이 막힌다 — 기록 한 줄 때문에 잃을 게 아니다).
            user_agent=user_agent[:200] if user_agent else None,
        )
    )
    await db.flush()
    await _enforce_session_limit(db, user.id, settings.session_max_per_user, now)
    await db.commit()
    return token


async def _enforce_session_limit(
    db: AsyncSession, user_id: int, limit: int, now: dt.datetime
) -> None:
    """한 계정의 살아 있는 세션을 `limit`장으로 자른다. 넘치면 **오래된 것부터** 무효화한다.

    왜 필요한가 — 로그인은 세션을 쌓기만 하고 끊는 자리가 로그아웃 하나다. 기기를 옮겨
    가며 로그인하거나 쿠키가 지워진 채로 다시 들어오면 옛 토큰이 12시간 내내 살아 있는데,
    그걸 낱장으로 끊을 창구가 없다(2026-08-02 백지 검토 F21).

    ⚠ 커밋은 **안 한다.** 부르는 쪽(`create_session`)의 INSERT와 같은 커밋에 실려야
    "새 걸 넣었는데 옛 건 안 끊긴" 중간 상태가 안 생긴다.

    정렬에 `id`를 같이 넣는 이유는 시계를 감는 시험 때문이다 — 같은 눈금에 여러 장을
    만들면 `created_at`이 전부 같아서 무엇이 오래된 것인지가 안 정해진다.
    """
    if limit <= 0:
        return
    stale_ids = (
        await db.execute(
            select(AuthSession.id)
            .where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
            .order_by(AuthSession.created_at.desc(), AuthSession.id.desc())
            .offset(limit)
        )
    ).scalars().all()
    if not stale_ids:
        return
    await db.execute(
        update(AuthSession)
        .where(AuthSession.id.in_(stale_ids))
        .values(revoked_at=now)
    )
    logger.info("계정 %s의 세션 상한(%d) 초과 — 오래된 %d장을 끊는다",
                user_id, limit, len(stale_ids))


async def resolve_session(
    db: AsyncSession, token: str, *, touch: bool = True
) -> AuthActor | None:
    """토큰 한 장 → 사람. 살아 있지 않으면 None이다(왜 죽었는지는 안 알려준다).

    죽는 갈래 다섯을 여기 한 자리에 모았다.
      ① 그런 세션이 없다 ②무효화됐다(`revoked_at`) ③절대 만료 ④유휴 만료
      ⑤계정이 해제됐다(`is_active=false`) — 강등·해제가 다음 요청부터 먹히는 근거다.

    ⚠ `last_seen_at` 갱신은 `SESSION_TOUCH_MIN_INTERVAL_SEC`가 지났을 때만 쓴다. 화면이
    주기로 조회를 때리는데 매 요청 UPDATE를 하면 세션 테이블이 쓰기 폭주를 맞는다(§1.4).

    ⭐ **`touch=False`는 "사람이 안 보낸 조회"다.** 유휴 만료(§1.4)는 `last_seen_at + 2시간`
    이고 **활동하면** 밀리는 계약인데, 서버가 스스로 도는 배경 조회까지 밀면 사람이 아무것도
    안 해도 만료가 영영 안 온다. 대시보드 WS 세션 재검증(`security._ws_dashboard_recheck_loop`)
    이 그 자리라 거기만 `touch=False`로 부른다. 사람이 실제로 요청을 보내는 갈래(HTTP 창구·
    WS 핸드셰이크)는 기본값 그대로 민다.

    ⚠ 새 배경 조회를 만들면 `touch=False`를 같이 챙겨야 한다. 안 챙기면 그 주기가 곧
    유휴 만료를 끄는 스위치가 된다.

    ⚠ 엔티티가 아니라 **필요한 칸만** 읽는 이유 둘.
      ① `password_hash`를 매 요청 메모리에 안 올린다. 판정에 안 쓰는 값이라 안 읽는 게 맞다.
      ② ORM 엔티티로 읽으면 아래 UPDATE 뒤에도 세션 안 사본이 옛 `last_seen_at`을 들고 있어,
         같은 요청에서 한 번 더 조회할 때 같은 값을 다시 쓰는 군더더기 UPDATE가 붙는다.
    """
    if not token:
        return None
    settings = get_settings()
    row = (
        await db.execute(
            select(
                AuthSession.id,
                AuthSession.revoked_at,
                AuthSession.expires_at,
                AuthSession.last_seen_at,
                AppUser.id,
                AppUser.username,
                AppUser.display_name,
                AppUser.role,
                AppUser.is_active,
            )
            .join(AppUser, AuthSession.user_id == AppUser.id)
            .where(AuthSession.token_hash == token_digest(token))
        )
    ).first()
    if row is None:
        return None
    (
        session_id, revoked_at, expires_at, last_seen_at,
        user_id, username, display_name, role, is_active,
    ) = row

    now = _now()
    if revoked_at is not None:
        return None
    if now >= expires_at:
        return None
    if now - last_seen_at >= dt.timedelta(seconds=settings.session_idle_ttl_sec):
        return None
    if not is_active:
        return None

    if touch and now - last_seen_at >= dt.timedelta(
        seconds=settings.session_touch_min_interval_sec
    ):
        await db.execute(
            update(AuthSession)
            .where(AuthSession.id == session_id)
            .values(last_seen_at=now)
        )
        await db.commit()

    return AuthActor(
        id=user_id, username=username, display_name=display_name, role=role
    )


async def revoke_session(db: AsyncSession, token: str) -> None:
    """무효화. 행을 **안 지운다** — "언제 왜 끊겼나"가 감사 자료다(§1.4)."""
    if not token:
        return
    await db.execute(
        update(AuthSession)
        .where(
            AuthSession.token_hash == token_digest(token),
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=_now())
    )
    await db.commit()


async def revoke_user_sessions(
    db: AsyncSession,
    user_id: int,
    *,
    commit: bool = True,
    except_token_hash: str | None = None,
) -> None:
    """그 사람의 살아 있는 세션을 전부 끊는다. 계정 해제·비밀번호 변경이 부르는 자리다.

    `commit=False`면 UPDATE만 내고 커밋은 **부르는 쪽 트랜잭션에 맡긴다.**

    왜 그 판이 필요한가 — 비밀번호 재설정은 "해시 교체 커밋"과 "세션 끊기"가 지금 서로 다른
    트랜잭션이라, 그 사이에서 프로세스가 죽으면 옛 비밀번호로 연 세션이 절대 만료(12시간)까지
    산다. 비밀번호를 바꾼 뜻이 통째로 사라지는 자리다. 두 쓰기를 한 커밋에 묶으면 그 틈이
    없어진다(2026-08-02 백지 검토 F19).

    ⚠ 기본값은 지금 거동 그대로(자기 커밋)다. 계정 해제 갈래는 `assert_not_last_admin`의
    행 잠금 때문에 **커밋 뒤에** 부르는 게 계약이라(routers/account.py 주석), 거기까지 같이
    바꾸면 그 잠금이 풀린다. 그래서 이 함수는 판만 열어 두고 어느 갈래를 묶을지는 부르는
    쪽이 정한다.
    """
    stmt = (
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    # 본인 비밀번호 변경이 쓰는 판 — 지금 이 요청을 실어 온 세션만 남기고 끊는다.
    # 안 남기면 바꾼 사람이 그 자리에서 로그아웃돼 "바꿨는데 쫓겨났다"가 된다. 훔친 세션을
    # 걱정하는 자리가 아니다 — 그 창구는 지금 비밀번호를 먼저 확인한다(routers/auth.py).
    if except_token_hash is not None:
        stmt = stmt.where(AuthSession.token_hash != except_token_hash)
    await db.execute(stmt)
    if commit:
        await db.commit()


async def purge_expired_sessions(db: AsyncSession) -> int:
    """만료 뒤 보관 기간이 지난 행만 지우고 지운 수를 돌려준다.

    간소화 — 별도 크론 없이 로그인 창구가 들어올 때 한 번 훑는다. 행이 6명치라 부하가
    없어서 그렇게 뒀다. 계정이 늘어 이게 무거워지면 주기 작업으로 옮긴다.
    """
    settings = get_settings()
    cutoff = _now() - dt.timedelta(days=settings.session_purge_after_days)
    result = await db.execute(delete(AuthSession).where(AuthSession.expires_at < cutoff))
    await db.commit()
    return result.rowcount or 0


# ── 역할 게이트 (§3 매트릭스가 부르는 자리) ────────────────────────────────

def session_cookie_name() -> str:
    """지금 설정에서 **심을** 쿠키 이름. https면 `__Host-` 접두가 붙는다(§1.2 보강).

    `__Host-`는 Secure + Path=/ + Domain 없음이 필수라, http로 도는 로컬 개발에서는
    브라우저가 저장을 거부한다. 그래서 `SESSION_COOKIE_SECURE` 하나로 이름을 가른다.
    """
    return (
        SESSION_COOKIE_NAME_SECURE
        if get_settings().session_cookie_secure
        else SESSION_COOKIE_NAME
    )


def read_session_token(cookies) -> str | None:
    """요청에서 세션 토큰을 꺼낸다. **두 이름을 다 받는다.**

    지금 설정이 심는 이름을 먼저 보고, 없으면 다른 이름을 본다. 롤아웃 중에 이미 브라우저에
    들어 있던 옛 이름 쿠키가 그 자리에서 무시되면 붙어 있던 사람이 전부 로그아웃된다.

    ⛔ **예전 주석이 틀렸다**(2026-08-09 백지검토 정정). "공격자가 심은 값이 유효한 세션이어야
    하는데 그건 못 만든다"고 단정해 뒀었는데, **우리 서버에 계정이 하나라도 있는 쪽(교육생
    포함)은 자기 세션 토큰을 그대로 심으면 된다.** 그러니 상위 도메인(`Domain=.p.ssafy.io`)에
    옛 이름 쿠키를 구울 수 있는 형제 팀 호스트가 있으면 세션 고정(도네이션)이 성립한다 —
    로그아웃 상태의 관제 브라우저가 **그 사람 계정으로** 로그인된 것처럼 돈다. 남의 계정을
    빼앗는 갈래는 아니지만, `__Host-` 접두를 붙인 목적이 이 폴백 한 줄로 그 창에서 무효가 된다.
    `routers/auth.py`의 로그아웃도 Domain 없는 쿠키만 지워서 상위 도메인 쿠키는 안 걷힌다.

    ⭐ **그런데도 지금은 안 걷는다** — 걷으면 `backend/tests`가 통째로 무너진다. 시험 헬퍼
    열댓 개가 `SESSION_COOKIE_NAME`(옛 이름)으로 쿠키 헤더를 만들고 conftest는
    `SESSION_COOKIE_SECURE=true`라, 폴백이 없으면 전부 401이다.
    `test_auth_endpoints.test_legacy_cookie_name_is_still_accepted`는 아예 이 폴백을 계약으로
    못박아 뒀다. 발표(2026-08-10)를 하루 앞두고 시험을 같이 갈아엎는 값어치가 없다고 봤다.

    ✅ **걷을 때가 되면 실사용자는 안 튕긴다.** `__Host-` 이름은 `3f38946`(2026-08-02)에
    들어왔고 세션 절대 만료가 12시간(`session_absolute_ttl_sec=43200`)이라, 옛 이름으로 살아
    있는 세션은 이미 하나도 없다. 폴백은 순수하게 롤아웃용 임시 장치이고 **시험 헬퍼를 새
    이름으로 옮기는 것과 한 묶음으로** 지울 자리다.
    """
    primary = session_cookie_name()
    token = cookies.get(primary)
    if token:
        return token
    other = (
        SESSION_COOKIE_NAME
        if primary == SESSION_COOKIE_NAME_SECURE
        else SESSION_COOKIE_NAME_SECURE
    )
    return cookies.get(other)


async def resolve_request_actor(request: Request, db: AsyncSession) -> AuthActor | None:
    """요청의 쿠키에서 사람을 찾는다. 쿠키가 없으면 **DB를 안 건드린다.**

    그 짧은 길이 제1 불변을 받친다 — 플래그가 꺼진 배포에서 마이그레이션이 아직 안 돌아도
    `auth_session` 조회가 안 나가서 기존 창구가 안 터진다.
    """
    token = read_session_token(request.cookies)
    if not token:
        return None
    return await resolve_session(db, token)


def _role_dependency(min_role: str, *, always: bool = False):
    """`min_role` 이상만 통과하는 FastAPI 의존성을 만든다.

    플래그가 꺼져 있으면 **판정을 아예 안 한다** — 세션이 있으면 누군지 알려주고, 없으면
    None으로 통과시킨다. 창구 코드는 두 경우를 똑같이 다루면 된다(값이 None일 수 있다).

    ⚠ `always=True`는 그 예외다. **로그인과 함께 새로 생긴 창구**(명부·계정)는 플래그와
    무관하게 늘 판정한다. 제1 불변은 "지금 있는 창구의 거동을 안 바꾼다"는 약속이라 없던
    창구에는 걸리지 않는데, 그대로 두면 롤아웃 1~4단계 내내 익명이 관리자 계정을 만들 수
    있다(검증 F1 — 만든 계정으로 플래그를 켠 뒤에도 들어가지는 뒷문까지 실측됐다).
    계정 심기는 창구가 아니라 `tools/seed_users.py`가 하는 자리다(설계 §6.2 2단계).
    """

    async def dependency(
        request: Request, db: AsyncSession = Depends(get_db)
    ) -> AuthActor | None:
        actor = await resolve_request_actor(request, db)
        if not always and not get_settings().auth_require_login:
            return actor
        if actor is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="로그인이 필요합니다.",
            )
        if RANK.get(actor.role, 0) < RANK[min_role]:
            # 403이다. 401로 주면 요원이 관리자 창구를 한 번 건드릴 때마다 로그아웃된다(§7.2).
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="권한이 없습니다."
            )
        return actor

    return dependency


require_agent = _role_dependency(ROLE_AGENT)
require_leader = _role_dependency(ROLE_LEADER)
require_admin = _role_dependency(ROLE_ADMIN)

# 로그인과 함께 새로 생긴 창구(명부·계정) 전용. 플래그와 무관하게 늘 판정한다 — 위 always
# 주석 참고. 기존 창구에는 절대 쓰지 마라(그 순간 제1 불변이 깨진다).
require_agent_always = _role_dependency(ROLE_AGENT, always=True)
require_leader_always = _role_dependency(ROLE_LEADER, always=True)
require_admin_always = _role_dependency(ROLE_ADMIN, always=True)
# 사다리 맨 아래라 사실상 "로그인만 돼 있으면 누구나"다. 본인 창구(자기 비밀번호 변경)처럼
# 급이 아니라 신원만 필요한 자리에 쓴다 — 교육생 계정도 자기 것은 자기가 바꾼다.
require_trainee_always = _role_dependency(ROLE_TRAINEE, always=True)


# ── 명부 쓰기 방어 (§4 — 함수 하나로 모은다) ───────────────────────────────

def assert_can_write(
    actor: AuthActor | None,
    target_rank: str | None,
    target_row: object | None = None,
    *,
    state_change: bool = False,
) -> None:
    """명부 행 하나를 쓸 수 있나. 못 쓰면 403을 던진다.

    등록·수정·해제 **셋이 같은 이 함수를 부른다.** 창구마다 `if role ==`을 흩으면 창구가
    늘 때 한쪽만 고쳐진다(§4). 화면의 버튼 비활성은 안내용이고 최종 판정은 여기 하나다.

    인자 둘의 뜻이 다르다.
      - `target_rank` — **결과로 놓일 급.** 등록이면 새로 지정한 직급이고(그래서 이 한
        인자가 "등록 직급 상한"까지 같이 막는다), 수정이면 대상 행의 지금 급이다. 직급을
        바꾸는 수정은 **옛 급과 새 급 둘 다** 한 번씩 넣어 부른다 — 아래 급 사람을 위로
        올리는 길과 위 급 사람을 건드리는 길이 다른 갈래라 한 번만 보면 하나가 샌다.
      - `target_row` — 대상 명부 행. `app_user_id`만 본다(확정 D). 이름 문자열 비교는
        안 쓴다 — 동명이인 한 명이면 남의 행이 잠긴다.

    갈래 순서가 계약이다.
      ① `actor`가 None = 플래그 off 구간이라 통과(제1 불변).
      ② **총괄(`owner`)은 전부 통과.** 자기 계정을 **정지·삭제**하는 것만 막는다.
      ③ 본인 항목 차단. 자기 카드를 자기가 지우는 잠금 사고를 막는다.
      ④ 아랫급만. 동급·윗급은 막힌다.

    ## ⭐ 총괄 등급이 생기면서 특례가 사라졌다 (2026-08-05 사용자 확정 · 프론트 20차)

    | 누가 | 무엇을 고칠 수 있나 |
    | --- | --- |
    | `owner` | 전부 (본인 행 포함. 단 자기 계정 정지·삭제는 막힘) |
    | `admin` | `leader`·`agent`만 — **다른 관리자도, 자기 자신도 못 고침** |
    | `leader` | `agent`만 |

    ⛔ **예전에는 관리자가 ④를 통째로 비켜갔다.** 그래서 "admin 급 행을 아무도 못 고치는
    영구 잠금"을 피하려고 본인 항목 특례를 따로 둬야 했고, 그 특례가 A안·B안으로 갈렸다.
    **위에 한 급을 얹으니 그 갈래가 통째로 없어졌다** — 총괄이 그 자리를 맡는다.

    ⚠ **③을 사다리에 맡기지 않고 남긴 까닭** — 명부 `rank`와 계정 `role`이 **어긋날 수
    있다.** 팀장 계정이 요원 급 명부 행에 링크돼 있으면 사다리로는 통과라 자기 행을
    고치게 된다. 지금 명부는 계정 급 그대로 심겨 있지만, 그 전제가 깨지는 날 조용히 열린다.

    ⚠ **총괄이 자기 계정을 정지·삭제하는 것만 막는다.** 그러면 총괄이 사라지고 되살릴
    사람도 같이 없어진다. 고치기(이름·학번·태그)는 열어 둔다.

    모르는 직급(None·사전 밖 값)은 **닫는 쪽**으로 간다. 명부에 급이 안 적힌 행을 아무나
    건드릴 수 있으면 상한이 뜻을 잃는다.
    """
    if actor is None:
        return

    self_row = target_row is not None and (
        getattr(target_row, "app_user_id", None) == actor.id
    )

    # ② 총괄은 전부. 자기 계정을 내리는 것만 막는다.
    if actor.role == ROLE_OWNER:
        if self_row and state_change:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "총괄 관리자 계정은 스스로 정지하거나 지울 수 없습니다. "
                    "총괄을 먼저 넘겨 주세요."
                ),
            )
        return

    # ③ 본인 항목 차단(위 docstring — 사다리에 안 맡기는 까닭이 적혀 있다).
    if self_row:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="본인 항목은 직접 바꿀 수 없습니다. 관리자에게 요청해 주세요.",
        )

    target_level = RANK.get(target_rank or "")
    if target_level is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="직급을 알 수 없는 항목이라 처리할 수 없습니다. 관리자에게 요청해 주세요.",
        )
    if RANK.get(actor.role, 0) <= target_level:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="자기보다 아랫급 항목만 처리할 수 있습니다.",
        )


def assert_can_write_account(
    actor: AuthActor | None,
    target_role: str | None = None,
    *,
    new_role: str | None = None,
    target_user_id: int | None = None,
    deactivate: bool = False,
) -> None:
    """계정 하나를 만들거나 고칠 수 있나. 못 하면 403을 던진다.

    계정 창구 **셋(생성 · 역할/활성 수정 · 비밀번호)이 같은 이 함수를 부른다** — 명부 쪽
    `assert_can_write`와 같은 원칙이다(창구마다 `if role ==`을 흩으면 한쪽만 고쳐진다, §4).

    ⛔ **없던 자리다(2026-08-08 백지검토).** 계정 창구 셋의 게이트가 `require_admin_always`
    하나뿐이라 관리자가 `PATCH /api/users/{자기 id}`로 자기를 `owner`로 올리고, 총괄
    비밀번호를 갈아 끼울 수 있었다. 명부는 `assert_can_write` 여덟 자리로 같은 일을 막는데
    계정 쪽만 안 따라간 자리다.

    인자 둘의 뜻은 명부 판과 같은 갈래인데, **`new_role`은 키워드로만 받는다** — 명부 판
    `assert_can_write(actor, target_rank)`의 둘째 위치 인자는 "결과로 놓일 급"이라 뜻이
    반대다. 위치로 부르면 새 급이 지금 급 자리에 들어가 본인 갈래에서 안 읽히고, 이 함수가
    막는 자기 승격이 조용히 다시 열린다(2026-08-08 교차 검증).
      - `target_role` — 대상 계정의 **지금** 역할. 수정·비밀번호가 넘긴다(생성은 대상이 없다).
      - `new_role` — **결과로 놓일** 역할. 생성과 역할 수정이 넘긴다. 지금 급과 새 급을
        **둘 다** 봐야 한다 — 아랫급을 위로 올리는 길과 윗급을 건드리는 길이 다른 갈래라
        한쪽만 보면 하나가 샌다(자기 승격이 정확히 "지금 급은 안 보고 새 급만 본" 구멍이다).
      - `deactivate` — 이번 조작이 활성 해제인가. 총괄 본인 보호(②)만 본다.

    갈래 순서가 계약이다.
      ① `actor` None = 판정 없이 통과. 계정 창구는 `require_admin_always`라 실제로는 안
         오는 값인데, 명부 판과 갈래 모양을 같게 둔다 — 두 함수를 나란히 읽는 자리가 많다.
      ② **총괄(`owner`)은 전부 통과 — 자기 계정을 강등·해제하는 것만 막는다.** admin 급
         계정을 아무도 못 고치는 영구 잠금을 막는 자리가 총괄인데, 총괄이 스스로 내려가면
         그 자리가 비고 admin은 owner 행을 못 만져(④) 되살릴 사람이 없다. 이번 사다리
         도입으로 "admin끼리 서로 구제"하던 길까지 닫혀서, 이 못이 없으면 잠금이 새로 열린다
         (2026-08-08 교차 검증). ⚠ 명부 판(`assert_can_write`)은 owner 본인의 정지·삭제만
         막고 강등은 열어 두는데, **여기는 강등까지 막아 한 겹 더 조인다** — 계정 role은
         곧 로그인 권한 자체라 명부 rank보다 무겁다. 두 자리를 "같은 못"으로 보고 한쪽만
         고치지 마라.
      ③ **본인 계정은 명부 판과 다르다** — 통째로 막지 않는다. 역할은 **내려가기(강등)·
         유지만** 통과다 — "관리자가 자기를 강등하는 건 막지 않는다, 정본은 마지막 관리자만
         막는다"가 §8 결정 4고 `test_second_to_last_admin_can_step_down`이 그걸 못박았다.
         **올라가기만 여기서 403이다** — 자기 승격이 이 함수가 생긴 이유다. 활성 해제도
         통과다(내려가는 방향이고, 마지막 관리자 보호가 그 아래에서 따로 돈다).
         ⚠ 본인 비밀번호는 이 함수 몫이 아니다 — 전용 창구(`POST /api/auth/password`)가
         지금 비밀번호를 확인하고 바꾼다. 관리자 재설정 창구는 본인을 아예 안 받는다
         (라우터에서 막는다) — 훔친 세션이 지금 비밀번호 없이 계정을 가져가는 길이라서다.
      ④ 남의 계정은 지금 급도 새 급도 **자기보다 아랫급**이어야 한다. 동급·윗급은 막힌다 —
         관리자는 다른 관리자도, 총괄도 못 건드리고, 관리자·총괄 계정을 못 만든다.

    모르는 역할(None 아닌 사전 밖 값)은 명부 판과 같은 이유로 **닫는 쪽**이다.

    ⚠ **부르는 쪽은 실물을 다시 읽은 뒤 한 번 더 불러야 한다.** 첫 호출의 `target_role`은
    잠금 없는 SELECT 값이라, 판정과 쓰기 사이에 대상이 승격되면 낡은 급으로 통과한다 —
    account.py의 X20 주석이 못박은 그 함정과 같은 모양이다(2026-08-08 교차 검증).
    """
    if actor is None:
        return
    actor_level = RANK.get(actor.role, 0)
    is_self = target_user_id is not None and target_user_id == actor.id
    if actor.role == ROLE_OWNER:
        if is_self and (deactivate or (new_role is not None and new_role != ROLE_OWNER)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "총괄 관리자 계정은 스스로 강등하거나 해제할 수 없습니다. "
                    "새 총괄을 세운 뒤 그분이 강등해 주셔야 합니다."
                ),
            )
        return
    if is_self:
        if new_role is not None:
            new_level = RANK.get(new_role)
            if new_level is None or new_level > actor_level:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="자기 계정의 역할은 올릴 수 없습니다. 총괄 관리자에게 요청해 주세요.",
                )
        return
    for role in (target_role, new_role):
        if role is None:
            continue
        level = RANK.get(role)
        if level is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="역할을 알 수 없는 계정이라 처리할 수 없습니다. 총괄 관리자에게 요청해 주세요.",
            )
        if actor_level <= level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="자기보다 아랫급 계정만 처리할 수 있습니다. 같은 급 이상은 총괄 관리자에게 요청해 주세요.",
            )


def assert_not_self_link(actor: AuthActor | None, app_user_id: int | None) -> None:
    """새로 거는 계정 링크가 **자기 계정**이면 403. 등록·수정 둘 다 이 한 자리를 부른다.

    `assert_can_write`의 ③ 본인 항목 차단은 **대상 행의 지금 값**만 본다. 그래서 새로 넣는
    `app_user_id`는 그 검사를 안 지나고, 팀장이 자기 계정을 링크한 행을 새로 만들거나 남의
    행을 자기 링크로 갈아 끼울 수 있다 — `staff.app_user_id`에 유니크가 없어서 몇 장이든
    된다. 그러면 §2.5 "본인 항목은 관리자 몫"이 무력해진다(2026-08-02 백지 검토 3차 X32).

    갈래 순서는 `assert_can_write`와 같다 — 플래그 off 구간(actor None)은 통과, **총괄은**
    무조건 통과다. 총괄까지 막으면 admin 급 행의 링크를 아무도 못 거는 잠금이 된다.

    ⚠ **예전에는 `admin`이 이 자리를 비켜갔다.** 총괄 등급이 생기면서 그 예외를 총괄로
    옮겼다(2026-08-05 프론트 20차) — 관리자도 이제 자기 계정 링크는 못 건다. 그래야
    `assert_can_write`의 본인 항목 규칙과 한 방향이다.
    """
    if actor is None or actor.role == ROLE_OWNER:
        return
    if app_user_id is not None and app_user_id == actor.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="본인 계정을 연결한 항목은 직접 만들거나 바꿀 수 없습니다. 관리자에게 요청해 주세요.",
        )


async def assert_not_last_admin(db: AsyncSession, user_id: int) -> None:
    """이 계정을 내리면 관리자가 0이 되나. 그러면 403을 던진다(§4).

    ⚠ **행을 잠그고 개수는 파이썬에서 센다.** 집계 함수와 `FOR UPDATE`를 같이 쓰면
    Postgres가 거부한다(`FOR UPDATE is not allowed with aggregate functions`). 그래서
    id만 뽑아 잠근 뒤 len()으로 센다.

    락이 없으면 동시 강등 두 건이 **둘 다** 자기 시점에 "관리자 2명"을 보고 통과해
    관리자가 0이 된다. 잠근 뒤에는 뒤에 온 트랜잭션이 앞 커밋을 기다렸다가 다시 읽어서
    (READ COMMITTED의 잠금 재평가) 이미 내려간 사람을 결과에서 뺀 채 세게 된다.

    `ORDER BY id`를 붙인 이유는 데드락 방지다 — 여러 트랜잭션이 같은 순서로 잠근다.

    ⚠ 이 함수를 부른 트랜잭션이 **강등 UPDATE까지 같이 커밋해야** 락이 뜻을 가진다.
    잠그고 커밋해 버린 뒤에 UPDATE를 따로 내면 그 사이가 다시 열린다.
    """
    admin_ids = (
        await db.execute(
            select(AppUser.id)
            .where(AppUser.role == ROLE_ADMIN, AppUser.is_active.is_(True))
            .order_by(AppUser.id)
            .with_for_update()
        )
    ).scalars().all()
    if user_id in admin_ids and len(admin_ids) <= 1:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="마지막 관리자 계정은 강등하거나 해제할 수 없습니다.",
        )
