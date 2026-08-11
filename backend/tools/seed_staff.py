"""출입 명부(`staff`)를 심는다. 계정용 `seed_users.py`의 짝이다.

## 왜 필요한가

2026-08-05에 사용자가 "명부에 아직 아무도 없는데?"라고 짚었다. 확인해 보니 계정(`app_user`)은
여섯인데 명부(`staff`)가 **0행**이었다. 명부를 채우는 길이 `POST /api/staff` 창구뿐이라
**팀장급 세션으로 로그인해 여섯 번 눌러야** 했고, 자동화할 자리가 없었다.

명부가 비면 이런 것이 안 돈다.

- 통과 이벤트에 **카드 주인 이름·학번**이 안 실린다(`EventOut.staff_name`). 화면이 전부
  "명부 밖 카드"로 그린다.
- `GET /api/staff/by-tag/{tag_id}` 신원 조회가 늘 빈손이다.
- 발표에서 명부 탭이 빈 화면이다.

⚠ **미태깅 감지 자체는 명부와 무관하다.** 그쪽은 크레딧 상태머신이 판정한다
(`app/credit/state_machine.py`). 명부는 "그 카드가 누구 것이냐"만 맡는다.

## 쓰는 법

    # 계정 여섯을 그대로 명부로 옮긴다(이름·직급·계정 링크까지)
    python tools/seed_staff.py --from-accounts

    # 한 사람만
    python tools/seed_staff.py --name 손세욱 --rank admin --student-no 1512345

    # 카드를 대서 UID를 알아낸 뒤 채운다
    python tools/seed_staff.py --name 손세욱 --tag-id AABBCCDD

    # 지금 명부를 본다(아무것도 안 바꾼다)
    python tools/seed_staff.py --list

## ⛔ 안전선

- **이미 있는 이름은 안 건드린다.** 같은 이름이 있으면 건너뛰고 그 사실을 찍는다. 덮어쓰기가
  필요하면 화면(`PATCH /api/staff/{id}`)에서 한다 — 그쪽은 감사 기록이 남는다.
- **태그 UID는 중복을 막는다.** 이미 다른 사람이 쓰는 UID면 거절한다(창구 `_assert_tag_free`와
  같은 규칙).
- ⚠ **이 도구는 감사 기록(`staff_audit`)을 안 남긴다.** 창구를 안 지나기 때문이다. 운영 중
  변경은 화면에서 하고, 이 도구는 **처음 심을 때만** 쓴다(`seed_users.py`와 같은 결).
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

# ⚠ `python tools/seed_staff.py`로 부르면 `sys.path[0]`이 **`tools/`**가 되어 `app`을 못 찾는다
#    (`python -m tools.seed_staff`로 부르면 cwd가 첫 자리라 문제가 없다). 부르는 방식에 따라
#    되고 안 되는 도구는 급할 때 사람을 붙잡으므로 여기서 backend 폴더를 직접 얹는다.
#
# ⛔ **거기에 진짜 `app` 패키지가 있을 때만 얹는다.** 이 검사가 없으면 파일을 딴 데 두고
#    돌릴 때(운영 컨테이너가 read_only라 `/tmp`에 놓고 `docker run -v`로 태우는 길이 그렇다)
#    `/`가 `sys.path` 첫 자리에 꽂혀 `/app`을 `app` 패키지로 잘못 잡고
#    `ImportError: cannot import name 'auth' from 'app' (unknown location)`으로 죽는다.
#    2026-08-05에 실서버에서 실제로 밟았다.
#
# ⚠ `__file__`이 없는 판도 있다 — `docker exec -i ... python - < seed_staff.py`로 **stdin**에서
#    돌리는 길이다. 그때는 cwd가 이미 첫 자리라 얹을 필요가 없으므로 조용히 건너뛴다.
if "__file__" in globals():
    _backend_dir = pathlib.Path(__file__).resolve().parent.parent
    if (_backend_dir / "app" / "__init__.py").exists():
        sys.path.insert(0, str(_backend_dir))

from sqlalchemy import select  # noqa: E402

from app import auth  # noqa: E402
from app.db import get_session  # noqa: E402
from app.models import AppUser, Staff  # noqa: E402

# ⭐ 08-08 — trainee(교육생)·owner(총괄)를 더했다. 명부 rank는 RoleName enum과 같은 사다리라
#   (schemas.RoleName) 여기만 셋으로 두면 교육생·총괄 출입자를 도구로 못 심는다.
RANKS = (
    auth.ROLE_TRAINEE, auth.ROLE_AGENT, auth.ROLE_LEADER, auth.ROLE_ADMIN, auth.ROLE_OWNER,
)


async def _list_staff() -> None:
    async with get_session() as session:
        rows = (await session.execute(select(Staff).order_by(Staff.id))).scalars().all()
    if not rows:
        print("명부가 비어 있습니다.")
        return
    print(f"명부 {len(rows)}명")
    for row in rows:
        tag = row.tag_id or "-"
        # ⚠ 태그 UID는 앞 네 글자만 찍는다. 전체를 콘솔에 남기면 그 값으로 카드를 흉내 낼 수 있다.
        tag_shown = f"{tag[:4]}…" if row.tag_id else "-"
        print(
            f"  #{row.id:<3} {row.name:<10} 직급={row.rank or '-':<8} "
            f"태그={tag_shown:<8} 계정={row.app_user_id or '-'} 활성={row.is_active}"
        )


async def _seed_one(
    session, *, name: str, rank: str | None, tag_id: str | None,
    student_no: str | None, app_user_id: int | None,
) -> str:
    """한 명을 심는다. 무슨 일이 있었는지를 한 줄로 돌려준다."""
    exists = (
        await session.execute(select(Staff).where(Staff.name == name))
    ).scalars().first()
    if exists is not None:
        return f"건너뜀 {name} — 이미 명부에 있습니다(#{exists.id})"

    if tag_id:
        taken = (
            await session.execute(select(Staff).where(Staff.tag_id == tag_id))
        ).scalars().first()
        if taken is not None:
            return f"거절 {name} — 그 태그를 {taken.name}(#{taken.id})가 이미 씁니다"

    session.add(
        Staff(
            name=name,
            rank=rank,
            tag_id=tag_id,
            student_no=student_no,
            app_user_id=app_user_id,
            is_active=True,
        )
    )
    await session.flush()
    return f"심음 {name} — 직급={rank or '-'} 태그={'있음' if tag_id else '없음'}"


async def _seed_from_accounts() -> None:
    """계정 여섯을 그대로 명부로 옮긴다.

    ⭐ 계정의 `display_name`을 명부 이름으로, `role`을 직급으로, `id`를 계정 링크로 쓴다.
    태그 UID와 학번은 안 채운다 — **UID는 카드를 대야 알고 학번은 사람이 넣을 값이다.**
    나중에 화면에서 `PATCH /api/staff/{id}`로 채우면 된다.
    """
    async with get_session() as session:
        users = (
            await session.execute(select(AppUser).order_by(AppUser.id))
        ).scalars().all()
        if not users:
            print("계정이 하나도 없습니다. 먼저 tools/seed_users.py 로 계정을 심으세요.")
            return
        print(f"계정 {len(users)}개를 명부로 옮깁니다.")
        for user in users:
            said = await _seed_one(
                session,
                name=user.display_name or user.username,
                rank=user.role,
                tag_id=None,
                student_no=None,
                app_user_id=user.id,
            )
            print(f"  {said}")
        await session.commit()
    print("끝났습니다. 태그 UID는 카드를 댄 뒤 화면에서 채우세요.")


async def _seed_manual(args: argparse.Namespace) -> None:
    async with get_session() as session:
        said = await _seed_one(
            session,
            name=args.name,
            rank=args.rank,
            tag_id=args.tag_id,
            student_no=args.student_no,
            app_user_id=args.app_user_id,
        )
        print(said)
        await session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="출입 명부(staff)를 심습니다. 처음 채울 때만 쓰고, 운영 중 변경은 화면에서 하세요."
    )
    parser.add_argument(
        "--from-accounts",
        action="store_true",
        help="계정(app_user) 전부를 이름·직급·계정링크까지 그대로 명부로 옮깁니다.",
    )
    parser.add_argument("--name", help="명부에 적을 이름(필수 칸은 이것 하나입니다)")
    parser.add_argument("--rank", choices=RANKS, help=" · ".join(RANKS))
    parser.add_argument("--tag-id", help="카드 UID. 카드를 대서 알아낸 값을 넣습니다")
    parser.add_argument("--student-no", help="학번")
    parser.add_argument("--app-user-id", type=int, help="이을 계정 id")
    parser.add_argument("--list", action="store_true", help="지금 명부만 보여줍니다(안 바꿉니다).")
    args = parser.parse_args()

    if args.list:
        asyncio.run(_list_staff())
        return
    if args.from_accounts:
        asyncio.run(_seed_from_accounts())
        return
    if not args.name:
        parser.error("--name 이 필요합니다(또는 --from-accounts · --list).")
    asyncio.run(_seed_manual(args))


if __name__ == "__main__":
    sys.exit(main())
