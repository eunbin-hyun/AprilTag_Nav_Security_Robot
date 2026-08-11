"""모듈 안에서 오가는 자료 구조.

검출기·판정기·출력기가 이 구조만 주고받아서, YOLO를 TensorRT 엔진으로
바꿔도 판정 로직은 그대로 쓸 수 있다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 판정 결과 세 가지
STOP = "STOP"    # 정지 신호
SLOW = "SLOW"    # 감속·재확인 (confidence 애매 구간)
CLEAR = "CLEAR"  # 진행 가능


@dataclass
class Detection:
    """검출기가 낸 person 박스 하나. 좌표는 픽셀 (x1, y1, x2, y2)."""

    bbox: Tuple[float, float, float, float]
    conf: float

    @property
    def area(self) -> float:
        x1, y1, x2, y2 = self.bbox
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)


@dataclass
class PersonHit:
    """한 사람에 대한 판정 결과."""

    bbox: Tuple[float, float, float, float]
    conf: float
    overlap_ratio: float  # 박스 넓이 대비 정지 존과 겹친 비율
    in_zone: bool
    level: str  # "stop" | "warn" | "ignore"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["bbox"] = [round(v, 1) for v in self.bbox]
        d["conf"] = round(self.conf, 4)
        d["overlap_ratio"] = round(self.overlap_ratio, 4)
        return d


@dataclass
class FrameResult:
    """프레임 하나의 판정 결과."""

    frame_index: int
    decision: str
    raw_decision: str  # 디바운스 전 판정 (튀는 값 확인용)
    persons: List[PersonHit] = field(default_factory=list)
    timestamp_ms: Optional[float] = None
    inference_ms: Optional[float] = None

    @property
    def trigger_count(self) -> int:
        return sum(1 for p in self.persons if p.in_zone and p.level == "stop")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "timestamp_ms": None if self.timestamp_ms is None else round(self.timestamp_ms, 1),
            "decision": self.decision,
            "raw_decision": self.raw_decision,
            "trigger_count": self.trigger_count,
            "inference_ms": None if self.inference_ms is None else round(self.inference_ms, 2),
            "persons": [p.to_dict() for p in self.persons],
        }
