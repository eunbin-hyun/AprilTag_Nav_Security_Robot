"""사건 수명주기 순수 규칙 — 순위·정규화·전이 함수 (DB·HTTP 없이).

규칙 정본은 app/alert_lifecycle.py 모듈 머리에 있다. 여기는 그 규칙을 함수 단위로 못박는
자리다. HTTP를 타는 계약 시험은 test_alert_lifecycle.py에 있다.

이 파일에 pytestmark(asyncio)를 안 붙인다 — 전부 sync 케이스라 붙이면 pytest가 "async가
아닌데 asyncio 마크가 달렸다"고 경고한다.
"""
from __future__ import annotations

import datetime as dt

from app.alert_lifecycle import (
    AlertResolution,
    AlertStatus,
    apply_transition,
    demote_after_identity_cleared,
    is_forward,
    normalize_status,
    rank,
)


# ── 순수 규칙 (DB·HTTP 없이) ────────────────────────────────────────────────

def test_상태_순위는_전진만_인정한다():
    assert rank("OPEN") < rank("ACKED") < rank("IDENTIFIED") < rank("RESOLVED")
    assert is_forward("OPEN", "RESOLVED") is True
    assert is_forward("ACKED", "ACKED") is False, "같은 단계는 전이가 아니다(멱등)"
    assert is_forward("RESOLVED", "ACKED") is False


def test_모르는_상태값은_OPEN으로_읽는다():
    """마이그레이션을 안 거친 사본 DB나 손으로 넣은 행이 목록 API를 500으로 만들면 안 된다."""
    assert normalize_status(None) == "OPEN"
    assert normalize_status("") == "OPEN"
    assert normalize_status("낯선값") == "OPEN"
    assert normalize_status("RESOLVED") == "RESOLVED"


class _Row:
    """apply_transition이 만지는 칸만 든 가짜 행."""

    def __init__(self, status: str = "OPEN", ack: bool = False) -> None:
        self.status = status
        self.ack = ack
        self.acked_at = None
        self.acked_by = None
        self.resolved_at = None
        self.resolved_by = None
        self.resolution = None


def test_건너뛴_종결은_관제_확인_기록을_안_찍는다():
    """관제가 안 본 채로 일괄 종결한 건과 확인까지 한 건은 다른 사실이다."""
    row = _Row()
    assert apply_transition(row, to="RESOLVED", actor="요원A", resolution="false_positive")
    assert row.status == "RESOLVED"
    assert row.ack is False, "종결이 관제 확인까지 대신 찍었다"
    assert row.acked_at is None
    assert (row.resolved_at is not None, row.resolved_by) == (True, "요원A")
    assert row.resolution == "false_positive"


def test_확인_전이는_ack와_시각을_찍는다():
    row = _Row()
    assert apply_transition(row, to="ACKED", actor="요원A")
    assert (row.status, row.ack, row.acked_by) == ("ACKED", True, "요원A")
    assert row.acked_at is not None


def test_뒤_단계여도_확인_기록은_찍힌다():
    """신원이 적힌 경고를 관제가 확인 누른 건 실제로 일어난 일이다.

    status는 전진 전용이라 안 움직이는데, 그것 때문에 ack까지 버리면 "확인 버튼을
    눌렀는데 아무 일도 안 일어나는" 화면이 된다.
    """
    row = _Row("IDENTIFIED")
    assert apply_transition(row, to="ACKED") is True
    assert row.status == "IDENTIFIED", "status가 뒤로 밀렸다"
    assert row.ack is True and row.acked_at is not None

    # 이미 확인한 뒤 단계 경고면 바뀔 게 없다(멱등).
    assert apply_transition(row, to="ACKED") is False


def test_후퇴_요청은_status를_안_건드린다():
    row = _Row("RESOLVED", ack=True)
    assert apply_transition(row, to="ACKED") is False
    assert row.status == "RESOLVED"


def test_신원_삭제_강등은_관제_확인_증거를_따른다():
    """확인을 눌렀던 경고는 ACKED로, 안 눌렀던 경고는 OPEN으로 내려온다."""
    acked = _Row("IDENTIFIED", ack=True)
    acked.acked_at = dt.datetime(2026, 7, 30, tzinfo=dt.timezone.utc)
    assert demote_after_identity_cleared(acked) is True
    assert acked.status == "ACKED"

    never_acked = _Row("IDENTIFIED")
    assert demote_after_identity_cleared(never_acked) is True
    assert never_acked.status == "OPEN", "확인 안 한 경고가 ACKED로 올라갔다"

    resolved = _Row("RESOLVED", ack=True)
    assert demote_after_identity_cleared(resolved) is False, "닫힌 사건을 되열면 안 된다"

    open_row = _Row("OPEN")
    assert demote_after_identity_cleared(open_row) is False
    assert open_row.status == "OPEN"


def test_전이_사유_낱말이_닫혀_있다():
    """자유 문자열이면 요원이 적은 문장이 인증 없는 목록으로 나가고 집계 축이 흔들린다."""
    assert {r.value for r in AlertResolution} == {
        "confirmed", "false_positive", "duplicate", "other"
    }
    assert {s.value for s in AlertStatus} == {
        "OPEN", "ACKED", "IDENTIFIED", "RESOLVED"
    }
