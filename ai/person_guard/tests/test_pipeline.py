"""파이프라인 계약 시험. 검출기를 스텁으로 갈아서 YOLO 없이 돈다."""

import json
import os
import tempfile
import unittest

from ai.person_guard.cli import main
from ai.person_guard.config import GuardConfig
from ai.person_guard.pipeline import PersonGuard, format_console
from ai.person_guard.types import CLEAR, STOP, Detection

W, H = 640, 480

IN_ZONE = Detection(bbox=(280, 330, 380, 470), conf=0.90)
OUT_ZONE = Detection(bbox=(300, 10, 360, 120), conf=0.90)


class StubDetector:
    """프레임마다 미리 정한 검출을 돌려준다."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = 0

    def detect(self, frame):
        dets = self.frames[min(self.calls, len(self.frames) - 1)]
        self.calls += 1
        return dets, 12.34, (W, H)


class RunImageTest(unittest.TestCase):
    def test_존_안_사람이면_보고서가_STOP(self):
        guard = PersonGuard(GuardConfig(), detector=StubDetector([[IN_ZONE]]))
        report = guard.run_image("frame.jpg")
        self.assertEqual(report["mode"], "image")
        self.assertEqual(report["summary"]["frame_count"], 1)
        self.assertEqual(report["summary"]["final_decision"], STOP)
        self.assertEqual(report["summary"]["stop_frames"], 1)
        self.assertEqual(report["summary"]["in_zone_total"], 1)
        self.assertEqual(report["frames"][0]["trigger_count"], 1)

    def test_이미지_한_장은_디바운스를_안_탄다(self):
        # stop_frames=2 여도 이미지 한 장이면 바로 STOP 이어야 한다
        guard = PersonGuard(
            GuardConfig(stop_frames=2), detector=StubDetector([[IN_ZONE]])
        )
        report = guard.run_image("frame.jpg")
        self.assertEqual(report["summary"]["final_decision"], STOP)

    def test_사람이_없으면_CLEAR(self):
        guard = PersonGuard(GuardConfig(), detector=StubDetector([[]]))
        report = guard.run_image("frame.jpg")
        self.assertEqual(report["summary"]["final_decision"], CLEAR)
        self.assertEqual(report["summary"]["person_total"], 0)

    def test_보고서는_JSON_직렬화가_된다(self):
        guard = PersonGuard(GuardConfig(), detector=StubDetector([[IN_ZONE, OUT_ZONE]]))
        report = guard.run_image("frame.jpg")
        self.assertIn("person_guard", format_console(report))
        json.dumps(report, ensure_ascii=False)  # 예외 안 나야 한다


class DebounceInVideoTest(unittest.TestCase):
    def test_프레임_흐름에서_한_번_튐은_눌린다(self):
        guard = PersonGuard(
            GuardConfig(stop_frames=2, release_frames=2),
            detector=StubDetector([]),
        )
        seq = [[OUT_ZONE], [IN_ZONE], [OUT_ZONE], [IN_ZONE], [IN_ZONE]]
        guard.detector = StubDetector(seq)
        decisions = [
            guard.process_frame(None, frame_index=i).decision for i in range(len(seq))
        ]
        self.assertEqual(decisions, [CLEAR, CLEAR, CLEAR, CLEAR, STOP])


class CliTest(unittest.TestCase):
    def test_모드_자동판별(self):
        from ai.person_guard.cli import _resolve_mode

        self.assertEqual(_resolve_mode("a/b.JPG", "auto"), "image")
        self.assertEqual(_resolve_mode("a/b.mp4", "auto"), "video")
        self.assertEqual(_resolve_mode("a/b.jpg", "video"), "video")

    def test_설정파일과_옵션이_합쳐진다(self):
        from ai.person_guard.cli import _build_config, build_parser

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            with open(path, "w", encoding="utf-8") as fp:
                json.dump({"stop_conf": 0.9, "zone": {"y_min": 0.5}}, fp)
            args = build_parser().parse_args(
                ["--source", "x.jpg", "--config", path, "--overlap-ratio", "0.4"]
            )
            config = _build_config(args)
        self.assertEqual(config.stop_conf, 0.9)
        self.assertEqual(config.zone.y_min, 0.5)
        self.assertEqual(config.overlap_ratio, 0.4)

    def test_명령줄_zone이_설정파일을_덮는다(self):
        from ai.person_guard.cli import _build_config, build_parser

        args = build_parser().parse_args(
            ["--source", "x.jpg", "--zone", "0.0", "1.0", "0.0", "1.0"]
        )
        config = _build_config(args)
        self.assertEqual(
            (config.zone.x_min, config.zone.x_max, config.zone.y_min, config.zone.y_max),
            (0.0, 1.0, 0.0, 1.0),
        )

    def test_잘못된_임계는_에러(self):
        from ai.person_guard.cli import _build_config, build_parser

        args = build_parser().parse_args(
            ["--source", "x.jpg", "--stop-conf", "0.2", "--warn-conf", "0.8"]
        )
        with self.assertRaises(ValueError):
            _build_config(args)

    def test_source_없으면_종료(self):
        with self.assertRaises(SystemExit):
            main([])


if __name__ == "__main__":
    unittest.main()
