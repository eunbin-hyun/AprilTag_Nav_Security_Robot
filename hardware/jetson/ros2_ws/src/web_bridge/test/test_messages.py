"""WebSocket 메시지 생성기 단위 테스트."""
import json

import pytest

from web_bridge.messages import (command_ack_message, decode_server_message,
                                 failure_reason,
                                 mission_status_from_route_state,
                                 mission_status_message, pong_message,
                                 status_summary,
                                 tag_target_message,
                                 unavailable_odom_message)


def test_status_summary_uses_security_screen_type():
    message = status_summary('RB1-01', 7, True, {'drive_state': 3}, 'OUTDOOR')

    assert message['type'] == 'status_summary'
    assert message['robot_id'] == 'RB1-01'
    assert message['seq'] == 7
    assert message['data']['drive_state'] == 'DRIVING'
    assert message['data']['comm_status'] == 'connected'
    assert message['data']['camera_status'] == 'unavailable'


def test_unavailable_odom_uses_null_coordinates_and_reason():
    message = unavailable_odom_message('RB1-01', 8, 'odom_timeout')

    assert message['type'] == 'odom'
    assert message['data']['valid'] is False
    assert message['data']['x'] is None
    assert message['data']['yaw'] is None
    assert message['data']['reason'] == 'odom_timeout'


def test_tag_target_contains_map_correction_values():
    message = tag_target_message('RB1-01', 9, {
        'tag_id': 3,
        'along': 0.82,
        'cross_track': 0.04,
        'heading_error': 0.03,
    })

    assert message['type'] == 'tag_target'
    assert message['robot_id'] == 'RB1-01'
    assert message['seq'] == 9
    assert message['data'] == {
        'tag_id': 3,
        'along': 0.82,
        'cross_track': 0.04,
        'heading_error': 0.03,
        'valid': True,
    }


def test_lost_tag_keeps_id_and_clears_stale_coordinates():
    message = tag_target_message(
        'RB1-01', 10, {'tag_id': 3, 'along': 0.82},
        valid=False, reason='tag_timeout')

    assert message['data'] == {
        'tag_id': 3,
        'along': None,
        'cross_track': None,
        'heading_error': None,
        'valid': False,
        'reason': 'tag_timeout',
    }


def test_ping_is_decoded_and_pong_matches_server_contract():
    kind, data = decode_server_message('{"type":"ping","data":{}}')

    assert (kind, data) == ('ping', {})
    assert pong_message() == {'type': 'pong', 'data': {}}


def test_command_is_decoded_and_accepted_ack_matches_server_contract():
    payload = json.dumps({
        'type': 'command',
        'data': {
            'command_id': 'shuttle-12',
            'type': 'cmd_destination',
            'ttl_ms': 30000,
            'cmd_destination': 'OUTDOOR_TAGGING',
        },
    })

    kind, command = decode_server_message(payload)

    assert kind == 'command'
    assert command['type'] == 'cmd_destination'
    assert command_ack_message(command['command_id'], True, '복귀 시작') == {
        'type': 'command_ack',
        'version': '1',
        'data': {
            'command_id': 'shuttle-12',
            'result': 'accepted',
            'reason': '복귀 시작',
        },
    }


def test_rejected_ack_contains_service_reason():
    message = command_ack_message('shuttle-13', False, '이미 주행 중')

    assert message['data']['result'] == 'rejected'
    assert message['data']['reason'] == '이미 주행 중'


def test_route_fault_becomes_failed_mission_with_reason():
    assert mission_status_from_route_state('FAULT') == 'FAILED'

    message = mission_status_message(
        'RB1-01', 11, 'FAILED', reason='태그 미검출')

    assert message['data'] == {
        'status': 'FAILED',
        'reason': '태그 미검출',
    }


def test_running_becomes_en_route_without_checkpoint():
    """이동 중은 지점 낱말이 없다. A·C 뿐인 표에 지어 넣으면 도착으로 잘못 짝짓는다."""
    assert mission_status_from_route_state('RUNNING') == 'EN_ROUTE'

    message = mission_status_message('RB1-01', 12, 'EN_ROUTE')

    assert message['data'] == {'status': 'EN_ROUTE'}


@pytest.mark.parametrize('state', ['IDLE', 'HOLD', 'BOOT', None, 7])
def test_states_without_server_word_are_not_sent(state):
    """HOLD 를 EN_ROUTE 로 옮기면 멈춘 로봇이 이동 중으로 보고된다."""
    assert mission_status_from_route_state(state) is None


def test_failure_reason_carries_where_it_stopped():
    """서버가 좌표·IMU 를 같이 저장하므로 '어디서' 만 있으면 원인이 좁혀진다."""
    reason = failure_reason({
        'fault_reason': '태그 미검출',
        'step_name': '태그 3 접근',
        'step_index': 1,
        'step_total': 4,
        'target_tag_id': 3,
        'traveled_m': 1.238,
    })

    assert reason == ('태그 미검출 (단계 2/4 태그 3 접근, 목표 태그 3, '
                      '주행 1.24 m)')


def test_failure_reason_omits_missing_and_meaningless_fields():
    """목표 태그 0 은 '이 단계엔 태그가 없다' 는 뜻이라 싣지 않는다."""
    assert failure_reason({'fault_reason': '통신 단절',
                           'target_tag_id': 0}) == '통신 단절'
    assert failure_reason({}) == '주행 실패'


def test_tag_target_carries_distance_reference_plane():
    """`along` 이 뒷차축 기준이라, 앞머리 여유를 그리려면 이 값이 필요하다."""
    target = {'tag_id': 3, 'along': 0.82, 'cross_track': 0.04,
              'heading_error': 0.03}

    with_plane = tag_target_message('RB1-01', 13, target,
                                    front_overhang_m=0.18)
    assert with_plane['data']['front_overhang_m'] == 0.18

    # route_runner 가 없으면 칸 자체를 뺀다 — 0 으로 채우면 두 기준을 구분 못 한다.
    assert 'front_overhang_m' not in tag_target_message(
        'RB1-01', 14, target)['data']


@pytest.mark.parametrize('payload', [
    'not-json',
    '[]',
    '{"type":"unknown"}',
    '{"type":"command","data":{}}',
    ('{"type":"command","data":{"command_id":"x",'
     '"type":"unknown","ttl_ms":30000}}'),
])
def test_invalid_server_messages_are_rejected(payload):
    with pytest.raises(ValueError):
        decode_server_message(payload)
