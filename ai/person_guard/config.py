"""person_guard 설정값.

정지 존 기하(normalized 0~1)와 confidence 3단 임계, 디바운스 프레임 수를
한 곳에 모았다. 젯슨에서 카메라 화각이 바뀌면 이 값만 갈면 된다.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict


@dataclass(frozen=True)
class ZoneConfig:
    """정지 존 사각형. 프레임 크기 대비 비율(0~1)로 적는다.

    기본값은 "프레임 하단 35%, 가로 중앙 60%" — 로봇 진행 방향 바로 앞이다.
    좌표계는 이미지와 같아서 y=0이 위, y=1이 아래다.
    """

    x_min: float = 0.20
    x_max: float = 0.80
    y_min: float = 0.65
    y_max: float = 1.00

    def __post_init__(self) -> None:
        for name in ("x_min", "x_max", "y_min", "y_max"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"zone.{name} 는 0~1 사이여야 합니다: {value}")
        if self.x_min >= self.x_max:
            raise ValueError("zone.x_min 은 x_max 보다 작아야 합니다")
        if self.y_min >= self.y_max:
            raise ValueError("zone.y_min 은 y_max 보다 작아야 합니다")

    def to_pixels(self, width: int, height: int) -> tuple:
        """비율 사각형을 픽셀 사각형 (x1, y1, x2, y2)으로 바꾼다."""
        return (
            self.x_min * width,
            self.y_min * height,
            self.x_max * width,
            self.y_max * height,
        )


@dataclass(frozen=True)
class GuardConfig:
    """추론 + 판정 전체 설정."""

    weights: str = "yolov8n.pt"
    device: str = "0"
    imgsz: int = 640

    # confidence 3단 (조사 정본 §③ 항목 3과 같은 설계)
    #   conf >= stop_conf            -> 확정, 정지
    #   warn_conf <= conf < stop_conf -> 애매, 감속
    #   conf <  warn_conf            -> 무시 (검출로 안 침)
    stop_conf: float = 0.60
    warn_conf: float = 0.35

    # 사람 박스가 정지 존과 이 비율 이상 겹치면 "존 안"으로 본다.
    # 박스 넓이 기준이라 멀리 있는 작은 사람도 같은 잣대로 잡힌다.
    overlap_ratio: float = 0.25

    # 영상 입력용 디바운스. 한 프레임 튀는 오탐으로 로봇이 멈추지 않게 한다.
    stop_frames: int = 2
    release_frames: int = 3

    zone: ZoneConfig = field(default_factory=ZoneConfig)

    def __post_init__(self) -> None:
        if not 0.0 < self.warn_conf <= self.stop_conf <= 1.0:
            raise ValueError("0 < warn_conf <= stop_conf <= 1 이어야 합니다")
        if not 0.0 < self.overlap_ratio <= 1.0:
            raise ValueError("overlap_ratio 는 0 초과 1 이하여야 합니다")
        if self.stop_frames < 1 or self.release_frames < 1:
            raise ValueError("stop_frames·release_frames 는 1 이상이어야 합니다")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GuardConfig":
        data = dict(data or {})
        zone = data.pop("zone", None)
        known = {f for f in cls.__dataclass_fields__ if f != "zone"}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"모르는 설정 항목입니다: {sorted(unknown)}")
        kwargs: Dict[str, Any] = {k: v for k, v in data.items()}
        if zone is not None:
            zone_unknown = set(zone) - set(ZoneConfig.__dataclass_fields__)
            if zone_unknown:
                raise ValueError(f"모르는 zone 항목입니다: {sorted(zone_unknown)}")
            kwargs["zone"] = ZoneConfig(**zone)
        return cls(**kwargs)

    @classmethod
    def from_json(cls, path: str | Path) -> "GuardConfig":
        with open(path, "r", encoding="utf-8") as fp:
            return cls.from_dict(json.load(fp))
