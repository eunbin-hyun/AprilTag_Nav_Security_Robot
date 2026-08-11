"""정지 존 판정 — 순수 파이썬. 외부 의존 없음.

카메라·모델을 안 물고도 시험할 수 있게 기하 계산과 상태기계만 뒀다.
"""

from __future__ import annotations

from typing import Iterable, List, Tuple

from .config import GuardConfig
from .types import CLEAR, SLOW, STOP, Detection, PersonHit


def intersection_area(
    box: Tuple[float, float, float, float],
    zone: Tuple[float, float, float, float],
) -> float:
    """두 사각형이 겹친 넓이."""
    x1 = max(box[0], zone[0])
    y1 = max(box[1], zone[1])
    x2 = min(box[2], zone[2])
    y2 = min(box[3], zone[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    return (x2 - x1) * (y2 - y1)


def classify_level(conf: float, config: GuardConfig) -> str:
    """confidence 3단 임계."""
    if conf >= config.stop_conf:
        return "stop"
    if conf >= config.warn_conf:
        return "warn"
    return "ignore"


def judge_frame(
    detections: Iterable[Detection],
    width: int,
    height: int,
    config: GuardConfig,
) -> Tuple[str, List[PersonHit]]:
    """프레임 하나를 판정해서 (결정, 사람별 판정) 을 돌려준다.

    정지 존과 `overlap_ratio` 이상 겹친 사람 중,
    confidence 가 stop_conf 이상이면 STOP, warn 구간이면 SLOW 다.

    ## ⛔ 코앞의 사람이 구조적으로 STOP 이 안 나던 결함 (2026-08-09 백지검토가 잡음)

    예전에는 겹친 넓이를 **박스 넓이로만** 나눴다. 그러면 비율의 상한이
    `존 넓이 / 박스 넓이` 라서, 박스가 존보다 크면 아무리 정확히 겹쳐도 1.0 이 안 된다.
    기본 존은 화면의 0.21(가로 0.6 × 세로 0.35)이고 임계가 0.25 라,
    **박스가 화면의 84% 를 넘으면 위치와 무관하게 in_zone 이 절대 참이 안 됐다.**

        직접 실행 — GuardConfig() 기본값 · Detection(bbox=(0,0,640,480), conf=0.95)
          고치기 전  overlap_ratio 0.2100 · in_zone False · CLEAR
          (20,20,620,470) 도 0.2247 로 CLEAR

    ⛔ 하필 **로봇에 제일 가까운 사람이 제일 큰 박스**다. 가장 위험한 입력이 통째로
    CLEAR 로 빠졌다.

    ⭐ 그래서 겹침 비율을 **둘 중 작은 넓이 기준**으로 잰다(겹침 계수).
    `max(교집합/박스, 교집합/존)` 과 같은 값이고, 이렇게 하면
    "박스가 존을 다 덮었다" 도 "박스가 존 안에 다 들어왔다" 와 똑같이 1.0 이 된다.

    ⚠ 사람 발끝 좌표가 존 안이면 무조건 참으로 보는 길도 있었는데 **안 썼다.**
    기존 시험 `test_겹침이_임계_아래면_통과`(존 위 경계를 살짝만 문 박스)가 그 방식이면
    STOP 으로 뒤집힌다 — 계약을 바꾸지 않고 결함만 없애는 쪽을 골랐다.
    """
    zone_px = config.zone.to_pixels(width, height)
    zone_area = max(0.0, zone_px[2] - zone_px[0]) * max(0.0, zone_px[3] - zone_px[1])
    hits: List[PersonHit] = []
    decision = CLEAR

    for det in detections:
        level = classify_level(det.conf, config)
        area = det.area
        # 분모가 0 이면(넓이 0 박스·찌그러진 존) 나눗셈을 아예 안 한다.
        denom = min(area, zone_area) if area > 0 and zone_area > 0 else 0.0
        ratio = 0.0 if denom <= 0 else intersection_area(det.bbox, zone_px) / denom
        in_zone = ratio >= config.overlap_ratio
        hits.append(
            PersonHit(
                bbox=tuple(float(v) for v in det.bbox),
                conf=float(det.conf),
                overlap_ratio=ratio,
                in_zone=in_zone,
                level=level,
            )
        )
        if in_zone:
            if level == "stop":
                decision = STOP
            elif level == "warn" and decision != STOP:
                decision = SLOW

    return decision, hits


class StopSignalTracker:
    """프레임 사이 튐을 눌러주는 상태기계.

    - CLEAR 에서 STOP 으로 가려면 STOP 판정이 `stop_frames` 번 연속 나야 한다.
    - STOP 에서 풀리려면 STOP 아닌 판정이 `release_frames` 번 연속 나야 한다.
    - SLOW 는 즉시 반영한다 (감속은 늦게 걸리면 의미가 없다).

    이미지 한 장을 볼 땐 쓸 일이 없고, 영상·카메라 입력에서만 의미가 있다.
    """

    def __init__(self, config: GuardConfig) -> None:
        self._config = config
        self._state = CLEAR
        self._stop_streak = 0
        self._clear_streak = 0

    @property
    def state(self) -> str:
        return self._state

    def update(self, raw_decision: str) -> str:
        if raw_decision == STOP:
            self._stop_streak += 1
            self._clear_streak = 0
        else:
            self._stop_streak = 0
            self._clear_streak += 1

        if self._state == STOP:
            if self._clear_streak >= self._config.release_frames:
                self._state = SLOW if raw_decision == SLOW else CLEAR
        else:
            if self._stop_streak >= self._config.stop_frames:
                self._state = STOP
            else:
                self._state = SLOW if raw_decision == SLOW else CLEAR
        return self._state

    def reset(self) -> None:
        self._state = CLEAR
        self._stop_streak = 0
        self._clear_streak = 0
