"""인입 필수 칸 계약이 정본 JSON과 서버 모델 사이에서 안 갈리게 못박는다.

왜 생겼나 — 정본 `schemas/detection_event.schema.json`이 `required`로
`["event_id","kind","observed_at"]`을 적어 뒀는데 서버는 그 셋을 요구한 적이 없었다.
`kind`는 어느 In 모델에도 칸이 없어서 실어 보내면 pydantic 기본(extra=ignore)이 조용히
버렸고, 셔틀 인입은 `observed_at` 대신 `signal_ts`를 써서 정본만 보고 만든 생산자가
422를 맞는데 문서로는 원인을 못 짚었다(전체검토 2026-08-04 L11).

2026-08-04 사용자 확정 — **느슨한 쪽으로 맞춘다.** 모델을 조이면 라파이가 지금 보내는
프레임이 막혀 시연이 깨지니, 정본 쪽을 실측값에 맞추고 "안 받기로 한 칸"을 사유와 함께
정본에 적었다. 이 파일은 그 뒤로 한쪽만 바뀌는 드리프트를 잡는 자리다.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.schemas import GatePassEventIn, ShuttleArrivalIn, TaggingEventIn

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "detection_event.schema.json"

# 정본의 x-required-by-endpoint 키 → 그 창구가 쓰는 pydantic 모델.
ENDPOINT_MODELS = {
    "POST /api/tagging-events": TaggingEventIn,
    "POST /api/gate-pass-events": GatePassEventIn,
    "POST /api/shuttle-arrivals": ShuttleArrivalIn,
}


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _required_fields(model) -> set[str]:
    return {name for name, f in model.model_fields.items() if f.is_required()}


def test_정본_창구별_필수칸이_pydantic_모델과_같다():
    """정본 목록과 코드가 갈리면 여기서 깨진다. 모델을 고치면 정본도 같이 고쳐라."""
    declared = _schema()["x-required-by-endpoint"]
    for endpoint, model in ENDPOINT_MODELS.items():
        assert set(declared[endpoint]) == _required_fields(model), endpoint


def test_창구_목록이_닫혀있다():
    """정본에 창구가 늘었는데 이 시험이 안 따라가면 새 창구는 대조 없이 지나간다."""
    declared = {k for k in _schema()["x-required-by-endpoint"] if not k.startswith("_")}
    assert declared == set(ENDPOINT_MODELS)


def test_최상위_required는_세_창구_공통칸뿐이다():
    """한 창구에만 필요한 칸을 최상위 required로 올리면 나머지 두 창구가 거짓으로 막힌다."""
    common = set.intersection(*(_required_fields(m) for m in ENDPOINT_MODELS.values()))
    assert set(_schema()["required"]) == common == {"event_id"}


def test_안_받기로_한_칸은_모델에_없고_사유가_적혀있다():
    """`kind`는 창구 URL로 갈리니 본문으로 안 받는다. 그냥 지우면 다음 사람이 되돌린다."""
    schema = _schema()
    for model in ENDPOINT_MODELS.values():
        assert "kind" not in model.model_fields
    assert "observed_at" not in ShuttleArrivalIn.model_fields  # 그 자리는 signal_ts다
    not_accepted = schema["x-not-accepted"]
    assert "kind" in not_accepted and "다시 볼 자리" in not_accepted["kind"]
    assert any("observed_at" in k for k in not_accepted)


def test_정본이_기존_기기_프레임을_안_막는다():
    """additionalProperties를 걸면 지금 라파이가 보내는 여분 칸이 통째로 막힌다."""
    assert "additionalProperties" not in _schema()
