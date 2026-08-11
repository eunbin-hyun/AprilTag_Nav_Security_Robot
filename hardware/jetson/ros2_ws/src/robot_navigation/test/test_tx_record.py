"""/stm32/command 레코드 시험 (전선에 나간 CMD_DRIVE)."""
# _tx_record 만 떼어 본다 — RouteRunner 전체를 띄우려면 시리얼 포트와 ROS
# 컨텍스트가 필요하고, 이 레코드의 계약(스키마 고정 + 포화 판정)은 그것들과
# 무관하다. 그래서 필요한 속성만 가진 stub 에 언바운드 메서드를 붙인다.
from robot_navigation.route_runner import RouteRunner

# 소비자(웹·rosbag 분석)가 기대하는 키. 하나라도 사라지면 조용히 깨지므로
# **집합으로 고정**한다. reason 은 kind='stop' 에만 붙는 선택 키다.
REQUIRED = {
    'kind', 'seq', 'speed_mm_s', 'steering_cdeg', 'steering_deg', 'enable',
    'control_steering_cdeg', 'trim_cdeg', 'saturated', 'forced_neutral',
    'mcu_ack_seq',
}


class _Stub:
    """_tx_record 가 실제로 읽는 속성만 갖는다."""

    def __init__(self, trim=0, forced=False):
        self.steer_trim_cdeg = trim
        self._forced_neutral = forced


def rec(stub, *a, **kw):
    return RouteRunner._tx_record(stub, *a, **kw)


def test_schema_is_stable_for_drive_and_stop():
    drive = rec(_Stub(trim=500), 200, 1955, 1, 42, 1800)
    stop = rec(_Stub(trim=500), 0, 0, 0, 43, kind='stop', reason=1)
    assert REQUIRED <= set(drive)
    assert REQUIRED <= set(stop)
    # mcu_ack_seq 는 stop 경로에서도 **키가 있어야** 한다 (값은 None).
    # 없으면 소비자가 kind 마다 다르게 방어해야 한다.
    assert drive['mcu_ack_seq'] is None
    assert stop['mcu_ack_seq'] is None
    assert 'reason' not in drive          # drive 에는 붙지 않는다
    assert stop['reason'] == 1
    assert set(drive) - REQUIRED == set()


def test_wire_value_is_post_trim():
    # 제어층은 +1800 을 냈지만 전선에는 트림이 얹혀 +1955 가 나갔다.
    # 이 둘을 같이 담는 것이 이 토픽의 존재 이유다 — /route/state 의
    # command 는 트림 전 값만 있어서 실제 곡률을 알 수 없었다.
    r = rec(_Stub(trim=500), 200, 1955, 1, 7, 1800)
    assert r['steering_cdeg'] == 1955
    assert r['steering_deg'] == 19.55
    assert r['control_steering_cdeg'] == 1800
    assert r['trim_cdeg'] == 500


def test_saturated_flags_lost_curvature():
    # 요청(1800) + 트림(500) = 2300 이 한계 1955 로 잘렸다 -> 포화.
    assert rec(_Stub(trim=500), 200, 1955, 1, 1, 1800)['saturated']
    # 잘리지 않았으면 포화가 아니다: -1800 + 500 = -1300 이 그대로 나갔다.
    assert not rec(_Stub(trim=500), 200, -1300, 1, 2, -1800)['saturated']
    # enable=0 프레임은 조향을 얹지 않으므로 포화로 보지 않는다.
    assert not rec(_Stub(trim=500), 0, 0, 0, 3, 0)['saturated']
    # 트림이 없으면 포화의 원인이 될 것도 없다 (route_logic 이 이미 clamp).
    assert not rec(_Stub(trim=0), 200, 1955, 1, 4, 1800)['saturated']


def test_forced_neutral_is_recorded():
    # TX watchdog 이 neutral 을 강제한 순간은 지금 WARN 한 줄로만 지나간다.
    # 나중에 로그 없이 재구성하려면 스트림에 남아야 한다.
    assert rec(_Stub(trim=500, forced=True), 0, 0, 0, 5)['forced_neutral']
    assert not rec(_Stub(trim=500), 0, 0, 0, 6)['forced_neutral']
