"""person_guard — 주행 중 사람 감지 → 안전 정지 판정 뼈대.

사전학습 YOLO 의 person 클래스만 쓰므로 추가 학습이 필요 없다.
자세한 설명과 젯슨 배포 시 바꿀 점은 같은 폴더 README.md 에 있다.
"""

from .config import GuardConfig, ZoneConfig
from .types import CLEAR, SLOW, STOP, Detection, FrameResult, PersonHit
from .zone import StopSignalTracker, classify_level, intersection_area, judge_frame

__all__ = [
    "GuardConfig",
    "ZoneConfig",
    "Detection",
    "PersonHit",
    "FrameResult",
    "STOP",
    "SLOW",
    "CLEAR",
    "judge_frame",
    "classify_level",
    "intersection_area",
    "StopSignalTracker",
]

__version__ = "0.1.0"
