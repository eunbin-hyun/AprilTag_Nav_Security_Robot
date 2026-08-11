"""Ackermann 변환 시험. 명세 §16.1 UT-05/06."""
import math

from stm32_bridge.ackermann import twist_to_drive, NEUTRAL

# 시험용 임의 차량 (실차 값 아님 — TBD 확정 전 개발용)
KW = dict(wheelbase_m=0.3, max_speed_mm_s=1000, max_steering_cdeg=3000)


def test_straight_forward():
    cmd = twist_to_drive(0.5, 0.0, **KW)
    assert cmd.speed_mm_s == 500
    assert cmd.steering_cdeg == 0
    assert cmd.enable is True


def test_forward_left_turn_positive_steering():
    # REP-103: omega(+) = 좌회전 -> 조향각(+)
    cmd = twist_to_drive(0.5, 0.5, **KW)
    expected = round(math.degrees(math.atan(0.3 * 0.5 / 0.5)) * 100)
    assert cmd.steering_cdeg == expected
    assert cmd.steering_cdeg > 0


def test_reverse_sign_flips():
    # v<0에서 같은 omega면 조향 부호가 뒤집힌다 (명세 §11.5 경고 대상)
    fwd = twist_to_drive(0.5, 0.5, **KW)
    rev = twist_to_drive(-0.5, 0.5, **KW)
    assert rev.speed_mm_s == -500
    assert rev.steering_cdeg == -fwd.steering_cdeg


def test_stop_is_neutral():
    assert twist_to_drive(0.0, 0.0, **KW) == NEUTRAL


def test_in_place_rotation_rejected():
    # |v|<eps, |omega|>eps -> None (거부). 0으로 나누기 없음.
    assert twist_to_drive(0.0, 1.0, **KW) is None
    assert twist_to_drive(0.01, 1.0, **KW) is None    # eps 미만


def test_clamp():
    cmd = twist_to_drive(5.0, 0.0, **KW)              # 5 m/s 요청
    assert cmd.speed_mm_s == 1000                     # max로 clamp
    cmd = twist_to_drive(0.1, 50.0, **KW)             # 급조향 요청
    assert cmd.steering_cdeg == 3000


def test_tbd_guard_forces_neutral():
    # 한계값 미설정(TBD) -> 비영 주행 금지 (명세 §20.5)
    cmd = twist_to_drive(0.5, 0.0, wheelbase_m=0.0,
                         max_speed_mm_s=0, max_steering_cdeg=0)
    assert cmd == NEUTRAL
