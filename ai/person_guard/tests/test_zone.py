"""정지 존 판정 단위시험. ultralytics·opencv 없이 돈다."""

import unittest

from ai.person_guard.config import GuardConfig, ZoneConfig
from ai.person_guard.types import CLEAR, SLOW, STOP, Detection
from ai.person_guard.zone import (
    StopSignalTracker,
    classify_level,
    intersection_area,
    judge_frame,
)

W, H = 640, 480


def cfg(**kw):
    return GuardConfig(**kw)


class ZoneConfigTest(unittest.TestCase):
    def test_기본_존은_하단_중앙(self):
        x1, y1, x2, y2 = ZoneConfig().to_pixels(W, H)
        self.assertEqual((x1, x2), (128.0, 512.0))
        self.assertEqual((y1, y2), (312.0, 480.0))

    def test_범위_밖_비율은_거부(self):
        with self.assertRaises(ValueError):
            ZoneConfig(y_max=1.5)

    def test_뒤집힌_사각형은_거부(self):
        with self.assertRaises(ValueError):
            ZoneConfig(x_min=0.8, x_max=0.2)


class IntersectionTest(unittest.TestCase):
    def test_안_겹치면_0(self):
        self.assertEqual(intersection_area((0, 0, 10, 10), (20, 20, 30, 30)), 0.0)

    def test_모서리만_닿으면_0(self):
        self.assertEqual(intersection_area((0, 0, 10, 10), (10, 10, 20, 20)), 0.0)

    def test_절반_겹침(self):
        self.assertEqual(intersection_area((0, 0, 10, 10), (5, 0, 15, 10)), 50.0)


class ClassifyLevelTest(unittest.TestCase):
    def test_3단_경계(self):
        c = cfg(stop_conf=0.6, warn_conf=0.35)
        self.assertEqual(classify_level(0.60, c), "stop")
        self.assertEqual(classify_level(0.59, c), "warn")
        self.assertEqual(classify_level(0.35, c), "warn")
        self.assertEqual(classify_level(0.34, c), "ignore")


class JudgeFrameTest(unittest.TestCase):
    def test_존_밖_사람은_통과(self):
        # 화면 위쪽 = 멀리 있는 사람
        det = Detection(bbox=(300, 10, 360, 120), conf=0.9)
        decision, hits = judge_frame([det], W, H, cfg())
        self.assertEqual(decision, CLEAR)
        self.assertFalse(hits[0].in_zone)
        self.assertEqual(hits[0].overlap_ratio, 0.0)

    def test_존_안_확신_검출은_정지(self):
        det = Detection(bbox=(280, 330, 380, 470), conf=0.88)
        decision, hits = judge_frame([det], W, H, cfg())
        self.assertEqual(decision, STOP)
        self.assertTrue(hits[0].in_zone)
        self.assertEqual(hits[0].level, "stop")
        self.assertAlmostEqual(hits[0].overlap_ratio, 1.0, places=6)

    def test_애매한_confidence는_감속(self):
        det = Detection(bbox=(280, 330, 380, 470), conf=0.45)
        decision, _ = judge_frame([det], W, H, cfg())
        self.assertEqual(decision, SLOW)

    def test_낮은_confidence는_무시(self):
        det = Detection(bbox=(280, 330, 380, 470), conf=0.20)
        decision, hits = judge_frame([det], W, H, cfg())
        self.assertEqual(decision, CLEAR)
        self.assertEqual(hits[0].level, "ignore")

    def test_겹침이_임계_아래면_통과(self):
        # 존 위 경계 y=312 를 살짝만 물어서 겹침 비율이 0.25 미만
        det = Detection(bbox=(280, 100, 380, 322), conf=0.95)
        decision, hits = judge_frame([det], W, H, cfg(overlap_ratio=0.25))
        self.assertLess(hits[0].overlap_ratio, 0.25)
        self.assertEqual(decision, CLEAR)

    def test_한_명이라도_정지면_프레임은_정지(self):
        dets = [
            Detection(bbox=(300, 10, 360, 120), conf=0.9),      # 존 밖
            Detection(bbox=(280, 330, 380, 470), conf=0.45),    # 존 안, 애매
            Detection(bbox=(200, 340, 300, 470), conf=0.92),    # 존 안, 확신
        ]
        decision, hits = judge_frame(dets, W, H, cfg())
        self.assertEqual(decision, STOP)
        self.assertEqual(sum(1 for h in hits if h.in_zone), 2)

    def test_넓이_0_박스는_안전하게_통과(self):
        det = Detection(bbox=(300, 400, 300, 400), conf=0.99)
        decision, hits = judge_frame([det], W, H, cfg())
        self.assertEqual(decision, CLEAR)
        self.assertEqual(hits[0].overlap_ratio, 0.0)

    def test_존을_넓히면_같은_박스가_정지로_바뀐다(self):
        det = Detection(bbox=(280, 100, 380, 322), conf=0.95)
        wide = cfg(zone=ZoneConfig(x_min=0.0, x_max=1.0, y_min=0.0, y_max=1.0))
        decision, _ = judge_frame([det], W, H, wide)
        self.assertEqual(decision, STOP)


class TrackerTest(unittest.TestCase):
    def test_한_프레임_튐은_정지로_안_간다(self):
        t = StopSignalTracker(cfg(stop_frames=2, release_frames=3))
        self.assertEqual(t.update(STOP), CLEAR)
        self.assertEqual(t.update(CLEAR), CLEAR)

    def test_연속_두_프레임이면_정지(self):
        t = StopSignalTracker(cfg(stop_frames=2, release_frames=3))
        t.update(STOP)
        self.assertEqual(t.update(STOP), STOP)

    def test_해제는_연속_세_프레임_뒤(self):
        t = StopSignalTracker(cfg(stop_frames=2, release_frames=3))
        t.update(STOP)
        t.update(STOP)
        self.assertEqual(t.update(CLEAR), STOP)
        self.assertEqual(t.update(CLEAR), STOP)
        self.assertEqual(t.update(CLEAR), CLEAR)

    def test_감속은_즉시_반영(self):
        t = StopSignalTracker(cfg(stop_frames=2, release_frames=3))
        self.assertEqual(t.update(SLOW), SLOW)

    def test_reset하면_처음_상태(self):
        t = StopSignalTracker(cfg(stop_frames=1, release_frames=1))
        t.update(STOP)
        self.assertEqual(t.state, STOP)
        t.reset()
        self.assertEqual(t.state, CLEAR)


class GuardConfigTest(unittest.TestCase):
    def test_임계_순서가_뒤집히면_거부(self):
        with self.assertRaises(ValueError):
            GuardConfig(stop_conf=0.3, warn_conf=0.7)

    def test_모르는_항목은_거부(self):
        with self.assertRaises(ValueError):
            GuardConfig.from_dict({"stopconf": 0.5})

    def test_dict_왕복(self):
        original = GuardConfig(stop_conf=0.7, zone=ZoneConfig(y_min=0.5))
        self.assertEqual(GuardConfig.from_dict(original.to_dict()), original)


if __name__ == "__main__":
    unittest.main()
