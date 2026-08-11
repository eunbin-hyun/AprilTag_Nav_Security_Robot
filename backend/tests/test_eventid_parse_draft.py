"""[초안·팀 확정 대기] 안건② event_id 파서 단위 시험. DB도 앱도 안 띄운다."""
import pytest
from pydantic import ValidationError

from app.eventid import format_event_id, parse_event_id
from app.schemas import EventIdIn

UNIX_MS = 1785294000000


# 1. 예전 3토막 형식을 그대로 읽는다.
def test_parse_legacy_format():
    parts = parse_event_id(f"raspberry01-{UNIX_MS}-7")
    assert parts is not None
    assert parts.device_id == "raspberry01"
    assert parts.boot_id is None
    assert parts.legacy is True
    assert (parts.unix_ms, parts.seq) == (UNIX_MS, 7)


# 2. device_id에 하이픈이 섞여도 boot 자리를 정확히 집는다(안 B 표식의 값어치).
def test_parse_boot_format_with_hyphenated_device():
    eid = format_event_id("gate-a-raspberry01", "1a2f3c4d", UNIX_MS, 1)
    assert eid == f"gate-a-raspberry01-b1a2f3c4d-{UNIX_MS}-1"
    parts = parse_event_id(eid)
    assert parts is not None
    assert parts.device_id == "gate-a-raspberry01"
    assert parts.boot_id == "1a2f3c4d"
    assert parts.legacy is False


# 3. boot처럼 생겼어도 표식 `b`가 없으면 device_id 꼬리로 본다(오인식 방지).
def test_parse_unmarked_hex_stays_in_device_id():
    parts = parse_event_id(f"gate-deadbeef-{UNIX_MS}-1")
    assert parts is not None
    assert parts.device_id == "gate-deadbeef"
    assert parts.boot_id is None


# 4. 형식이 낯설면 None을 돌려주고 예외를 안 던진다(인입을 막지 않는다).
def test_parse_unknown_shape_returns_none():
    assert parse_event_id("자유형식키") is None
    assert parse_event_id("raspberry01-notanumber-1") is None


# 4-b. 회귀 — 유니코드 숫자는 str.isdigit()이 참이라도 None이다.
# 예전 코드는 위첨자에서 int()가 ValueError를 던져, 플래그가 다 꺼진 기본값에서도
# 인입이 422로 떨어졌다(약속은 "형식이 안 맞으면 None"이다).
def test_parse_unicode_digits_return_none_without_raising():
    assert parse_event_id("raspberry01-1785294000000-²") is None   # 위첨자 2
    assert parse_event_id("raspberry01-³-1") is None               # 위첨자 3
    assert parse_event_id("raspberry01-٣٤٥-1") is None   # 아랍-인도 숫자
    assert parse_event_id("raspberry01-１２-1") is None         # 전각 숫자


# 4-c. 회귀 — 인입 계약도 그 event_id를 막지 않는다(422가 아니라 그대로 통과).
def test_ingest_schema_accepts_unicode_digit_event_id():
    body = EventIdIn(event_id="raspberry01-1785294000000-²")
    assert body.boot_id is None


# 4-d. 본문 boot_id는 빈 값·공백만 있으면 422로 막는다(형식 최소 검증).
def test_body_boot_id_rejects_blank():
    for bad in ("", "   ", "\t"):
        with pytest.raises(ValidationError):
            EventIdIn(event_id=f"raspberry01-{UNIX_MS}-1", boot_id=bad)


# 4-e. boot_id 대문자도 받아 소문자로 모은다(대소문자는 회의에서 확정, 초안은 양쪽 수용).
def test_uppercase_boot_id_is_normalized():
    parts = parse_event_id(f"raspberry01-b1A2F3C4D-{UNIX_MS}-1")
    assert parts is not None
    assert parts.boot_id == "1a2f3c4d"
    assert EventIdIn(event_id=f"raspberry01-b1A2F3C4D-{UNIX_MS}-1").boot_id == "1a2f3c4d"
    assert (
        EventIdIn(event_id=f"raspberry01-{UNIX_MS}-1", boot_id="AB12CD34").boot_id
        == "ab12cd34"
    )
    # 형식기도 같은 자리로 모은다(파서랑 왕복이 어긋나지 않게).
    assert format_event_id("raspberry01", "1A2F3C4D", UNIX_MS, 1).endswith(
        f"-b1a2f3c4d-{UNIX_MS}-1"
    )


# 5. 형식기는 boot_id 유무로 두 형식을 갈라 만든다.
def test_format_round_trip():
    for boot in (None, "1a2f3c4d"):
        eid = format_event_id("raspberry01", boot, UNIX_MS, 42)
        parts = parse_event_id(eid)
        assert parts is not None
        assert (parts.device_id, parts.boot_id, parts.unix_ms, parts.seq) == (
            "raspberry01", boot, UNIX_MS, 42,
        )
