"""이미지·영상을 받아 정지 신호까지 내는 파이프라인."""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import GuardConfig
from .detector import PersonDetector
from .types import CLEAR, SLOW, STOP, FrameResult
from .zone import StopSignalTracker, judge_frame


class PersonGuard:
    """검출 → 정지 존 판정 → 결과 묶기.

    detector 를 밖에서 넣을 수 있게 열어뒀다. 젯슨에서 TensorRT 엔진으로 갈 때
    같은 인터페이스(detect → (검출목록, 밀리초, (가로,세로)))만 지키면 이 클래스는 그대로다.
    """

    def __init__(self, config: GuardConfig, detector: Optional[Any] = None) -> None:
        self.config = config
        self.detector = detector or PersonDetector(
            weights=config.weights,
            device=config.device,
            imgsz=config.imgsz,
            min_conf=config.warn_conf,
        )
        self._tracker = StopSignalTracker(config)

    # ------------------------------------------------------------------ 프레임

    def process_frame(
        self,
        frame,
        frame_index: int = 0,
        timestamp_ms: Optional[float] = None,
        debounce: bool = True,
    ) -> FrameResult:
        detections, inference_ms, (width, height) = self.detector.detect(frame)
        raw_decision, persons = judge_frame(detections, width, height, self.config)
        decision = self._tracker.update(raw_decision) if debounce else raw_decision
        return FrameResult(
            frame_index=frame_index,
            decision=decision,
            raw_decision=raw_decision,
            persons=persons,
            timestamp_ms=timestamp_ms,
            inference_ms=inference_ms,
        )

    # ------------------------------------------------------------------ 이미지

    def run_image(self, path: str) -> Dict[str, Any]:
        self._tracker.reset()
        result = self.process_frame(path, frame_index=0, debounce=False)
        return self._report("image", path, [result])

    # ------------------------------------------------------------------ 영상

    def run_video(
        self,
        path: str,
        stride: int = 1,
        max_frames: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._tracker.reset()
        results = list(self.iter_video(path, stride=stride, max_frames=max_frames))
        return self._report("video", path, results)

    def iter_video(
        self,
        path: str,
        stride: int = 1,
        max_frames: Optional[int] = None,
    ) -> Iterator[FrameResult]:
        """영상을 프레임 단위로 흘려보낸다. 실시간 배선의 자리표다."""
        try:
            import cv2  # noqa: PLC0415  (의도한 지연 import)
        except ImportError as exc:  # pragma: no cover - 환경 의존
            raise RuntimeError(
                "opencv-python 이 없습니다. 영상 입력을 쓰려면 설치하세요."
            ) from exc

        capture = cv2.VideoCapture(path)
        if not capture.isOpened():
            raise RuntimeError(f"영상을 열지 못했습니다: {path}")
        try:
            index = 0
            emitted = 0
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                if index % max(1, stride) == 0:
                    timestamp_ms = capture.get(cv2.CAP_PROP_POS_MSEC)
                    yield self.process_frame(
                        frame, frame_index=index, timestamp_ms=timestamp_ms
                    )
                    emitted += 1
                    if max_frames is not None and emitted >= max_frames:
                        break
                index += 1
        finally:
            capture.release()

    # ------------------------------------------------------------------ 보고

    def _report(self, mode: str, source: str, frames: List[FrameResult]) -> Dict[str, Any]:
        inference_values = [f.inference_ms for f in frames if f.inference_ms is not None]
        stop_frames = [f for f in frames if f.decision == STOP]
        return {
            "source": source,
            "mode": mode,
            "config": self.config.to_dict(),
            "frames": [f.to_dict() for f in frames],
            "summary": {
                "frame_count": len(frames),
                "person_total": sum(len(f.persons) for f in frames),
                "in_zone_total": sum(
                    1 for f in frames for p in f.persons if p.in_zone
                ),
                "stop_frames": len(stop_frames),
                "slow_frames": sum(1 for f in frames if f.decision == SLOW),
                "clear_frames": sum(1 for f in frames if f.decision == CLEAR),
                "first_stop_frame": stop_frames[0].frame_index if stop_frames else None,
                "final_decision": frames[-1].decision if frames else CLEAR,
                "inference_ms_avg": (
                    round(sum(inference_values) / len(inference_values), 2)
                    if inference_values
                    else None
                ),
            },
        }


def format_console(report: Dict[str, Any], max_rows: int = 20) -> str:
    """사람이 읽을 요약. CLI 가 그대로 찍는다."""
    summary = report["summary"]
    zone = report["config"]["zone"]
    lines = [
        f"[person_guard] source={report['source']} mode={report['mode']}",
        "  정지 존(비율) x {x_min:.2f}~{x_max:.2f} / y {y_min:.2f}~{y_max:.2f}".format(**zone),
        "  임계 stop_conf={stop_conf} warn_conf={warn_conf} overlap_ratio={overlap_ratio}".format(
            **report["config"]
        ),
        f"  프레임 {summary['frame_count']}장 · person {summary['person_total']}명 "
        f"· 존 안 {summary['in_zone_total']}건",
        f"  판정 STOP {summary['stop_frames']} / SLOW {summary['slow_frames']} "
        f"/ CLEAR {summary['clear_frames']} · 최종 {summary['final_decision']}",
    ]
    if summary["inference_ms_avg"] is not None:
        lines.append(f"  추론 평균 {summary['inference_ms_avg']}ms/장")
    if summary["first_stop_frame"] is not None:
        lines.append(f"  첫 정지 신호 프레임 #{summary['first_stop_frame']}")

    lines.append("  --- 프레임별 ---")
    for frame in report["frames"][:max_rows]:
        detail = ", ".join(
            f"conf={p['conf']:.2f}/겹침={p['overlap_ratio']:.2f}/{p['level']}"
            + ("/존안" if p["in_zone"] else "")
            for p in frame["persons"]
        ) or "person 없음"
        lines.append(
            f"  #{frame['frame_index']:>5} {frame['decision']:<5} (raw {frame['raw_decision']}) {detail}"
        )
    if len(report["frames"]) > max_rows:
        lines.append(f"  … {len(report['frames']) - max_rows}장 생략")
    return "\n".join(lines)
