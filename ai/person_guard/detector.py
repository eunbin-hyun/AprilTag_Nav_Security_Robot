"""YOLO person 검출 껍데기.

ultralytics 는 무겁고 젯슨·GPU 서버에만 있어서 여기서만 늦게 import 한다.
그래서 zone·pipeline 계약 시험은 ultralytics 없이도 돈다.
"""

from __future__ import annotations

import os
import time
from typing import List, Optional, Sequence, Tuple

from .types import Detection


def pin_gpu(index: str = "0") -> None:
    """쓸 GPU 를 고정한다. **torch·ultralytics import 앞에서** 불러야 먹는다.

    GPU 서버(jupyter02)처럼 카드가 여럿 꽂힌 기계에서 남의 카드를 안 건드리려면
    필수다. 젯슨은 GPU 가 하나라 부를 일이 없다.
    """
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    os.environ["CUDA_VISIBLE_DEVICES"] = str(index)


class PersonDetector:
    """사전학습 YOLO 로 person 클래스만 뽑는다. 추가 학습은 필요 없다."""

    def __init__(
        self,
        weights: str,
        device: str = "0",
        imgsz: int = 640,
        min_conf: float = 0.35,
    ) -> None:
        self.weights = weights
        self.device = device
        self.imgsz = imgsz
        self.min_conf = min_conf
        self._model = None
        self._person_ids: Optional[Sequence[int]] = None

    def load(self) -> None:
        """가중치를 올린다. 여러 번 불러도 한 번만 올라간다."""
        if self._model is not None:
            return
        try:
            from ultralytics import YOLO  # noqa: PLC0415  (의도한 지연 import)
        except ImportError as exc:  # pragma: no cover - 환경 의존
            raise RuntimeError(
                "ultralytics 가 없습니다. `pip install ultralytics` 후 다시 실행하세요."
            ) from exc
        self._model = YOLO(self.weights)
        self._person_ids = self._resolve_person_ids(self._model)

    @staticmethod
    def _resolve_person_ids(model) -> List[int]:
        """클래스 이름에서 person id 를 찾는다.

        COCO 사전학습은 0 이지만, 나중에 커스텀 모델로 갈아도 이름으로 찾으면 안 깨진다.
        """
        names = getattr(model, "names", None) or {}
        if isinstance(names, dict):
            pairs = names.items()
        else:
            pairs = enumerate(names)
        ids = [int(i) for i, name in pairs if str(name).lower() == "person"]
        if not ids:
            raise RuntimeError(
                f"모델에 person 클래스가 없습니다. 가진 클래스: {list(names)[:20]}"
            )
        return ids

    @property
    def person_class_ids(self) -> Sequence[int]:
        if self._person_ids is None:
            raise RuntimeError("load() 를 먼저 부르세요.")
        return self._person_ids

    def detect(self, frame) -> Tuple[List[Detection], float, Tuple[int, int]]:
        """프레임 하나를 추론한다.

        frame 은 파일 경로거나 numpy BGR 배열이다.
        (검출 목록, 추론 밀리초, (가로, 세로)) 를 돌려준다.
        """
        self.load()
        started = time.perf_counter()
        results = self._model.predict(
            source=frame,
            imgsz=self.imgsz,
            conf=self.min_conf,
            classes=list(self.person_class_ids),
            device=self.device,
            verbose=False,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        if not results:
            return [], elapsed_ms, (0, 0)
        result = results[0]
        height, width = result.orig_shape

        detections: List[Detection] = []
        boxes = getattr(result, "boxes", None)
        if boxes is not None and len(boxes):
            xyxy = boxes.xyxy.tolist()
            confs = boxes.conf.tolist()
            for (x1, y1, x2, y2), conf in zip(xyxy, confs):
                detections.append(
                    Detection(bbox=(float(x1), float(y1), float(x2), float(y2)), conf=float(conf))
                )
        return detections, elapsed_ms, (int(width), int(height))
