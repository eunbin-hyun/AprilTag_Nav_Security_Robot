"""크레딧 상태머신 (아키텍처 2026-07-23 §03 · 역할상세 §1.5).

미태깅 감지의 심장이다. 판별 축은 "빔을 몇 개 지났나"가 아니라
**유효 크레딧이 있는 상태에서 A→B 순서로 지나갔나**다.

크레딧 한 장의 생애.
- 발급  태깅이 오면 그 게이트에 크레딧 한 장(issued)이 생긴다. 만료 시각은 관측 시각 + TTL.
        같은 카드가 또 오면 이전 크레딧을 만료로 덮고 새로 낸다(한 사람이 두 장을 못 쌓게).
- 소비  A→B 통과가 오면 **먼저 그 방향 주장이 같은 이벤트의 빔 시각과 맞는지 본다**
        (`direction_beam_conflict` — 빔 시각이 하나도 없거나 선후가 거꾸로면 크레딧을
        안 태우고 보수 판정으로 내린다). 주장이 실물과 맞을 때만, 그 게이트에서 아직 안 쓰인
        유효 크레딧 중 먼저 발급된 것(FIFO)
        한 장을 쓴다. UID 최근접이 아니다 — 빔은 사람을 못 알아보니 소비 범위는 게이트다.
        소비 창은 **양쪽이 닫힌 구간**이다 — `발급시각 <= 통과시각 < 만료시각`. 위쪽만 보면
        아직 오지도 않은 미래 태깅이 과거 통과를 정상으로 만든다(`_consume_fifo` 참고).
        태운 크레딧의 **카드 UID를 통과 행에 적는다**(S15P11C207-241 · `gate_pass_event.tag_id`).
        예전엔 익명 크레딧을 수량으로만 세서 A가 찍고 B가 지나가도 서버가 아무 이름도 못
        댔다. ⚠ 이 UID는 "누가 지나갔나"의 증거가 아니라 **어느 크레딧을 태웠나**의 기록이다 —
        고르는 규칙이 그대로 게이트 FIFO라, 줄이 엉키면 적히는 이름도 같이 엉킨다.
- 만료  타이머를 안 돌린다. 빔 시점에 now(=beam_a_ts) > expires_at 을 비교하는 lazy expiry.
        재배포로 서버가 껐다 켜져도 복원할 게 없다.
- 청소  판정이 끝난 뒤 남은 만료분을 expired로 닫는다. 이 청소만 **시계 둘의 합의**로 돈다 —
        이벤트 시각과 서버 수신 시각이 다 만료로 볼 때만 닫는다(min을 기준으로 비교). 기기
        시각 하나로 지우면 라파이 시계가 미래로 튀는 순간 유효 크레딧까지 통째로 만료되고,
        서버 시각 하나로 지우면 백로그·배속으로 뒤늦게 들어온 통과가 남의 유효 크레딧을
        닫아 정상 통과를 미태깅으로 뒤집는다. 판정 창은 그대로 이벤트 시각이다.
- 경고  A→B로 지나갔는데 쓸 크레딧이 없으면 미태깅이다. **모든 미태깅 사건은 Alert 행을
        남긴다** — 쿨다운은 외부 알림(팝업·매터모스트) 중복만 억제하고 사건 기록은 안 건드린다
        (`evaluate_pass` 아래 주석). B→A 퇴장은 경고 대상이 아니라 기록만 남긴다. 크레딧
        회수는 끊었다(`EXIT_RECOVERY_ENABLED` 주석에 근거). 만료분을 닫는 일은 청소 몫이고,
        입장(A→B)·퇴장(B→A) 두 경로 다 판정 뒤에 같은 청소를 밟아 만료분이 issued로 쌓이지
        않게 한다.

통과 행 `tag_id` 칸의 출처는 둘이다 (F22).
- 소비한 크레딧의 UID — 위 "소비" 규칙 그대로다. 크레딧을 태운 판정에서만 찬다.
- **기기가 실제로 읽은 UID** — 인입이 `tag_id`를 실어 보내면 판정과 무관하게 그 값을 남긴다.
  미태깅·퇴장·보류에도 남으므로, 통과 행에서 신원으로 가는 길(`/api/staff/by-tag/{tag_id}`)이
  미태깅 경고에서도 열린다. 크레딧 쪽 UID가 있으면 그쪽이 이긴다(소비 기록이 더 정확한 사실).

⚠ 서버가 이름을 **지어내지는 않는다.** "이 게이트 최근 태깅"으로 칸을 채우는 길은 여전히
없다 — 방금 만료된 사람 이름이 미태깅 통과에 붙는다(`tests/test_credit_identity.py` ②③).
기기가 안 실어 보내면 예전처럼 NULL이다.

소비·회수는 조건부 UPDATE 한 방으로 원자화한다(통과 두 건이 같은 크레딧을 집으면 안 되니까).
UPDATE ... WHERE id = (SELECT ... ORDER BY 발급순 FOR UPDATE SKIP LOCKED LIMIT 1) 꼴이다.

M-5(카드 UID↔학번 매핑)는 팀 미정이라 tag_id를 그대로 쓴다. 매핑은 넣지 않는다.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.credit.idempotency import as_utc as _as_utc
from app.credit.idempotency import conflicting_fields
from app.models import Alert, GatePassEvent, TaggingEvent

logger = logging.getLogger("c207.credit")

# ── 미래 시각 스큐 한도 ────────────────────────────────────────────────────
# 이벤트 시각이 서버 수신 시각보다 이만큼 넘게 앞서면 시계가 잘못된 것으로 보고 경고를 남긴다.
#
# 값 근거 — 실기기(raspberry01) 태깅 12건의 `observed_at - received_at`이 -11.9ms ~ -18.9ms로
# 전부 **음수**다(2026-07-30 실서버 실측). 실제 기기는 미래로 안 튄다. 60초는 관측 폭의
# 3천 배라 정상 지터로는 절대 안 걸리고, NTP가 나간 기기나 손입력 자료만 걸린다
# (실서버에 KST를 UTC로 넣은 `palm-test-1785305915` 행이 있다).
#
# ⚠ 넘어도 **거절하지 않는다.** 라파 `gate_server.py`의 sender_worker가
# 2xx가 아닌 응답을 무한 재시도하는 구조라(4xx도 포기하지 않는다) 422·409 한 건이 그 기기의
# 전송 큐를 통째로 영구히 막는다. 그래서 여기서는 기록·경고만 하고, 미래 시각이 판정을
# 뒤집는 건 소비 창 아래쪽 경계(`_consume_fifo`)로 막는다. 진짜 거절로 올릴지는 기기 쪽
# 재시도 정책을 같이 고쳐야 하는 팀 결정이다.
MAX_FUTURE_SKEW_SEC = 60.0

# ── [팀 결정 필요] 퇴장 회수를 되살리는 스위치 ─────────────────────────────
# 정본(`docs/아키텍처_2026-07-23.html:468`)은 "B→A 퇴장은 그 게이트의 가장 오래된 미소비
# 크레딧 한 장을 회수한다"고 적었는데, 이 회수는 **구조적으로 남의 크레딧만 먹는다.**
#
# 물리 근거 — 태깅은 게이트 바깥(빔 A 쪽)에서 일어난다. 라파가 방향을 `beam_a_ts < beam_b_ts`
# 로 가르니(`gate_server.py`) B→A 퇴장은 **안쪽에서 나오는 몸**이다. 크레딧을
# 쥔 사람은 아직 바깥에 서 있어서 그 퇴장을 만들 수가 없다. 즉 회수 대상은 언제나 제3자의
# 크레딧이고, 실측 S2에서 A의 크레딧이 `recovered`로 회수돼 A의 정상 입장이 `untagged`로
# 뒤집혔다(수량 불일치도 안 생기는 깨끗한 오탐). 1인 직렬화로도 안 막힌다 — 들어오는 사람과
# 나가는 사람은 다른 사람이다.
#
# 얻는 것도 없다. 안 쓰인 크레딧은 TTL 3초면 청소(`_expire_overdue`)가 닫는다.
#
# 그래서 기본값을 False로 두고 회수를 끊었다. 정본 한 줄을 벗어나는 판단이라 **팀 결정으로
# 올렸다** — 되돌리려면 이 상수만 True로 하면 예전 거동이 그대로 돌아온다(그 경로도
# `test_verdict_hardening.py`가 시험으로 지킨다). 되돌릴 때 정본 468행도 같이 고친다.
EXIT_RECOVERY_ENABLED = False


# ── 방향 주장 대 빔 시각 정합 (S15P11C207-243) ─────────────────────────────
# 기기가 보낸 `direction`은 **주장**이지 관측값이 아니다. 라파는 빔 두 개의 선후로 방향을
# 만드는데(`hardware/rpi/gate_server.py` — `A_TO_B if beam_a_ts < beam_b_ts else B_TO_A`),
# 서버는 그 주장만 보고 A_TO_B면 크레딧을 태웠다. 그래서 같은 이벤트 안에서 주장과 빔 시각이
# 어긋나도(빔이 하나도 없어도, 순서가 거꾸로여도) 정상 통과가 되고 남의 크레딧이 사라졌다
# (실측 반증 — `tests/test_beam_direction_guard.py` 머리 주석에 수리 전 값이 있다).
#
# 검사는 **이벤트 한 건 안에서만** 한다. 벽시계·서버 수신 시각과 견주는 축은 안 늘린다 —
# 만료 청소(min)·퇴장 회수(max)가 쓰는 시계 두 축 체계(S15P11C207-242)를 여기서 건드리면
# 그 수리가 통째로 흔들린다.
#
# ⚠ 이 검사는 **위조 방지가 아니다.** API 키를 쥔 쪽은 언제든 정상 모양(빔 A 먼저·빔 B
#   나중)을 지어내 크레딧을 태울 수 있고, 그건 서버가 센서를 직접 안 재는 한 못 막는다.
#   여기서 얻는 건 두 가지다 — ① 배선이 뒤집혔거나 값을 안 싣는 **고장 기기를 짚어내고**
#   ② 실물이 못 받쳐 주는 주장으로는 **남의 크레딧을 안 태운다**.
#
# 허용 모양은 **라파가 실제로 만드는 것만** 추린 화이트리스트다(`hardware/rpi/gate_server.py`).
#  - `emit(..., "complete")` — 빔 둘을 다 싣고, 방향은 `A_TO_B if beam_a_ts < beam_b_ts`다.
#    즉 라파가 A_TO_B라고 적는 순간 **a < b가 이미 참**이다(a == b면 라파는 B_TO_A라 적는다).
#  - `emit_tagged_entry(beam_b_ts)` — 태깅 직후 빔 B 확인만으로 확정하는 정상 입장.
#    빔 A를 일부러 안 읽어서 `beam_a_ts=null`이다. 이 모양(b_only)을 막으면 시연 본 갈래가
#    통째로 죽는다 — 지라 카드 문구("beam_a_ts가 없어도")를 글자대로 따르면 안 되는 자리다.
# 나머지 넷(빔 없음·빔 A만·역순·같은 눈금)은 라파가 못 내는 모양이라 어긋남으로 본다.
BEAM_CONFLICT_NO_TS = "no_beam_ts"            # 방향을 주장했는데 빔 시각이 하나도 없다
BEAM_CONFLICT_ORDER = "beam_order_reversed"    # 빔 A가 빔 B보다 늦게 끊겼다 (a > b)
BEAM_CONFLICT_SAME_TS = "beam_ts_equal"        # 눈금이 같다 — 라파 판별식이면 B_TO_A였을 값
BEAM_CONFLICT_B_MISSING = "beam_b_missing"     # 빔 A만 있다 — complete인데 나가는 쪽 확인이 없다


def direction_beam_conflict(
    direction: str | None,
    beam_a_ts: dt.datetime | None,
    beam_b_ts: dt.datetime | None,
) -> str | None:
    """A→B 주장이 같은 이벤트의 빔 시각과 어긋나면 그 사유를, 아니면 None을 돌려준다.

    A→B만 본다. 크레딧을 태우는 갈래가 거기 하나뿐이라서다 — 퇴장(B→A)은 회수가 꺼져 있어
    (`EXIT_RECOVERY_ENABLED`) 크레딧을 안 건드리고, 그쪽까지 순서를 따지면 `exit` 기록이
    `beam_incomplete`로 바뀌어 통계 칸과 dev 시험 자산이 같이 흔들린다. 퇴장에도 같은 잣대를
    댈지는 팀 결정으로 올렸다.

    통과 모양은 딱 둘이다(위 화이트리스트 주석 참고).
      - 빔 둘 다 있고 `a < b`  — `emit(complete)`
      - 빔 B만 있음            — `emit_tagged_entry`

    ⚠ `a == b`도 어긋남으로 본다. 라파 판별식(`A_TO_B if a < b else B_TO_A`)이면 같은 눈금은
    **B_TO_A로 적히는 값**이라, A_TO_B 주장과 같이 오면 그 자체로 자기모순이다. 1차 수리는
    "굵은 시계가 접은 모양"이라며 통과시켰는데, 그건 `tools/dummy_publisher.py`가 빔 두 칸에
    `now_iso()`를 각각 불러 **이동 시간 0초짜리 통과**를 만들던 시늉 도구 결함이었다(그 도구를
    같이 고쳤다). 실기기 빔 두 개는 사람이 지나가는 수백 ms 사이에 끊겨서 µs 시계로는 안 접힌다.
    """
    if direction != "A_TO_B":
        return None
    if beam_a_ts is None and beam_b_ts is None:
        return BEAM_CONFLICT_NO_TS
    if beam_b_ts is None:
        return BEAM_CONFLICT_B_MISSING
    if beam_a_ts is None:
        return None  # b_only — `emit_tagged_entry`가 만드는 정상 태깅 입장
    a, b = _as_utc(beam_a_ts), _as_utc(beam_b_ts)
    if a > b:
        return BEAM_CONFLICT_ORDER
    if a == b:
        return BEAM_CONFLICT_SAME_TS
    return None


class CreditState:
    ISSUED = "issued"
    CONSUMED = "consumed"      # A→B 통과가 정상적으로 소비
    EXPIRED = "expired"        # 재태깅으로 덮여 무효화 (만료 자체는 lazy라 상태를 안 바꾼다)
    RECOVERED = "recovered"    # B→A 퇴장으로 회수


class Verdict:
    NORMAL = "normal"                  # 유효 크레딧 소비
    UNTAGGED = "untagged"              # A→B인데 쓸 크레딧 없음 → 미태깅
    # 한쪽 빔만 끊겼거나(방향 판별 불가), 방향 주장이 빔 시각과 어긋남(-243) → 기록만.
    # 둘 다 "그 방향으로 지나갔다는 걸 증명 못 한 사건"이라 같은 보수 판정으로 접는다.
    # 새 낱말을 만들면 통계 칸(TodayGatePassCounts)·화면 계약이 같이 흔들린다.
    BEAM_INCOMPLETE = "beam_incomplete"
    EXIT = "exit"                      # B→A 퇴장 → 경고 없이 기록·회수


ALERT_TYPE_UNTAGGED = "untagged"

# 쿨다운 키의 "종류" 칸에 들어가는 외부 알림 채널. 채널마다 창을 따로 든다 — 한 채널이
# 삼켜졌다고 다른 채널까지 조용해지면 안 된다(예전엔 불리언 하나로 둘이 같이 빠졌다).
# ⚠ Alert 행 자체는 이 키를 안 본다. 기록은 쿨다운 밖이다.
_CHANNEL_DASHBOARD = f"{ALERT_TYPE_UNTAGGED}:dashboard"
_CHANNEL_CHAT = f"{ALERT_TYPE_UNTAGGED}:chat"


def _who_key(tag_id: str | None) -> str:
    """쿨다운 창을 **사람마다** 가르는 조각(2026-08-06 · 프론트 40차 §6, 양쪽 같은 판단).

    ⛔ **왜 필요한가** — 키가 `(게이트, 방, 채널)`뿐이라 사람을 안 봤다. 그래서 10초 안에
    두 사람이 줄지어 지나가면 **두 번째가 통째로 조용했다.** 무단 통과는 모달까지 띄우는
    유일한 종류라(요원이 화면을 안 보고 있을 수 있어 소리와 팝업을 겹쳐 뒀다) 그 겹표기가
    통째로 무의미해진다. 시연에서 사람이 줄지어 지나가는 건 아주 흔한 그림이다.

    ⭐ **원래 목적은 그대로 지킨다** — 쿨다운은 "게이트에 물건이 놓여 같은 자리가 반복되는"
    폭주를 막으려고 뒀다. 그건 같은 카드(또는 카드 없는 같은 상황)라 여전히 한 창에 묶인다.

    ⛔ **카드 UID가 없으면 예전처럼 한 창으로 묶는다.** 프론트에 처음 답할 때는 "통과 번호를
    써서 매번 다른 창으로 두겠다"고 했는데, **그러면 쿨다운이 통째로 죽는다** — 지금 라파이가
    UID를 아예 안 실어서(8/6 실기기 대조) 사실상 모든 통과가 이 갈래다.

    실측으로 갈랐다. 오늘 미태깅 17건 중 10초 안에 이어진 것이 3건이고 **최소 간격이
    1.1초**였다. 사람이 1.1초 간격으로 줄지어 지나가기는 어렵다 — 게이트에 물건이 놓였거나
    같은 사람이 겹친 쪽에 가깝고, 그게 이 쿨다운이 원래 막으려던 그림이다.

    ⏸ **UID가 실리기 시작하면 위 갈래가 저절로 산다.** 그때부터 다른 사람의 미태깅은 안
    삼켜진다. 라파이에 UID 싣기를 요청해 뒀고, 그게 이 문제의 진짜 해법이다.
    """
    return f"tag:{tag_id}" if tag_id else "anon"


@dataclass
class IssueResult:
    stored: bool          # 이번 요청으로 새 태깅 행이 생겼나(멱등)
    tagging_id: int | None
    superseded: int       # 재태깅으로 덮은 이전 크레딧 수


@dataclass
class PassEvaluation:
    stored: bool                       # 이번 요청으로 새 통과 행이 생겼나(멱등)
    pass_id: int | None                # 중복이면 이미 저장돼 있던 행의 id
    verdict: str | None                # 중복이어도 원래 판정을 그대로 돌려준다
    matched_tagging_event_id: int | None
    # 미태깅이면 **항상** 채워진다. 쿨다운은 알림만 막고 사건 기록은 안 막는다.
    alert_id: int | None
    low_confidence: bool               # 미소비 유효 크레딧 과다 플래그
    # 통과 행에 실제로 적힌 카드 UID(S15P11C207-241 · F22). 출처는 둘이다 — 소비한 크레딧의
    # UID가 우선이고, 없으면 기기가 인입에 실어 보낸 관측 UID다. 둘 다 없으면 None.
    # ⚠ `matched_tagging_event_id`와 늘 짝이던 계약은 앞쪽 출처에만 남는다 — 기기가 UID를
    # 실어 보낸 미태깅 통과는 이 칸만 차고 `matched_tagging_event_id`는 None이다.
    tag_id: str | None = None
    # 외부 알림 판정을 채널마다 따로 든다. 예전엔 불리언 하나라 대시보드 팝업과 매터모스트
    # 카드가 한꺼번에 빠졌다 — 한 채널이 삼켜졌다고 다른 채널까지 조용해지면 안 된다.
    notify_dashboard: bool = False     # 커밋 뒤 WS `untagged_alert` 팝업 대상인가
    notify_chat: bool = False          # 커밋 뒤 매터모스트 카드 대상인가
    # 같은 event_id로 왔는데 본문이 저장된 행과 어긋난 칸들(비어 있으면 순수 재시도).
    conflicting_fields: list[str] = field(default_factory=list)
    # 방향 주장이 빔 시각과 어긋난 사유(S15P11C207-243). 어긋남이 없었으면 None이다.
    # 어느 기기가 이상한 주장을 보내는지 짚는 자리라 **호출자가 실제로 내보내야 값이 산다** —
    # `routers/ingest.py`가 WS `gate_pass_event`와 gate-pass 응답(`GatePassIngestAck`)에
    # 싣는다. 중복 재전송 응답에도 저장 행에서 다시 뽑아 담는다(`_describe_duplicate_pass`).
    beam_conflict: str | None = None

    @property
    def notify(self) -> bool:
        """외부 알림이 하나라도 나가나. 채널별 판정이 생기기 전 계약을 그대로 읽는 자리다."""
        return self.notify_dashboard or self.notify_chat

    @property
    def payload_conflict(self) -> bool:
        return bool(self.conflicting_fields)


# ── [초안·팀 확정 대기] 안건① 크레딧 소비 범위 ─────────────────────────────
# 지금 소비 범위는 게이트 하나다. 시뮬이 룸을 여러 개 돌리면 A룸 태깅을 B룸 통과가
# 집어가는 교차 소비가 난다(실측 근거: worklog/2026-07-26_시뮬갭_이벤트계약.md).
# 정공법은 범위를 (gate_no, room_id)로 좁히는 건데, 확정 전까지는 플래그로만 연다.
# credit_scope_room=False면 room 절을 아예 안 붙여서 기존 판정이 한 글자도 안 바뀐다.


def _scope_clauses(model, gate_no: int | None, room_id: str | None) -> list:
    clauses = [model.gate_no.is_not_distinct_from(gate_no)]
    if get_settings().credit_scope_room:
        clauses.append(model.room_id.is_not_distinct_from(room_id))
    return clauses


def _cooldown_room(room_id: str | None) -> str | None:
    """쿨다운 키에 쓸 룸. 플래그가 꺼져 있으면 항상 None이라 기존 키와 같아진다."""
    return room_id if get_settings().credit_scope_room else None


# ── 쿨다운 상태 (인메모리) ─────────────────────────────────────────────────
# 워커 1개·단일 이벤트 루프 전제라 인메모리로 충분하다. 재배포로 리셋돼도
# 쿨다운은 초 단위 짧은 창이라 복원할 필요가 없다(크레딧 상태와 성격이 다르다).
# 창은 두 축을 AND로 묶는다 — 이벤트 시각(판정과 같은 축)과 서버 벽시계 둘 다
# 창 안일 때만 삼킨다. 한 축만 쓰면 각각 반대 방향으로 깨진다(_in_cooldown 참고).
# 값은 (마지막 경고의 이벤트 시각, 그때의 벽시계 눈금)이다.
#
# 키 축과 값 축은 서로 다른 안건에서 왔고 둘 다 살린다(2026-07-28 dev 현행화).
#  - 키   (gate_no, 쿨다운 룸, 종류)  ← [초안·팀 확정 대기] 안건① 룸 단위 크레딧
#  - 값   (이벤트 시각, 벽시계 눈금)  ← dev 쿨다운 두 축 수리(S15P11C207-82)
# 안건① 플래그가 꺼져 있으면 _cooldown_room이 항상 None이라 키가 dev와 같은 자리에
# 떨어진다 — 즉 기본값에서는 dev 거동과 한 글자도 안 달라진다.
_last_alert_at: dict[tuple[int | None, str | None, str], tuple[dt.datetime, float]] = {}


def _wall_now() -> float:
    """쿨다운 벽시계 축. 단조 시계라 서버 NTP 보정에도 안 튀고 경과가 음수로 안 나온다.
    시험이 실제 10초를 안 기다리고 시간을 감을 수 있게 함수로 뺐다."""
    return time.monotonic()


def _warn_future_skew(kind: str, event_id: str, event_ts: dt.datetime, server_now: dt.datetime) -> float:
    """이벤트 시각이 서버 수신 시각보다 앞선 폭(초)을 재고, 한도를 넘으면 경고를 남긴다.

    거절은 안 한다(`MAX_FUTURE_SKEW_SEC` 주석 — 기기 큐가 막힌다). 판정을 뒤집는 건
    소비 창 경계가 막고, 여기는 "그 기기 시계가 틀렸다"를 운영자가 알아채는 자리다.
    """
    skew = (_as_utc(event_ts) - server_now).total_seconds()
    if skew > MAX_FUTURE_SKEW_SEC:
        logger.warning(
            "미래 시각 스큐 %.1f초 — 한도 %.1f초 초과 (%s event_id=%s). 기록은 남기고 판정은"
            " 소비 창 경계로 막는다. 기기 시계(NTP)를 확인해라.",
            skew, MAX_FUTURE_SKEW_SEC, kind, event_id,
        )
    return skew


def _in_cooldown(
    gate_no: int | None, room_id: str | None, alert_type: str, now: dt.datetime
) -> bool:
    """이벤트 시각 창과 벽시계 창을 둘 다 만족할 때만 삼킨다(AND).

    한 축만 쓰면 반대 방향으로 하나씩 깨진다.
    - 이벤트 축만: 기기 시계가 멈추면 경과가 계속 0이라 경고가 영구 봉쇄된다.
    - 벽시계 축만: 배속 시연에서 같은 사건이 더 촘촘히 도착해 경고가 조용히 준다(E2E F2).

    경과가 음수인 인입(기기 사이 시계 어긋남·순서 뒤바뀐 도착)은 이벤트 축에서 창 안으로 본다.
    음수를 우회로 뚫어주면 시계가 어긋난 기기 둘이 번갈아 들어올 때 매번 걸려 쿨다운이
    통째로 무력화된다. 봉쇄 걱정은 벽시계 축이 받아준다 — 실제 시간이 창을 넘으면 다시 울린다.

    [초안·팀 확정 대기] 키의 룸 칸은 `_cooldown_room`이 정한다. 안건① 플래그가 꺼져 있으면
    항상 None이라 게이트 하나로 묶이는 예전 창 그대로다.
    """
    last = _last_alert_at.get((gate_no, _cooldown_room(room_id), alert_type))
    if last is None:
        return False
    last_event_ts, last_wall = last
    cooldown = get_settings().alert_cooldown_sec
    if (now - last_event_ts).total_seconds() >= cooldown:
        return False
    return (_wall_now() - last_wall) < cooldown


def _mark_alert(
    gate_no: int | None, room_id: str | None, alert_type: str, now: dt.datetime
) -> None:
    """실제로 경고가 나갈 때만 찍는다(고정 창). 삼킬 때도 찍으면 창이 계속 밀려
    미태깅이 촘촘히 이어질 때 경고가 영영 안 나온다."""
    _last_alert_at[(gate_no, _cooldown_room(room_id), alert_type)] = (now, _wall_now())


def _unmark_alert(
    gate_no: int | None,
    room_id: str | None,
    alert_type: str,
    previous: tuple[dt.datetime, float] | None,
) -> None:
    """`_mark_alert`로 찍은 예약 표시를 되돌린다. **알림이 결국 안 나갔을 때만** 부른다.

    표시를 커밋 뒤로 미루면 검사와 표시 사이에 await가 끼어서 동시 두 건이 둘 다 창을
    통과한다 — 대시보드 팝업과 매터모스트 카드가 겹친다. 그래서 검사 직후에 찍고, 커밋이
    터진 갈래에서만 여기로 되돌린다(`dispatch.rollback_command_throttle`과 같은 방식).
    """
    key = (gate_no, _cooldown_room(room_id), alert_type)
    if previous is None:
        _last_alert_at.pop(key, None)
    else:
        _last_alert_at[key] = previous


def _peek_alert_mark(
    gate_no: int | None, room_id: str | None, alert_type: str
) -> tuple[dt.datetime, float] | None:
    """되돌리기용으로 지금 표시를 떠 둔다(없으면 None)."""
    return _last_alert_at.get((gate_no, _cooldown_room(room_id), alert_type))


def reset_cooldowns() -> None:
    """시험 격리용. 상태 오염이 다음 케이스로 새지 않게 초기화한다."""
    _last_alert_at.clear()


# ── 발급 (태깅 인입) ────────────────────────────────────────────────────────

async def issue_credit(
    session: AsyncSession,
    *,
    event_id: str,
    device_id: str | None,
    gate_no: int | None,
    tag_id: str,
    observed_at: dt.datetime,
    room_id: str | None = None,
    boot_id: str | None = None,
    is_demo: bool = False,
) -> IssueResult:
    """태깅 한 건으로 크레딧을 발급한다. 멱등(event_id 충돌이면 아무것도 안 함)."""
    settings = get_settings()
    expires_at = observed_at + dt.timedelta(seconds=settings.credit_ttl_sec)
    _warn_future_skew("tagging", event_id, observed_at, dt.datetime.now(dt.timezone.utc))

    insert_stmt = (
        pg_insert(TaggingEvent)
        .values(
            event_id=event_id,
            device_id=device_id,
            boot_id=boot_id,
            gate_no=gate_no,
            room_id=room_id,
            tag_id=tag_id,
            observed_at=observed_at,
            credit_state=CreditState.ISSUED,
            expires_at=expires_at,
            # ⭐ 시연 도구가 만든 행인가. 발급 규칙에는 한 줄도 안 쓴다 — 나중에 골라
            # 지우려고만 남기는 표시다(2026-08-05 프론트 22차).
            is_demo=is_demo,
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(TaggingEvent.id)
    )
    new_id = (await session.execute(insert_stmt)).scalar_one_or_none()
    if new_id is None:
        # 재시도 중복 — 새 행이 없으니 발급·덮어쓰기도 없다.
        await session.rollback()
        return IssueResult(stored=False, tagging_id=None, superseded=0)

    # 같은 카드 재태깅: 그 카드의 issued 크레딧 중 **관측이 가장 늦은 한 장만** 남기고 나머지를
    # 만료로 덮는다. "한 사람이 두 장을 못 쌓게" 하는 규칙은 그대로인데, 기준이 도착 순서가
    # 아니라 관측 시각이다.
    #
    # 예전엔 자기 자신만 빼고 전부 덮어서, 늦게 도착한 **옛** 사건이 이미 있던 최신 크레딧을
    # 만료시켰다(실측 P0-2-b — 정상 태깅자가 미태깅 경고를 맞았다). 관측 시각으로 남길 한 장을
    # 고르면 도착 순서가 뒤바뀌어도 결과가 같다 — 늦게 온 옛 사건은 자기가 만료된다.
    #
    # gate_no가 None이어도 None끼리 매칭되게 IS NOT DISTINCT FROM을 쓴다.
    card_credits = (
        *_scope_clauses(TaggingEvent, gate_no, room_id),
        TaggingEvent.tag_id == tag_id,
        TaggingEvent.credit_state == CreditState.ISSUED,
    )
    keeper = (
        select(TaggingEvent.id)
        .where(*card_credits)
        .order_by(TaggingEvent.observed_at.desc(), TaggingEvent.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    supersede = (
        update(TaggingEvent)
        .where(*card_credits, TaggingEvent.id != keeper)
        .values(credit_state=CreditState.EXPIRED)
        .returning(TaggingEvent.id)
    )
    expired_ids = [row[0] for row in (await session.execute(supersede)).all()]
    await session.commit()
    if new_id in expired_ids:
        logger.warning(
            "늦게 도착한 옛 태깅 — 더 최신 크레딧이 이미 있어 이 건을 만료로 발급했다"
            " (event_id=%s tag_id=%s)", event_id, tag_id,
        )
    return IssueResult(
        stored=True,
        tagging_id=new_id,
        superseded=len([i for i in expired_ids if i != new_id]),
    )


# ── 소비·회수 원자 연산 ─────────────────────────────────────────────────────

def _live_credit_window(at: dt.datetime) -> tuple:
    """`at` 시점에 **살아 있는** 크레딧 조건 두 개. `발급시각 <= at < 만료시각`이다.

    아래쪽 경계가 이 수리의 핵심이다. 예전엔 위쪽(`expires_at > at`)만 봐서 **아직 오지도
    않은 미래 태깅이 과거 통과를 정상화**했다 — 1시간 미래 태깅 한 건이 1시간 전 통과를
    `normal`로 만들고, TTL 3초가 스큐만큼 늘어났다(실측 P0-2-a).

    아래쪽 경계를 `observed_at <= at`이 아니라 `expires_at <= at + TTL`로 적은 이유 —
    `expires_at`은 발급 때 `observed_at + TTL`로 굳은 값이라 두 식이 같은데, 이렇게 쓰면
    양쪽 경계가 **같은 컬럼 하나**를 보게 돼서 인덱스도 하나로 끝나고 "창 하나"라는 뜻이
    코드에 그대로 남는다. 그 인덱스가 실물로 있다 — `ix_tagging_event_credit_window`
    (`credit_state, expires_at, observed_at` · 마이그레이션 0009). ⚠ 축에 `gate_no`가
    없는 건 뺀 게 맞다. `_scope_clauses`가 거는 `IS NOT DISTINCT FROM`을 Postgres가
    인덱스 조건으로 못 써서, 앞에 세우면 인덱스가 통째로 안 잡힌다(0009 머리에 실측).

    ⚠ 이 등가는 `credit_ttl_sec`이 발급 시점과 같을 때만 성립한다. 설정을 배포 중에 바꾸면
    이미 발급된 크레딧의 아래쪽 경계가 새 TTL만큼 밀린다. TTL은 배포 단위 고정값이라 지금은
    문제가 없지만, 런타임 변경을 허용하려면 발급 시각 컬럼을 직접 봐야 한다.
    """
    ttl = dt.timedelta(seconds=get_settings().credit_ttl_sec)
    return (
        TaggingEvent.expires_at > at,        # lazy expiry: 아직 안 만료
        TaggingEvent.expires_at <= at + ttl,  # 이미 발급됨 (= observed_at <= at)
    )


async def _consume_fifo(
    session: AsyncSession,
    gate_no: int | None,
    compare_ts: dt.datetime,
    pass_id: int,
    room_id: str | None = None,
) -> tuple[int, str] | None:
    """그 게이트의 유효 미소비 크레딧 중 먼저 발급된 한 장을 소비하고 (행 id, 카드 UID)를 준다.

    조건부 UPDATE 한 방(FOR UPDATE SKIP LOCKED)이라 통과 두 건이 같은 크레딧을 못 집는다.
    유효 크레딧이 없으면(=만료됐거나 애초에 없음) None을 돌려준다.

    UID를 **같은 UPDATE의 RETURNING으로** 가져온다(S15P11C207-241). 골라 잡은 뒤 따로
    SELECT하면 그 사이 재태깅·회수가 같은 행을 건드릴 수 있어서, 통과 행에 적히는 UID가
    실제로 태운 크레딧과 어긋날 여지가 생긴다. 소비와 UID 읽기가 한 문장이면 그 틈이 없다.
    """
    pick = (
        select(TaggingEvent.id)
        .where(
            *_scope_clauses(TaggingEvent, gate_no, room_id),
            TaggingEvent.credit_state == CreditState.ISSUED,
            *_live_credit_window(compare_ts),
        )
        .order_by(TaggingEvent.observed_at.asc(), TaggingEvent.id.asc())  # FIFO
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    stmt = (
        update(TaggingEvent)
        .where(TaggingEvent.id == pick)
        .values(
            credit_state=CreditState.CONSUMED,
            consumed_at=func.now(),
            consumed_by_pass_id=pass_id,
        )
        .returning(TaggingEvent.id, TaggingEvent.tag_id)
    )
    row = (await session.execute(stmt)).one_or_none()
    return None if row is None else (row.id, row.tag_id)


async def _expire_overdue(
    session: AsyncSession,
    gate_no: int | None,
    sweep_ts: dt.datetime,
    room_id: str | None = None,
) -> int:
    """만료 시각이 지난 issued 크레딧을 expired로 닫고 닫은 수를 돌려준다(청소).

    lazy expiry라 평소엔 상태를 안 바꾸는데(판정은 소비 시점 비교로만 한다), 남은 issued가
    쌓이면 두 가지가 틀어진다 — 퇴장 회수가 만료를 안 따지고 집어 가고, 미소비 크레딧 수도
    부풀어 보인다. 그래서 판정 뒤에 한 번 밟는다.

    ⚠ `sweep_ts`는 **이벤트 시각과 서버 수신 시각 중 이른 쪽**(min)이다. 시계 둘이 다 만료로
    볼 때만 닫는다는 뜻이라, 어느 한쪽이 튀어도 남의 유효 크레딧을 안 건드린다.

    - 기기 시각 하나로 재면 라파이 시계가 미래로 한 번 튀는 순간 그 게이트의 유효 크레딧이
      통째로 만료된다(기기가 보낸 값 하나로 서버 자료가 지워지는 자리).
    - 서버 시각 하나로 재면 백로그·배속 재생처럼 인입이 늦은 자리에서, 이벤트 시각으로는
      아직 살아 있는 크레딧이 닫힌다. 통과 여러 건이 한꺼번에 밀려오면 첫 건만 normal이고
      나머지가 미태깅으로 뒤집힌다.

    판정 창(소비 가능 여부·저신뢰 집계)은 여기와 별개로 이벤트 시각 하나만 쓴다 — 몸이
    게이트에 들어선 순간이 기준이라야 맞다.

    [초안·팀 확정 대기] 청소 범위도 소비·회수와 같은 `_scope_clauses`를 쓴다. 범위가
    갈리면 A룸 통과가 B룸 만료분을 닫아 저신뢰 집계가 룸 사이로 샌다.
    """
    stmt = (
        update(TaggingEvent)
        .where(
            *_scope_clauses(TaggingEvent, gate_no, room_id),
            TaggingEvent.credit_state == CreditState.ISSUED,
            TaggingEvent.expires_at <= sweep_ts,
        )
        .values(credit_state=CreditState.EXPIRED)
        .returning(TaggingEvent.id)
    )
    return len((await session.execute(stmt)).all())


async def _recover_oldest(
    session: AsyncSession,
    gate_no: int | None,
    recover_ts: dt.datetime,
    issued_by_ts: dt.datetime,
    pass_id: int,
    room_id: str | None = None,
) -> int | None:
    """B→A 퇴장 시 그 게이트의 유효한 미소비 크레딧 중 가장 오래된 한 장을 회수한다.

    ⚠ 기본 배치에서는 **안 불린다**(`EXIT_RECOVERY_ENABLED=False`). 정본 468행을 되살릴
    팀 결정이 나면 이 경로가 다시 열리므로 시각 가드까지 맞춰 둔 상태로 남긴다.

    ⚠ `recover_ts`는 **이벤트 시각과 서버 수신 시각 중 늦은 쪽**(max)이다. 늦은 쪽을 넘기면
    비교 한 번으로 "양쪽 시계가 다 TTL 안으로 본다"가 성립할 때만 집힌다. 둘 중 하나라도
    만료로 보는 크레딧은 회수 대상에서 뺀다 — 회수하면 그 크레딧이 무관한 퇴장을
    가리키게 되고(F4 원건), 만료분을 닫는 일은 청소(`_expire_overdue`) 몫이다.

    가드가 없으면 청소만으로는 못 막는다. 청소는 이른 쪽(min) 기준이라, 기기 시계가 과거로
    튄 퇴장에서는 아무것도 안 닫는다 — 그 사이 회수가 벽시계로 이미 만료된 크레딧을 집어 가
    3차까지 고쳐 둔 결함이 되살아난다. 청소는 min, 회수는 max로 견주는 게 짝이다.

    아래쪽 경계(`issued_by_ts`)는 반대로 이른 쪽(min)으로 받는다 — 양쪽 시계가 다 "이미
    발급됐다"고 볼 때만 집는다는 뜻이다. 안 그러면 미래 시각 태깅이 지금 퇴장에 회수된다.

    회수할 게 없으면(유효 미소비 크레딧 없음) None. 이건 정상 상황이다.
    """
    ttl = dt.timedelta(seconds=get_settings().credit_ttl_sec)
    pick = (
        select(TaggingEvent.id)
        .where(
            *_scope_clauses(TaggingEvent, gate_no, room_id),
            TaggingEvent.credit_state == CreditState.ISSUED,
            TaggingEvent.expires_at > recover_ts,          # 양시계 유효분만
            TaggingEvent.expires_at <= issued_by_ts + ttl,  # 양시계 발급분만
        )
        .order_by(TaggingEvent.observed_at.asc(), TaggingEvent.id.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    stmt = (
        update(TaggingEvent)
        .where(TaggingEvent.id == pick)
        .values(
            credit_state=CreditState.RECOVERED,
            consumed_at=func.now(),
            consumed_by_pass_id=pass_id,
        )
        .returning(TaggingEvent.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _count_unconsumed(
    session: AsyncSession,
    gate_no: int | None,
    compare_ts: dt.datetime,
    room_id: str | None = None,
) -> int:
    """그 게이트에 지금 열려 있는(유효·미소비) 크레딧 수. 저신뢰 플래그 판정용.

    소비와 같은 창을 쓴다 — 아직 발급도 안 된 미래 태깅을 세면 저신뢰 플래그가 부풀어 뜬다.
    """
    stmt = (
        select(func.count())
        .select_from(TaggingEvent)
        .where(
            *_scope_clauses(TaggingEvent, gate_no, room_id),
            TaggingEvent.credit_state == CreditState.ISSUED,
            *_live_credit_window(compare_ts),
        )
    )
    return (await session.execute(stmt)).scalar_one()


# ── 판정 (빔 통과 인입) ─────────────────────────────────────────────────────

async def evaluate_pass(
    session: AsyncSession,
    *,
    event_id: str,
    device_id: str | None,
    gate_no: int | None,
    direction: str | None,
    status: str | None,
    beam_a_ts: dt.datetime | None,
    beam_b_ts: dt.datetime | None,
    observed_at: dt.datetime,
    room_id: str | None = None,
    boot_id: str | None = None,
    tag_id: str | None = None,
    is_demo: bool = False,
) -> PassEvaluation:
    """빔 통과 한 건을 적재하고 판정한다. 멱등 + 판정 + 크레딧 소비까지 한 트랜잭션.

    `tag_id`는 **기기가 그 통과에서 실제로 읽은 카드 UID**다(F22). 판정에는 안 쓴다 —
    크레딧 소비는 지금처럼 게이트 FIFO 그대로고, 이 값은 통과 행에 기록만 된다. 그래서
    미태깅으로 판정된 통과에도 UID가 남아 신원 조회 창구가 그 경고를 짚을 수 있다.
    안 실려 오면 예전 거동 그대로 NULL이다(라파 `gate_server.py`는 아직 안 싣는다).

    ⚠ ON CONFLICT로 새 행이 실제로 생겼을 때만 판정·소비·알림으로 넘어간다.
    재시도로 같은 event_id가 또 와도 크레딧을 두 번 먹지 않는다(아키텍처 §05).
    중복이어도 **원래 판정은 그대로 돌려준다** — 예전엔 verdict=null이라 재시도한 기기가
    판정을 못 받았다(실측 P0-4-a).
    """
    settings = get_settings()

    insert_stmt = (
        pg_insert(GatePassEvent)
        .values(
            event_id=event_id,
            device_id=device_id,
            boot_id=boot_id,
            gate_no=gate_no,
            room_id=room_id,
            direction=direction,
            status=status,
            beam_a_ts=beam_a_ts,
            beam_b_ts=beam_b_ts,
            observed_at=observed_at,
            # 기기가 읽은 UID는 판정 앞에서 적재한다. 판정 갈래를 안 타는 사실이라
            # 크레딧을 못 태우고 빠지는 갈래(미태깅·퇴장·보류)에서도 행에 남는다.
            tag_id=tag_id,
            # ⭐ 시연 도구가 만든 행인가(2026-08-05 프론트 22차). 판정에는 한 줄도 안 쓴다 —
            # 시연이라고 다르게 판정하면 리허설이 본 것과 발표에서 볼 것이 갈라진다.
            # 오직 **나중에 골라 지우려고** 남기는 표시다.
            is_demo=is_demo,
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(GatePassEvent.id)
    )
    pass_id = (await session.execute(insert_stmt)).scalar_one_or_none()
    if pass_id is None:
        # 재시도 중복 — 이미 판정된 통과다. 크레딧·알림을 다시 건드리지 않는다.
        await session.rollback()
        return await _describe_duplicate_pass(
            session,
            event_id=event_id,
            incoming={
                "device_id": device_id,
                "boot_id": boot_id,
                "gate_no": gate_no,
                "room_id": room_id,
                "direction": direction,
                "status": status,
                "beam_a_ts": beam_a_ts,
                "beam_b_ts": beam_b_ts,
                "observed_at": observed_at,
            },
        )

    # 판정 창 기준은 빔 A 시각(몸이 게이트에 들어선 순간). 없으면 관측 시각으로 폴백.
    compare_ts = beam_a_ts or observed_at
    server_now = dt.datetime.now(dt.timezone.utc)
    _warn_future_skew("gate_pass", event_id, compare_ts, server_now)
    # 만료 청소 기준은 시계 둘의 합의(이른 쪽)다. 판정 창과 다른 시계라 변수를 따로 든다.
    # 기기 시계가 미래로 튀어도, 인입이 한참 늦어도 유효 크레딧이 안 닫힌다(_expire_overdue).
    sweep_ts = min(_as_utc(compare_ts), server_now)
    # 퇴장 회수 기준은 반대로 늦은 쪽이다. 양쪽 시계가 다 유효로 볼 때만 회수한다(_recover_oldest).
    recover_ts = max(_as_utc(compare_ts), server_now)
    matched: int | None = None
    # 소비한 크레딧의 카드 UID. 소비가 실제로 일어난 갈래에서만 찬다(-241).
    matched_tag_id: str | None = None
    alert_id: int | None = None
    notify_dashboard = False
    notify_chat = False

    beam_conflict = direction_beam_conflict(direction, beam_a_ts, beam_b_ts)

    if status == "incomplete":
        # 한쪽 빔만 끊김 — 방향 판별 불가라 판정 없이 기록만.
        verdict = Verdict.BEAM_INCOMPLETE
    elif beam_conflict is not None:
        # 방향 주장이 같은 이벤트의 빔 시각과 어긋난다 — 정상 통과로 안 친다(-243).
        # **크레딧은 절대 안 태운다.** 못 믿을 주장 하나로 남의 크레딧을 없애면 안 된다.
        #
        # 다만 판정을 무조건 `beam_incomplete`로 접으면 안 된다. 그러면 공격자가 빔 칸만
        # 비우고 들어올 때 Alert도·매터모스트 카드도·팝업도 안 뜨고 untagged 통계에서도
        # 빠져서, 이 수리가 오히려 미태깅 탐지에 구멍을 낸다(수리 1차 검증 P1). 그래서
        # 크레딧이 있나 **읽기만** 해서 두 갈래로 가른다.
        #  - 유효 크레딧이 있다  → 보수 판정(`beam_incomplete`). 태울 뻔한 크레딧을 지키는 게
        #    목적이고, 태깅한 사람이 있는 자리라 미태깅이라 단정할 수 없다.
        #  - 유효 크레딧이 없다  → `untagged`. 지킬 크레딧이 없으니 보수적으로 굴 이유가 없고,
        #    "그 게이트에 아무도 태깅을 안 했는데 통과 사건이 왔다"는 사실은 그대로다.
        #    이 파일의 하드닝 규칙(**모든 미태깅 사건은 Alert 행을 남긴다**)을 여기서도 지킨다.
        # 소비가 아니라 조회라 크레딧 상태는 어느 갈래에서도 안 바뀐다.
        live_credits = await _count_unconsumed(session, gate_no, compare_ts, room_id)
        verdict = Verdict.BEAM_INCOMPLETE if live_credits > 0 else Verdict.UNTAGGED
        logger.warning(
            "방향 주장과 빔 시각이 어긋난다 (%s event_id=%s gate=%s direction=%s"
            " beam_a_ts=%s beam_b_ts=%s) → 판정 %s. 크레딧은 안 태운다 — 기기 빔 배선·"
            "판별식을 확인해라.",
            beam_conflict, event_id, gate_no, direction, beam_a_ts, beam_b_ts, verdict,
        )
        # 만료 청소는 입장·퇴장 경로와 대칭으로 여기서도 밟는다. 청소는 기기 주장이 아니라
        # 시계 둘의 합의(min)로만 도는 일이라, 못 믿을 주장이 왔다고 건너뛸 이유가 없다.
        # 건너뛰면 TTL 지난 크레딧이 issued로 남아 저신뢰 집계·퇴장 회수가 부풀어 보인다.
        await _expire_overdue(session, gate_no, sweep_ts, room_id)
    elif direction == "A_TO_B":
        consumed = await _consume_fifo(session, gate_no, compare_ts, pass_id, room_id)
        if consumed is not None:
            verdict = Verdict.NORMAL
            # 어느 크레딧을 태웠나(행 id)와 그 크레딧의 카드 UID를 같이 받는다(-241).
            matched, matched_tag_id = consumed
        else:
            verdict = Verdict.UNTAGGED
        # 소비 판정 **뒤에** 청소한다. 입장 경로에도 대칭으로 밟아 만료분이 issued로 안 남게
        # 한다 — 안 밟으면 게이트를 지나가는 사람만 있고 나가는 사람이 없는 동안 만료분이
        # 계속 쌓인다. 순서는 판정 먼저 청소 나중을 지킨다(자기 크레딧을 먼저 못 닫게).
        await _expire_overdue(session, gate_no, sweep_ts, room_id)
    elif direction == "B_TO_A":
        # 퇴장은 기록만 남긴다. 만료분 청소는 입장 경로와 대칭으로 여기서도 밟는다.
        await _expire_overdue(session, gate_no, sweep_ts, room_id)
        if EXIT_RECOVERY_ENABLED:
            # [팀 결정 필요] 정본 468행을 되살린 경로. 기본 배치에서는 안 돈다 —
            # 회수가 구조적으로 남의 크레딧만 먹는 근거는 상수 주석에 있다.
            await _recover_oldest(
                session, gate_no, recover_ts, sweep_ts, pass_id, room_id
            )
        verdict = Verdict.EXIT
    else:
        # complete인데 방향이 비어 있음 — 계약에 없는 조합이라 안전하게 판정 보류.
        verdict = Verdict.BEAM_INCOMPLETE

    # 통과 행에 남길 UID. 소비한 크레딧 쪽이 이긴다 — 그게 서버가 직접 만든 사실이고,
    # 기기 관측값은 그 갈래에서 이미 같은 카드라야 정상이다(어긋나면 크레딧 쪽이 정확하다).
    # 소비가 없는 판정(미태깅·퇴장·보류)에서만 기기 관측 UID가 그대로 남는다(F22).
    # 기기가 UID를 안 실어 보내면 둘 다 None이라 예전처럼 NULL이다.
    stored_tag_id = matched_tag_id if matched_tag_id is not None else tag_id
    await session.execute(
        update(GatePassEvent)
        .where(GatePassEvent.id == pass_id)
        .values(verdict=verdict, matched_tagging_event_id=matched, tag_id=stored_tag_id)
    )

    # ── 미태깅 경고: 사건 기록과 외부 알림을 갈라 놓는다 ────────────────────
    # 예전엔 Alert insert 자체가 쿨다운 안에 있어서, 창 안에 들어온 미태깅은 **사건이 통째로
    # 사라졌다**. 실서버에서 미태깅 297건 중 279건이 Alert 없이 남았고(2026-07-30 실측),
    # ToF 고착이 창 하나를 가짜 32~37건으로 채워 진짜 미태깅자가 경고를 받을 확률이 3% 안쪽으로
    # 떨어졌다. Alert 행을 손으로 만드는 API가 없어서 사후 복구도 불가였다.
    #
    # 그래서 규칙을 이렇게 갈랐다.
    #  - **사건(Alert 행)은 무조건 남긴다.** 신원 입력·상위 보고가 이 행을 대상으로 돌아간다.
    #  - **쿨다운은 외부 알림만 억제한다.** 화면 팝업과 매터모스트 카드가 도배되는 건 막는다.
    #  - 채널별로 판정을 따로 든다. 예전엔 불리언 하나라 둘이 한꺼번에 빠졌다.
    #
    # ⚠ 이 수리로 Alert 행 수가 늘어난다(고착 구간에서는 폭증한다). 진짜 해법은 ToF 고착
    # 수리(지라 -227)이고, 화면 쪽에서 "울린 경고"와 "기록만 남은 경고"를 가르려면 Alert에
    # 통지 시각 칸이 필요하다 — 마이그레이션이라 B조 몫으로 올렸다.
    #
    # 쿨다운에 넣는 이벤트 축 시각은 판정과 같은 기준(compare_ts)이다.
    # 벽시계 축은 _in_cooldown 안에서 따로 재서 AND로 묶인다.
    alert_ts = _as_utc(compare_ts)
    # ⭐ 채널 이름에 **누구인가**를 붙여 창을 사람마다 가른다(위 `_who_key`).
    # ⚠ **조건 밖에서 만든다.** 아래 표시·되돌리기가 미태깅이 아닌 갈래에서도 이 이름을
    # 읽는다 — 안에서 만들면 그 갈래가 통째로 죽는다.
    who = _who_key(stored_tag_id)
    dashboard_key = f"{_CHANNEL_DASHBOARD}:{who}"
    chat_key = f"{_CHANNEL_CHAT}:{who}"

    if verdict == Verdict.UNTAGGED:
        alert_id = (
            await session.execute(
                pg_insert(Alert)
                .values(
                    type=ALERT_TYPE_UNTAGGED,
                    severity="high",
                    source_type="gate_pass",
                    source_id=pass_id,
                    # ⭐ 시연 통과가 낳은 경고도 같이 표시된다. 통과만 표시하면
                    # "시연 통과는 지웠는데 그 경고는 남는" 갈래가 생긴다.
                    is_demo=is_demo,
                )
                .returning(Alert.id)
            )
        ).scalar_one()
        notify_dashboard = not _in_cooldown(
            gate_no, room_id, dashboard_key, alert_ts
        )
        notify_chat = not _in_cooldown(gate_no, room_id, chat_key, alert_ts)
        if not (notify_dashboard or notify_chat):
            logger.info(
                "미태깅 사건 기록 — 쿨다운 창 안이라 외부 알림은 생략했다"
                " (alert_id=%s gate=%s event_id=%s)", alert_id, gate_no, event_id,
            )

    # ⭐ 쿨다운 표시는 **검사 직후**에 찍는다. 예전에는 커밋 뒤로 미뤘는데(커밋 실패가
    # 쿨다운만 갱신하는 걸 막으려는 의도였다) 그 의도가 경합 창을 만들었다 — 검사와 표시
    # 사이에 커밋 await가 끼어서 동시에 들어온 미태깅 두 건이 **둘 다** 창을 통과하고,
    # 대시보드 팝업과 매터모스트 카드가 겹쳐 나갔다. 표시를 먼저 찍고, 커밋이 터진 갈래에서만
    # 되돌린다(`_unmark_alert`). 원래 의도는 그 되돌리기가 그대로 지킨다.
    prev_dashboard = _peek_alert_mark(gate_no, room_id, dashboard_key)
    prev_chat = _peek_alert_mark(gate_no, room_id, chat_key)
    if notify_dashboard:
        _mark_alert(gate_no, room_id, dashboard_key, alert_ts)
    if notify_chat:
        _mark_alert(gate_no, room_id, chat_key, alert_ts)

    try:
        unconsumed = await _count_unconsumed(session, gate_no, compare_ts, room_id)
        low_confidence = unconsumed > settings.low_confidence_credit_threshold
        await session.commit()
    except Exception:
        # 알림이 결국 안 나간다 — 예약해 둔 표시를 앞 값으로 되돌린다.
        if notify_dashboard:
            _unmark_alert(gate_no, room_id, dashboard_key, prev_dashboard)
        if notify_chat:
            _unmark_alert(gate_no, room_id, chat_key, prev_chat)
        raise

    return PassEvaluation(
        stored=True,
        pass_id=pass_id,
        verdict=verdict,
        matched_tagging_event_id=matched,
        tag_id=stored_tag_id,
        alert_id=alert_id,
        low_confidence=low_confidence,
        notify_dashboard=notify_dashboard,
        notify_chat=notify_chat,
        beam_conflict=beam_conflict,
    )


async def _describe_duplicate_pass(
    session: AsyncSession, *, event_id: str, incoming: dict
) -> PassEvaluation:
    """이미 저장돼 있던 통과 행을 읽어 중복 응답을 만든다.

    두 가지를 한다.
    1. **원래 판정을 돌려준다.** 예전엔 verdict=null이라, 재전송한 기기·CLI가 DB엔 멀쩡히
       있는 판정을 못 받았다(실측 P0-4-a).
    2. **본문이 어긋나면 그 칸 이름을 실어 준다.** 호출자가 409로 거절한다. 예전엔 다른
       사건을 같은 event_id로 보내도 200 duplicate로 조용히 버렸다(실측 P0-4-b).

    ⚠ `result`(기기 1차 판단)는 DB에 안 남는 참고 칸이라 비교에서 뺀다 — 넣으면 비교 대상이
    없어서 정상 재전송이 전부 409가 된다.

    ⚠ `tag_id`도 비교에서 뺀다(F22). DB에 남기는 하는데, 저장 값이 **기기가 보낸 값이 아니라
    소비한 크레딧의 UID일 수 있어서**(`stored_tag_id`) 정상 재전송이 통째로 409가 된다.
    """
    row = (
        await session.execute(select(GatePassEvent).where(GatePassEvent.event_id == event_id))
    ).scalars().one_or_none()
    if row is None:
        # 경합 — 딴 트랜잭션이 넣는 중이라 아직 안 보인다. 중복이라는 사실만 알린다.
        logger.warning("중복 판정인데 저장된 통과 행을 못 찾았다 (event_id=%s)", event_id)
        return PassEvaluation(
            stored=False, pass_id=None, verdict=None,
            matched_tagging_event_id=None, alert_id=None, low_confidence=False,
        )

    diffs = conflicting_fields(row, incoming)
    if diffs:
        logger.warning(
            "같은 event_id에 다른 본문이 왔다 — 거절한다 (event_id=%s 어긋난 칸=%s)",
            event_id, diffs,
        )
    return PassEvaluation(
        stored=False,
        pass_id=row.id,
        verdict=row.verdict,
        matched_tagging_event_id=row.matched_tagging_event_id,
        # UID도 저장 행에서 다시 뽑는다. 라파 sender가 2xx까지 무한 재전송이라 운영에서
        # 실제로 보이는 응답 대부분이 재전송분이다.
        # ⚠ 지금은 이 값을 읽는 자리가 없다 — WS는 `if ev.stored:` 안에서만 나가고
        #   GatePassIngestAck에도 tag_id 칸이 없다(2026-08-01 교차 검증 실측). 그래도
        #   비우지 않는 이유는 결과 객체가 저장 행과 같은 그림이어야 나중에 응답이나
        #   중복 갈래 알림에 UID를 실을 때 조용히 null이 새지 않기 때문이다.
        tag_id=row.tag_id,
        alert_id=None,
        low_confidence=False,
        conflicting_fields=diffs,
        # 어긋난 사유도 저장 행에서 다시 뽑는다. 라파 sender는 2xx가 나올 때까지 무한
        # 재전송하는 구조라(`MAX_FUTURE_SKEW_SEC` 주석) 운영에서 실제로 보이는 응답은
        # 대부분 이 재전송분이다. 여기서 None을 돌려주면 사유 칸이 사실상 늘 비어 보인다.
        beam_conflict=direction_beam_conflict(row.direction, row.beam_a_ts, row.beam_b_ts)
        if row.status != "incomplete" else None,
    )
