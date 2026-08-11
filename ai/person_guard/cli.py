"""person_guard 명령줄 진입점.

    python -m ai.person_guard --source frame.jpg --weights yolov8n.pt --gpu-index 6
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="person_guard",
        description="주행 중 사람 감지 → 정지 존 판정 (사전학습 YOLO person 클래스)",
    )
    parser.add_argument("--source", required=True, help="이미지 또는 영상 파일 경로")
    parser.add_argument(
        "--mode",
        choices=("auto", "image", "video"),
        default="auto",
        help="입력 종류. auto 는 확장자로 고릅니다 (기본값)",
    )
    parser.add_argument("--config", help="설정 JSON 경로. 나머지 옵션이 이 값을 덮습니다")
    parser.add_argument("--weights", help="가중치 파일 (기본 yolov8n.pt)")
    parser.add_argument("--device", help="ultralytics device 문자열 (기본 0)")
    parser.add_argument("--imgsz", type=int, help="추론 입력 크기 (기본 640)")
    parser.add_argument("--stop-conf", type=float, help="정지 확정 임계 (기본 0.60)")
    parser.add_argument("--warn-conf", type=float, help="감속·재확인 임계 (기본 0.35)")
    parser.add_argument("--overlap-ratio", type=float, help="존 침입으로 볼 겹침 비율 (기본 0.25)")
    parser.add_argument("--stop-frames", type=int, help="정지 확정에 필요한 연속 프레임 수")
    parser.add_argument("--release-frames", type=int, help="정지 해제에 필요한 연속 프레임 수")
    parser.add_argument(
        "--zone",
        nargs=4,
        type=float,
        metavar=("X_MIN", "X_MAX", "Y_MIN", "Y_MAX"),
        help="정지 존 비율 사각형 (기본 0.20 0.80 0.65 1.00)",
    )
    parser.add_argument(
        "--gpu-index",
        help="쓸 물리 GPU 번호. torch import 앞에서 CUDA_VISIBLE_DEVICES 로 고정합니다",
    )
    parser.add_argument("--stride", type=int, default=1, help="영상 프레임 건너뛰기 (기본 1)")
    parser.add_argument("--max-frames", type=int, help="영상에서 볼 최대 프레임 수")
    parser.add_argument("--json", dest="json_out", help="결과 JSON 을 저장할 경로")
    parser.add_argument("--quiet", action="store_true", help="콘솔 요약을 찍지 않습니다")
    return parser


def _resolve_mode(source: str, mode: str) -> str:
    if mode != "auto":
        return mode
    return "image" if os.path.splitext(source)[1].lower() in IMAGE_SUFFIXES else "video"


def _build_config(args: argparse.Namespace):
    from .config import GuardConfig  # noqa: PLC0415 (GPU 고정 뒤에 올리려고 늦춘다)

    data: Dict[str, Any] = {}
    if args.config:
        data = json.load(open(args.config, "r", encoding="utf-8"))

    overrides = {
        "weights": args.weights,
        "device": args.device,
        "imgsz": args.imgsz,
        "stop_conf": args.stop_conf,
        "warn_conf": args.warn_conf,
        "overlap_ratio": args.overlap_ratio,
        "stop_frames": args.stop_frames,
        "release_frames": args.release_frames,
    }
    for key, value in overrides.items():
        if value is not None:
            data[key] = value
    if args.zone:
        x_min, x_max, y_min, y_max = args.zone
        data["zone"] = {"x_min": x_min, "x_max": x_max, "y_min": y_min, "y_max": y_max}
    return GuardConfig.from_dict(data)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.gpu_index is not None:
        from .detector import pin_gpu  # noqa: PLC0415

        pin_gpu(args.gpu_index)

    config = _build_config(args)

    from .pipeline import PersonGuard, format_console  # noqa: PLC0415

    guard = PersonGuard(config)
    mode = _resolve_mode(args.source, args.mode)
    if mode == "image":
        report = guard.run_image(args.source)
    else:
        report = guard.run_video(
            args.source, stride=args.stride, max_frames=args.max_frames
        )

    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fp:
            json.dump(report, fp, ensure_ascii=False, indent=2)
    if not args.quiet:
        print(format_console(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
