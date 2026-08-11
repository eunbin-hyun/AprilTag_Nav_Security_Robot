"""시험 자료 정리 도구 — `tools/purge_test_data.py`.

파괴 도구라 안전선을 하나씩 실물로 눌러 본다.

- 미리보기가 기본값이고, 미리보기에서는 한 행도 안 지운다.
- 범위 옵션이 없으면 exit 2로 멈춘다(전부 지우기를 실수로 부르는 걸 막는다).
- 백업 파일이 실제로 쓰이고, 줄 수가 지울 행 수와 다르면 삭제를 안 한다.
- 지워질 이벤트를 가리키는 경고를 따라 지운다(아무것도 안 가리키는 경고가 남으면 안 된다).
- 되돌리기가 원래 id 그대로 살리고, 두 번 돌려도 같은 결과다(멱등).
- 시험 DB가 아니면 확인 문구를 요구하고, 입력이 터미널이 아니면 거절한다.

⚠ 이 파일은 async 시험이 아니다. 도구가 `main()` 안에서 `asyncio.run()`을 부르므로 세션
이벤트 루프 위에서 부르면 "asyncio.run() cannot be called from a running event loop"로
죽는다(test_migrations_roundtrip.py와 같은 함정이라 같은 방식으로 피한다).
"""
import asyncio
import contextlib
import datetime as dt
import os
import pathlib

import asyncpg
import pytest

from app.config import get_settings
from tools import purge_test_data as tool

DSN = tool.to_asyncpg_dsn(get_settings().database_url)
BASE = dt.datetime(2026, 7, 25, 0, 0, 0, tzinfo=dt.timezone.utc)


@contextlib.contextmanager
def _preserve_event_loop_pointer():
    """asyncio.run()이 지우는 '현재 이벤트 루프' 포인터를 되돌린다."""
    try:
        saved = asyncio.get_event_loop()
    except RuntimeError:
        saved = None
    try:
        yield
    finally:
        asyncio.set_event_loop(saved)


async def _sql(statements: list[str]) -> None:
    conn = await asyncpg.connect(DSN)
    try:
        for s in statements:
            await conn.execute(s)
    finally:
        await conn.close()


async def _counts() -> dict[str, int]:
    conn = await asyncpg.connect(DSN)
    try:
        return {
            t: await conn.fetchval(f"SELECT count(*) FROM {t}")
            for t in tool.PURGE_ORDER
        }
    finally:
        await conn.close()


async def _ids(table: str) -> list[int]:
    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(f"SELECT id FROM {table} ORDER BY id")
        return [r["id"] for r in rows]
    finally:
        await conn.close()


def _iso(day: int, hour: int = 0) -> str:
    return (BASE + dt.timedelta(days=day, hours=hour)).isoformat()


# 시험 자료 — 7/25 통과 2건(옛것)과 7/29 통과 1건(새것), 옛 통과를 가리키는 경고 1건.
def _seed() -> list[str]:
    return [
        "INSERT INTO gate_pass_event (id, event_id, device_id, gate_no, direction, status,"
        " observed_at, verdict) VALUES"
        f" (1, 'test-gate1-1-1', 'test-gate1', 1, 'A_TO_B', 'complete', '{_iso(0)}', 'untagged'),"
        f" (2, 'gate1-2-1', 'gate1', 1, 'A_TO_B', 'complete', '{_iso(0, 1)}', 'untagged'),"
        f" (3, 'gate1-3-1', 'gate1', 9, 'A_TO_B', 'complete', '{_iso(4)}', 'normal')",
        "INSERT INTO tagging_event (id, event_id, device_id, gate_no, tag_id, observed_at)"
        f" VALUES (1, 'test-gate1-9-1', 'test-gate1', 1, 'u1', '{_iso(0)}')",
        "INSERT INTO shuttle_arrival (id, event_id, gate_no, shuttle_no, signal_ts)"
        f" VALUES (1, 'webcall-1-babcdef01-5-1', 1, '1호차', '{_iso(0)}')",
        # 경고 2건 — id 1은 옛 통과(id 1)를 가리키고, id 2는 새 통과(id 3)를 가리킨다.
        "INSERT INTO alert (id, type, severity, source_type, source_id, ack, created_at)"
        f" VALUES (1, 'untagged', 'high', 'gate_pass', 1, false, '{_iso(0)}'),"
        f" (2, 'untagged', 'high', 'gate_pass', 3, false, '{_iso(4)}')",
    ]


@pytest.fixture
def seeded():
    _sync(_sql(_seed()))
    yield


@pytest.fixture
def backup_dir(tmp_path) -> pathlib.Path:
    return tmp_path / "backups"


def _run(argv: list[str]) -> int:
    with _preserve_event_loop_pointer():
        return tool.main(["--database-url", get_settings().database_url, *argv])


def _sync(coro):
    """헬퍼 코루틴을 돌린다. asyncio.run이 지우는 루프 포인터를 늘 되돌린다.

    포인터를 안 되돌리면 이 파일 뒤에 오는 async 시험이 "There is no current event loop"로
    죽는다(test_migrations_roundtrip.py가 실측으로 잡은 함정).
    """
    with _preserve_event_loop_pointer():
        return asyncio.run(coro)


# ── 순수 판정 ──────────────────────────────────────────────────────────────

def test_시험_DB_판정():
    assert tool.is_test_database("c207_test") is True
    assert tool.is_test_database("c207_test_gw0") is True
    assert tool.is_test_database("c207") is False, "운영 DB를 시험 DB로 봤다"
    assert tool.is_test_database("c207_prod") is False


def test_범위_옵션_판정():
    p = tool.build_parser()
    assert tool.has_scope(p.parse_args([])) is False
    assert tool.has_scope(p.parse_args(["--before", "2026-07-30"])) is True
    assert tool.has_scope(p.parse_args(["--device-prefix", "test-"])) is True
    assert tool.has_scope(p.parse_args(["--gate", "1"])) is True
    assert tool.has_scope(p.parse_args(["--all"])) is True
    # --apply만으로는 범위가 아니다 — 이게 뚫리면 전부 지우기가 실수로 돌아간다.
    assert tool.has_scope(p.parse_args(["--apply"])) is False


def test_시각은_오프셋_없으면_UTC로_읽는다():
    """로컬로 읽으면 같은 명령이 기계 시간대마다 다른 걸 지운다."""
    assert tool._iso_datetime("2026-07-30").tzinfo == dt.timezone.utc
    parsed = tool._iso_datetime("2026-07-30T09:00:00+09:00")
    assert parsed.utcoffset() == dt.timedelta(hours=9)


def test_기기_접두_범위는_경고를_직접_안_고른다():
    """경고엔 device_id·gate_no가 없다. 기기로 좁힐 땐 따라 지우기에만 맡긴다."""
    args = tool.build_parser().parse_args(["--device-prefix", "test-"])
    where, params = tool.build_where("alert", args)
    assert "false" in where
    where, params = tool.build_where("gate_pass_event", args)
    assert "device_id LIKE" in where
    assert params == ["test-%"]


def test_셔틀은_event_id_접두로_가른다():
    """shuttle_arrival엔 device_id 칸이 없다."""
    args = tool.build_parser().parse_args(["--device-prefix", "webcall-"])
    where, params = tool.build_where("shuttle_arrival", args)
    assert "event_id LIKE" in where
    assert params == ["webcall-%"]


def test_범위를_여러_개_주면_교집합이다():
    """OR로 묶으면 좁히려고 준 옵션이 대상을 늘린다 — 파괴 도구에선 위험하다."""
    args = tool.build_parser().parse_args(
        ["--before", "2026-07-30", "--gate", "1"]
    )
    where, _ = tool.build_where("gate_pass_event", args)
    assert " AND " in where and " OR " not in where


# ── 안전선 ─────────────────────────────────────────────────────────────────

def test_범위_없이_부르면_아무것도_안_지운다(seeded):
    before = _sync(_counts())
    assert _run(["--apply"]) == 2
    assert _sync(_counts()) == before


def test_미리보기가_기본값이다(seeded, capsys):
    before = _sync(_counts())
    assert _run(["--before", _iso(2)]) == 0
    out = capsys.readouterr().out
    assert "미리보기" in out
    assert _sync(_counts()) == before, "미리보기가 행을 지웠다"


def test_시험_DB가_아니면_확인_문구를_요구한다(seeded, monkeypatch, backup_dir):
    """입력이 터미널이 아니면 거절한다 — echo로 지날 수 있으면 게이트가 있으나 마나다."""
    monkeypatch.setattr(tool, "is_test_database", lambda name: False)
    monkeypatch.setattr(tool.sys.stdin, "isatty", lambda: False)
    before = _sync(_counts())
    assert _run(["--before", _iso(2), "--apply", "--backup-dir", str(backup_dir)]) == 3
    assert _sync(_counts()) == before
    assert not backup_dir.exists(), "확인 전에 백업을 떴다"


def test_확인_문구가_틀리면_안_지운다(seeded, monkeypatch, backup_dir):
    monkeypatch.setattr(tool, "is_test_database", lambda name: False)
    monkeypatch.setattr(tool.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "yes")
    before = _sync(_counts())
    assert _run(["--before", _iso(2), "--apply", "--backup-dir", str(backup_dir)]) == 3
    assert _sync(_counts()) == before


def test_확인_문구를_그대로_입력하면_지운다(seeded, monkeypatch, backup_dir):
    monkeypatch.setattr(tool, "is_test_database", lambda name: False)
    monkeypatch.setattr(tool.sys.stdin, "isatty", lambda: True)
    expected = tool.CONFIRM_TEMPLATE.format(db=tool.db_name_of(DSN))
    monkeypatch.setattr("builtins.input", lambda *a: expected)
    assert _run(["--before", _iso(2), "--apply", "--backup-dir", str(backup_dir)]) == 0
    assert _sync(_ids("gate_pass_event")) == [3]


def test_백업_줄_수가_안_맞으면_삭제를_안_한다(seeded, monkeypatch, backup_dir):
    """백업이 반만 쓰인 채로 지우면 되돌릴 방법이 사라진다."""
    monkeypatch.setattr(tool, "write_backup", lambda path, doomed: 0)
    before = _sync(_counts())
    assert _run(["--before", _iso(2), "--apply", "--backup-dir", str(backup_dir)]) == 4
    assert _sync(_counts()) == before


def test_백업은_덮지_않고_0600으로_만든다(backup_dir):
    """이름이 부딪히면 조용히 덮는 대신 선다. 되돌릴 길이 이 파일 하나뿐이다."""
    path = backup_dir / "purge_x.jsonl"
    assert tool.write_backup(path, {"alert": [{"id": 1}]}) == 1
    with pytest.raises(FileExistsError):
        tool.write_backup(path, {"alert": [{"id": 2}]})
    assert path.read_text(encoding="utf-8").count("\n") == 1, "앞 백업이 덮였다"
    if os.name != "nt":  # 윈도우는 mode가 사실상 안 먹는다
        assert path.stat().st_mode & 0o777 == 0o600


def test_백업_이름이_부딪히면_삭제를_안_한다(seeded, monkeypatch, backup_dir):
    def _collide(path, doomed):
        raise FileExistsError(path)

    monkeypatch.setattr(tool, "write_backup", _collide)
    before = _sync(_counts())
    assert _run(["--before", _iso(2), "--apply", "--backup-dir", str(backup_dir)]) == 4
    assert _sync(_counts()) == before


def test_기본_백업_자리는_배포_폴더_밖이다():
    """`$APP_DIR` 안에 두면 다음 배포의 `rsync --delete`가 백업을 통째로 지운다."""
    assert tool.BACKEND_DIR not in tool.DEFAULT_BACKUP_DIR.parents
    assert tool.DEFAULT_BACKUP_DIR.parent == pathlib.Path.home()


# ── 실제 삭제 ──────────────────────────────────────────────────────────────

def test_기간_범위로_지우고_남은_수를_보고한다(seeded, backup_dir, capsys):
    assert _run(["--before", _iso(2), "--apply", "--backup-dir", str(backup_dir)]) == 0
    out = capsys.readouterr().out
    assert "남은 행" in out

    # 7/25 것만 지워지고 7/29 통과(id 3)는 남는다.
    assert _sync(_ids("gate_pass_event")) == [3]
    assert _sync(_ids("tagging_event")) == []
    assert _sync(_ids("shuttle_arrival")) == []
    # 경고 id 1(옛 통과를 가리킴)만 지워지고 id 2는 남는다.
    assert _sync(_ids("alert")) == [2]


def test_기기_접두로_지우면_실기기_행은_남는다(seeded, backup_dir):
    assert _run(
        ["--device-prefix", "test-", "--apply", "--backup-dir", str(backup_dir)]
    ) == 0
    # test-gate1 것만 지워진다(통과 id 1, 태깅 id 1).
    assert _sync(_ids("gate_pass_event")) == [2, 3]
    assert _sync(_ids("tagging_event")) == []
    # 셔틀은 webcall- 접두라 test-로는 안 걸린다.
    assert _sync(_ids("shuttle_arrival")) == [1]
    # 지워진 통과 id 1을 가리키던 경고는 따라 지워진다.
    assert _sync(_ids("alert")) == [2]


def test_게이트_범위로_지운다(seeded, backup_dir):
    assert _run(["--gate", "9", "--apply", "--backup-dir", str(backup_dir)]) == 0
    assert _sync(_ids("gate_pass_event")) == [1, 2]
    # 게이트 9 통과(id 3)를 가리키던 경고 id 2가 따라 지워진다.
    assert _sync(_ids("alert")) == [1]


def test_all은_전부_지운다(seeded, backup_dir):
    assert _run(["--all", "--apply", "--backup-dir", str(backup_dir)]) == 0
    assert _sync(_counts()) == {t: 0 for t in tool.PURGE_ORDER}


def test_대상이_없으면_조용히_끝난다(backup_dir):
    assert _run(["--before", _iso(-5), "--apply", "--backup-dir", str(backup_dir)]) == 0


def test_지우는_사이_새_경고가_들어오면_통째로_되돌린다(seeded, monkeypatch, backup_dir):
    """조회 뒤에 들어온 경고가 지워질 이벤트를 가리키면 삭제를 통째로 되돌린다.

    그 경고는 백업에 없어서 같이 지우면 되돌릴 길이 없고, 두고 지우면 아무것도 안 가리키는
    경고가 남는다. 그래서 지우지 말고 되돌린다(종료 코드 5).
    """
    real = tool.collect_orphan_alert_ids
    calls = {"n": 0}

    async def _second_call_finds_new_alert(conn, doomed):
        calls["n"] += 1
        if calls["n"] == 1:
            return await real(conn, doomed)
        return [999]  # 삭제 트랜잭션 안 재조회 — 그 사이 새 경고가 들어왔다고 친다

    monkeypatch.setattr(tool, "collect_orphan_alert_ids", _second_call_finds_new_alert)
    before = {t: _sync(_ids(t)) for t in tool.PURGE_ORDER}
    assert _run(["--all", "--apply", "--backup-dir", str(backup_dir)]) == 5
    assert {t: _sync(_ids(t)) for t in tool.PURGE_ORDER} == before, "롤백이 안 됐다"


# ── 되돌리기 ───────────────────────────────────────────────────────────────

def _only_backup(backup_dir: pathlib.Path) -> pathlib.Path:
    files = sorted(backup_dir.glob("purge_*.jsonl"))
    assert len(files) == 1, files
    return files[0]


def test_되돌리기가_원래_id로_살린다(seeded, backup_dir):
    before = {t: _sync(_ids(t)) for t in tool.PURGE_ORDER}
    assert _run(["--all", "--apply", "--backup-dir", str(backup_dir)]) == 0
    assert _sync(_counts()) == {t: 0 for t in tool.PURGE_ORDER}

    assert _run(["--restore", str(_only_backup(backup_dir))]) == 0
    after = {t: _sync(_ids(t)) for t in tool.PURGE_ORDER}
    assert after == before, "되돌린 id가 원래와 다르다(경고 역추적이 깨진다)"


def test_되돌린_뒤_새_행이_중복_키로_안_터진다(seeded, backup_dir):
    """원래 id를 그대로 넣으니 시퀀스를 최대 id로 맞춰야 한다."""
    assert _run(["--all", "--apply", "--backup-dir", str(backup_dir)]) == 0
    assert _run(["--restore", str(_only_backup(backup_dir))]) == 0
    # id를 안 주고 새 행을 넣는다. 시퀀스가 안 밀렸으면 여기서 중복 키로 터진다.
    _sync(
        _sql([
            "INSERT INTO gate_pass_event (event_id, device_id, gate_no, observed_at)"
            f" VALUES ('gate1-after-1', 'gate1', 1, '{_iso(9)}')"
        ])
    )
    ids = _sync(_ids("gate_pass_event"))
    assert ids[-1] > 3


def test_되돌리기를_두_번_해도_같다(seeded, backup_dir, capsys):
    assert _run(["--all", "--apply", "--backup-dir", str(backup_dir)]) == 0
    path = str(_only_backup(backup_dir))
    assert _run(["--restore", path]) == 0
    first = {t: _sync(_ids(t)) for t in tool.PURGE_ORDER}
    assert _run(["--restore", path]) == 0
    assert {t: _sync(_ids(t)) for t in tool.PURGE_ORDER} == first
    assert "건너뛴 행" in capsys.readouterr().out


def test_없는_백업_파일이면_exit_2(backup_dir):
    assert _run(["--restore", str(backup_dir / "없는파일.jsonl")]) == 2


def test_수명주기_칸도_백업에_담긴다(seeded, backup_dir):
    """0005가 붙인 칸이 백업·되돌리기에서 빠지면 종결 이력이 사라진다."""
    _sync(
        _sql([
            "UPDATE alert SET status = 'RESOLVED', ack = true,"
            " resolution = 'false_positive', resolved_by = '요원A',"
            f" resolved_at = '{_iso(1)}', acked_at = '{_iso(1)}' WHERE id = 1"
        ])
    )
    assert _run(["--all", "--apply", "--backup-dir", str(backup_dir)]) == 0
    dumped = tool.read_backup(_only_backup(backup_dir))
    row = [r for r in dumped["alert"] if r["id"] == 1][0]
    assert row["status"] == "RESOLVED"
    assert row["resolution"] == "false_positive"

    assert _run(["--restore", str(_only_backup(backup_dir))]) == 0
    conn_rows = _sync(_alert_status(1))
    assert conn_rows == ("RESOLVED", "false_positive", True)


# ── 분류 닫힘 · 남겨야 하는 표 (2026-08-04) ─────────────────────────────────
#
# ⭐ 여기가 이 파일에서 제일 중요한 못이다. 위 시험들은 "지울 것이 지워지나"를 재는데,
# 파괴 도구에서 진짜 비싼 실패는 반대쪽이다 — **안 지워야 할 것이 지워지는 것.**
#
# ⚠ 그래서 아래 목록을 `tool.NEVER_PURGED`에서 읽지 않고 **손으로 박는다.** 도구가 들고 있는
# 목록을 그대로 되읽으면, 누가 `app_user`를 NEVER_PURGED에서 빼는 순간 시험은 "그 표는 원래
# 안 지키기로 한 것"으로 보고 조용히 통과한다 — 검증기가 스스로 답을 채우는 자리다.
# 실측으로 확인했다(2026-08-04): `app_user`를 PURGE_ORDER로 옮기는 변이를 넣었더니 닫힘
# 시험은 그대로 통과했다. 지켜야 할 표 목록은 도구 밖에 따로 있어야 그 변이가 잡힌다.
MUST_SURVIVE = ("app_user", "auth_session", "staff_audit", "staff")


def test_표_분류가_닫힌_집합이다():
    """모델의 모든 표가 세 목록 중 정확히 하나에 있다.

    손으로 적은 표 목록은 조용히 샌다. 같은 저장소에서 이미 났다 — `tests/conftest.py`의
    `_TRUNCATE_TABLES`에서 표 넷이 빠져 케이스 사이로 행이 샜고, 그래서 거기에
    `_assert_truncate_list_is_closed`가 붙었다. 이 시험은 그 잣대를 정리 도구에 그대로 건다.

    새 표를 모델에 붙이고 여기 안 적으면 이 시험이 터진다. 그 자리에서 "이건 지울 표인가
    남길 표인가"를 정하게 만드는 게 목적이다 — 잊으면 지워야 할 자국이 발표 화면에 남거나,
    더 나쁘게는 남겨야 할 표가 `--all`에 딸려 간다.
    """
    from app.models import Base

    known = set(Base.metadata.tables)
    purge = set(tool.PURGE_ORDER)
    keep = set(tool.NEVER_PURGED)
    skip = set(tool.OUT_OF_SCOPE)

    classified = purge | keep | skip
    assert not (known - classified), (
        f"어느 목록에도 없는 표가 있다: {sorted(known - classified)}. "
        "지울 표면 PURGE_ORDER, 남길 표면 NEVER_PURGED, 빈 뼈대면 OUT_OF_SCOPE에 넣어라."
    )
    assert not (classified - known), (
        f"모델에 없는 표가 목록에 있다: {sorted(classified - known)}. 이름이 바뀌었거나 지워졌다."
    )
    # 겹치면 "지우면서 남긴다"는 뜻이라 어느 쪽이 이기는지가 코드 순서에 숨는다.
    assert purge & keep == set(), f"PURGE_ORDER와 NEVER_PURGED가 겹친다: {purge & keep}"
    assert purge & skip == set(), f"PURGE_ORDER와 OUT_OF_SCOPE가 겹친다: {purge & skip}"
    assert keep & skip == set(), f"NEVER_PURGED와 OUT_OF_SCOPE가 겹친다: {keep & skip}"


async def _seed_kept_tables() -> None:
    """남겨야 하는 표에 행을 하나씩 심는다(계정·세션·명부·감사·로봇)."""
    conn = await asyncpg.connect(DSN)
    try:
        uid = await conn.fetchval(
            "INSERT INTO app_user (username, password_hash, display_name, role)"
            " VALUES ('keeper1', 'x', '지킴이', 'admin') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO auth_session (token_hash, user_id, expires_at, last_seen_at)"
            f" VALUES ('hash-keep-1', {uid}, '{_iso(30)}', '{_iso(0)}')"
        )
        await conn.execute(
            "INSERT INTO staff (name, role, tag_id) VALUES ('요원 지킴', 'agent', 'keep-uid-1')"
        )
        await conn.execute(
            "INSERT INTO staff_audit (staff_id, action, after)"
            " VALUES (1, 'create', '{\"x\": 1}'::jsonb)"
        )
        await conn.execute(
            # ⛔ 재료도 계약 낱말로 — `AUTO`·`ONLINE`은 `RobotMode`·`CommStatus`에 없는 값이다.
            # 여기 판정은 "안 지워졌나"라 값이 뭐든 통과하지만, 이 줄을 베껴 쓰는 다음 시험이
            # 계약 밖 재료를 물려받는다.
            "INSERT INTO robot (name, mode, battery, network_status)"
            " VALUES ('jetson01', 'OUTDOOR_TAGGING', 88, 'WS_OK')"
        )
    finally:
        await conn.close()


async def _counts_of(tables) -> dict[str, int]:
    conn = await asyncpg.connect(DSN)
    try:
        return {t: await conn.fetchval(f"SELECT count(*) FROM {t}") for t in tables}
    finally:
        await conn.close()


def test_all이어도_남겨야_하는_표는_안_지운다(seeded, backup_dir):
    """⭐ 제일 중요한 못 — `--all`이 계정·세션·명부·감사·로봇을 건드리면 안 된다.

    계정이 날아가면 시연 계정으로 로그인할 길이 없어지고, 감사 기록은 append-only 계약이다.
    `--all`이라는 이름이 "DB 전부"로 읽히기 쉬운 자리라 실물로 못 박는다.
    """
    # 지켜야 할 표가 지우는 목록에 들어가 있으면 그 자체가 결함이다(행을 세기 전에 본다).
    for table in MUST_SURVIVE:
        assert table not in tool.PURGE_ORDER, f"{table}이(가) 지우는 목록에 있다"

    _sync(_seed_kept_tables())
    kept = MUST_SURVIVE + tool.OUT_OF_SCOPE
    before = _sync(_counts_of(kept))
    assert before["app_user"] == 1 and before["robot"] == 1, before

    assert _run(["--all", "--apply", "--backup-dir", str(backup_dir)]) == 0

    # 지울 표는 비었고
    assert _sync(_counts()) == {t: 0 for t in tool.PURGE_ORDER}
    # 남길 표는 한 행도 안 줄었다
    assert _sync(_counts_of(kept)) == before, "남겨야 하는 표가 지워졌다"


def test_남겨야_하는_표는_백업에도_안_담긴다(seeded, backup_dir):
    """백업에 계정·세션이 담기면 그 JSONL 한 장이 비밀번호 해시 유출 경로가 된다."""
    _sync(_seed_kept_tables())
    assert _run(["--all", "--apply", "--backup-dir", str(backup_dir)]) == 0
    dumped = tool.read_backup(_only_backup(backup_dir))
    for table in MUST_SURVIVE + tool.OUT_OF_SCOPE:
        assert table not in dumped, f"{table}이(가) 백업에 담겼다"


# ── 시퀀스 되돌리기 ─────────────────────────────────────────────────────────


async def _next_id(table: str) -> int:
    """다음 INSERT가 받을 id를 시퀀스에서 **안 건드리고** 읽는다.

    행을 넣었다 지우는 식으로 재면 안 된다 — 그 INSERT 자체가 시퀀스를 밀어서, 재는 행위가
    재려는 값을 바꾼다. `is_called`가 false면 `last_value`를 그대로 준다(1을 안 건너뛴다).
    """
    conn = await asyncpg.connect(DSN)
    try:
        seq = await conn.fetchval("SELECT pg_get_serial_sequence($1, 'id')", table)
        row = await conn.fetchrow(f"SELECT last_value, is_called FROM {seq}")
        return row["last_value"] + 1 if row["is_called"] else row["last_value"]
    finally:
        await conn.close()


async def _set_seq(table: str, value: int) -> None:
    """시퀀스를 실서버처럼 앞세운다.

    `_seed()`는 id를 손으로 박아 넣어서 시퀀스가 안 밀린다. 실서버는 반대로 시퀀스가 id를
    나눠 준 자리라 8101까지 올라가 있다 — 그 상태를 안 만들면 "번호가 커진다"는 문제 자체가
    시험에서 재현되지 않는다(프론트가 실측한 `pass_id` 8101이 이 상황이다).
    """
    conn = await asyncpg.connect(DSN)
    try:
        seq = await conn.fetchval("SELECT pg_get_serial_sequence($1, 'id')", table)
        await conn.execute("SELECT setval($1, $2, true)", seq, value)
    finally:
        await conn.close()


@pytest.fixture
def seq_ahead(seeded):
    """실서버 판 — 통과 시퀀스가 8101까지 올라간 상태."""
    _sync(_set_seq("gate_pass_event", 8101))
    yield


def test_기본은_시퀀스를_안_건드린다(seq_ahead, backup_dir):
    """옵션 없이 지우면 번호는 이어서 커진다 — 되돌리기를 깨지 않는 쪽이 기본값이다.

    지우기만 해서는 시퀀스가 안 내려간다는 걸 못 박는다(이게 프론트가 본 증상의 원인이다).
    """
    assert _run(["--all", "--apply", "--backup-dir", str(backup_dir)]) == 0
    assert _sync(_next_id("gate_pass_event")) == 8102, "지우면 번호도 내려간다고 봤다"


def test_시퀀스를_되돌리면_1번부터_시작한다(seq_ahead, backup_dir):
    """발표 화면에 '8102번 통과'가 안 뜨게 하는 자리다."""
    assert _run(
        ["--all", "--apply", "--reset-sequences", "--backup-dir", str(backup_dir)]
    ) == 0
    assert _sync(_next_id("gate_pass_event")) == 1


def test_행이_남으면_시퀀스를_1로_안_내린다(seq_ahead, backup_dir):
    """⚠ 범위를 좁혀 지우면 행이 남는다. 1로 내리면 다음 INSERT가 중복 키로 터진다."""
    # 7/25 것만 지운다 — 통과 id 3(7/29)이 남는다.
    assert _run(
        ["--before", _iso(2), "--apply", "--reset-sequences", "--backup-dir", str(backup_dir)]
    ) == 0
    assert _sync(_ids("gate_pass_event")) == [3]
    # 남은 최대 id(3) 다음이라야 살아 있는 행을 안 덮는다.
    assert _sync(_next_id("gate_pass_event")) == 4
    # 실물로도 확인 — 새 행이 중복 키로 안 터지고 남은 행을 안 덮는다.
    _sync(
        _sql([
            "INSERT INTO gate_pass_event (event_id, device_id, gate_no, observed_at)"
            f" VALUES ('after-reset-1', 'gate1', 1, '{_iso(9)}')"
        ])
    )
    assert _sync(_ids("gate_pass_event")) == [3, 4]


def test_시퀀스_되돌리기는_남은_행을_안_건드린다(seq_ahead, backup_dir):
    """번호만 만지는 옵션이라 행 수가 달라지면 안 된다."""
    assert _run(
        ["--before", _iso(2), "--apply", "--reset-sequences", "--backup-dir", str(backup_dir)]
    ) == 0
    assert _sync(_ids("gate_pass_event")) == [3]
    assert _sync(_ids("alert")) == [2]


def test_시퀀스_되돌리기는_미리보기에서_안_돈다(seq_ahead):
    """미리보기는 아무것도 안 바꾼다 — 번호도 그대로여야 한다."""
    assert _run(["--all", "--reset-sequences"]) == 0
    assert _sync(_next_id("gate_pass_event")) == 8102


async def _alert_status(alert_id: int) -> tuple:
    conn = await asyncpg.connect(DSN)
    try:
        row = await conn.fetchrow(
            "SELECT status, resolution, ack FROM alert WHERE id = $1", alert_id
        )
        return (row["status"], row["resolution"], row["ack"])
    finally:
        await conn.close()
