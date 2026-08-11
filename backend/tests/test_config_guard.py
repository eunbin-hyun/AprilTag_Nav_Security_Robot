"""설정 가드 (교차검증 4번).

SHUTTLE_DESTINATION은 command.schema.json의 cmd_destination enum 안에서만 받는다. 오타가
설정에 들어가면 기동 시점에 터져야 한다 — 로봇이 해석 못 하는 목적지를 조용히 내려보내는
쪽이 더 나쁘다.
"""
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas import Destination


def test_unknown_shuttle_destination_fails_startup(monkeypatch):
    monkeypatch.setenv("SHUTTLE_DESTINATION", "MOON_BASE")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_contract_shuttle_destination_is_accepted(monkeypatch):
    monkeypatch.setenv("SHUTTLE_DESTINATION", "INDOOR_TAGGING")
    assert Settings(_env_file=None).shuttle_destination is Destination.INDOOR_TAGGING
