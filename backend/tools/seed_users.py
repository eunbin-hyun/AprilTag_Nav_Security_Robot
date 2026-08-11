"""로그인 계정 심기 (지라 S15P11C207-349, 설계 §6.2 2단계).

롤아웃 순서에서 **켜기 직전에 한 번** 도는 자리다. 계정 창구(`POST /api/users`)는 관리자
세션을 요구하므로 첫 계정은 창구로 못 만든다 — 그 닭·달걀을 푸는 스크립트다.

    1) AUTH_REQUIRE_LOGIN=false로 배포 + 마이그레이션
    2) ⭐ 이 스크립트로 계정 심기          ← 여기
    3) 프론트 배포
    4) AUTH_REQUIRE_LOGIN=true로 켜기

## 쓰는 법 (EC2 컨테이너 안이나 로컬 venv)

    # 계정 하나
    python -m tools.seed_users --username sonseuk --display-name 손세욱 --role admin

    # 비밀번호를 인자로 안 주면 물어본다(입력이 화면에 안 찍힌다 — 권장)
    # 자동화면 표준입력으로도 받는다
    echo '비밀번호' | python -m tools.seed_users --username agent1 --display-name 아무개 --role agent --password-stdin

    # 이미 있는 계정이면 기본은 건드리지 않는다(멱등). 비밀번호·역할을 다시 정하려면
    python -m tools.seed_users --username sonseuk --display-name 손세욱 --role admin --update

    # 지금 계정 목록만 보기(비밀번호 안 물어본다)
    python -m tools.seed_users --list

⚠ **비밀번호를 명령줄 인자로 받지 않는다.** 셸 히스토리와 프로세스 목록(`ps`)에 원문이
남는 자리라서다. 물어보기(getpass)나 표준입력 둘 중 하나다.

⚠ **마지막 관리자 검사는 여기서 안 돈다.** 창구(`PATCH /api/users/{id}`)는
`auth.assert_not_last_admin`으로 관리자 0명을 막는데 이 도구는 그 검사를 안 지난다 —
유일한 관리자를 `--update --role agent`로 내리면 창구로는 못 되돌리는 잠금이 난다(되돌리는
길은 이 도구를 한 번 더 돌리는 것뿐이다). 잠금 사고에서 빠져나오는 유일한 길이 이 도구라
일부러 열어 두는 것인지, 창구와 같게 막을 것인지는 팀 결정 자리다(2026-08-02 백지 검토 3차
X21 — `tests/test_seed_users.py`가 그 갈래를 실제로 밟고 있어 손대지 않았다).

⚠ `--update`가 한 일은 `staff_audit`에 한 줄로 남는다(행위자 칸은 전부 NULL, `after.via`가
`seed_users`다). 창구와 같은 조작을 하는 자리라 기록도 같은 표에 있어야 이력이 안 끊긴다.

⚠ 계정 6개는 사용자 확정이다 — 관리자 2 · 팀장 1 · 개발자 1(관리자 역할) · 요원 2.
아이디는 영문 소문자+숫자, 화면·감사 기록에는 한글 실명이 찍힌다(설계 §8 확정 A·B).
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app import auth
from app.db import get_session
from app.models import AppUser
from app.routers.account import account_snapshot
from app.routers.staff import ACCOUNT_AUDIT_STAFF_ID, ACTION_UPDATE, add_audit

# ⭐ 08-08 — trainee(교육생)와 owner(총괄)를 더했다. owner가 빠져 있으면 사다리 도입 뒤
#   새 환경에서 관리자 계정 관리가 통째로 잠긴다 — owner만 admin·owner를 만들 수 있어서다
#   (실서버에는 owner가 이미 있다 · 2026-08-08 실측). 교육생은 명부 출입자용 최저 급이다.
ROLES = (auth.ROLE_TRAINEE, auth.ROLE_AGENT, auth.ROLE_LEADER, auth.ROLE_ADMIN, auth.ROLE_OWNER)


def _read_password(args: argparse.Namespace) -> str:
    """비밀번호를 표준입력이나 물어보기로 받는다. 인자로는 절대 안 받는다(모듈 머리 ⚠)."""
    if args.password_stdin:
        password = sys.stdin.readline().strip()
    else:
        password = getpass.getpass("비밀번호: ")
        again = getpass.getpass("한 번 더: ")
        if password != again:
            raise SystemExit("두 번 입력한 비밀번호가 다릅니다.")
    if len(password) < 8:
        raise SystemExit("비밀번호는 8자 이상이어야 합니다.")
    return password


async def _list_users() -> None:
    async with get_session() as session:
        rows = (
            (await session.execute(select(AppUser).order_by(AppUser.id))).scalars().all()
        )
    if not rows:
        print("계정이 하나도 없습니다.")
        return
    print(f"계정 {len(rows)}개")
    for row in rows:
        state = "활성" if row.is_active else "해제"
        print(f"  #{row.id} {row.username} · {row.display_name} · {row.role} · {state}")


async def _seed(args: argparse.Namespace) -> None:
    username = auth.normalize_username(args.username)
    if not auth.USERNAME_RE.match(username):
        raise SystemExit("아이디는 영문 소문자와 숫자 3~20자여야 합니다.")
    if args.role not in ROLES:
        raise SystemExit(f"역할은 {' · '.join(ROLES)} 중 하나여야 합니다.")

    async with get_session() as session:
        existing = (
            await session.execute(select(AppUser).where(AppUser.username == username))
        ).scalar_one_or_none()

        if existing is not None and not args.update:
            # 멱등 — 여러 번 돌려도 안전해야 배포 스크립트에 넣을 수 있다.
            print(f"이미 있는 계정입니다: {username} ({existing.role}). 그대로 둡니다.")
            print("비밀번호·역할을 다시 정하려면 --update를 붙이세요.")
            return

        password = _read_password(args)
        password_hash = auth.hash_password(password)

        if existing is None:
            session.add(
                AppUser(
                    username=username,
                    password_hash=password_hash,
                    display_name=args.display_name,
                    role=args.role,
                    is_active=True,
                )
            )
            await session.commit()
            print(f"계정을 만들었습니다: {username} · {args.display_name} · {args.role}")
            return

        # ⭐ 감사 사본은 **바꾸기 전에** 뜬다(2026-08-02 백지 검토 3차 X21).
        before = account_snapshot(existing)

        existing.password_hash = password_hash
        existing.display_name = args.display_name
        existing.role = args.role
        existing.is_active = True

        # 이 갈래는 창구(`PATCH /api/users/{id}`)와 같은 일을 하는데 기록이 한 줄도 안 남았다.
        # 남의 비밀번호를 갈고 역할까지 바꾸는 조작이라, 그게 안 남으면 "누가 언제 이 계정을
        # 열었나"가 어디에도 없다. 행위자 칸은 전부 NULL이다 — 창구가 아니라 서버에서 손으로
        # 돌린 자리라는 사실 자체가 기록이다(add_audit docstring과 같은 잣대).
        add_audit(
            session,
            staff_id=ACCOUNT_AUDIT_STAFF_ID,
            action=ACTION_UPDATE,
            actor=None,
            before=before,
            after={
                **account_snapshot(existing),
                "password_reset": True,
                "via": "seed_users",
            },
        )

        # 비밀번호가 바뀌면 살아 있던 세션은 끊는다 — 옛 비밀번호로 연 창이 계속 살아 있으면
        # 다시 정한 뜻이 없다(auth.revoke_user_sessions docstring과 같은 자리).
        #
        # `commit=False`를 **명시로** 넘긴다. 해시 교체·감사 행·세션 무효화 셋이 한 커밋에
        # 실려야 그 사이에서 죽었을 때 갈라짐이 안 생긴다(`account.reset_password`와 같은 판).
        await auth.revoke_user_sessions(session, existing.id, commit=False)
        await session.commit()
        print(f"계정을 갱신했습니다: {username} · {args.display_name} · {args.role}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="C207 로그인 계정을 심습니다(설계 §6.2 2단계).",
    )
    parser.add_argument("--username", help="영문 소문자+숫자 3~20자")
    parser.add_argument("--display-name", help="화면·감사 기록에 찍히는 한글 실명")
    parser.add_argument("--role", choices=ROLES, help=" · ".join(ROLES))
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="비밀번호를 표준입력에서 한 줄로 읽습니다(자동화용).",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="이미 있는 계정의 비밀번호·역할을 다시 정합니다(세션도 끊습니다).",
    )
    parser.add_argument("--list", action="store_true", help="계정 목록만 보여줍니다.")
    args = parser.parse_args()

    if args.list:
        asyncio.run(_list_users())
        return
    if not (args.username and args.display_name and args.role):
        parser.error("--username · --display-name · --role을 모두 주세요(또는 --list).")
    asyncio.run(_seed(args))


if __name__ == "__main__":
    main()
