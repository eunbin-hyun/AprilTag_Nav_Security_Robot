"""가짜 토픽 발행 노드의 정적 검증."""
from pathlib import Path


def test_fake_topic_names_match_web_bridge_subscriptions():
    source = (Path(__file__).parent.parent / 'web_bridge' /
              'fake_web_topics.py').read_text()

    for topic in ('/stm32/connected', '/stm32/telemetry',
                  '/stm32/odom', '/stm32/imu'):
        assert topic in source
