"""출발·복귀 미션을 끝까지 돌려 결과를 판정한다.

성공 조건은 "FAULT 가 안 났다" 가 아니라 **모든 단계를 순서대로 밟고**
정해진 종착 상태에 도달하는 것이다. 단계를 건너뛰고 도착하면 실패로 본다.
"""
import json
import sys
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from std_srvs.srv import Trigger


class Verifier(Node):
    def __init__(self):
        super().__init__('verifier')
        self.st = {}
        self.steps = []          # 밟은 단계 이름 순서
        self.create_subscription(String, '/route/state', self.on_state, 10)
        self.cli = {n: self.create_client(Trigger, f'/route/{n}')
                    for n in ('start', 'return', 'abort', 'reset_fault')}

    def on_state(self, msg):
        self.st = json.loads(msg.data)
        n = self.st.get('step_name', '')
        if n and (not self.steps or self.steps[-1] != n):
            self.steps.append(n)

    def call(self, name):
        c = self.cli[name]
        if not c.wait_for_service(timeout_sec=10.0):
            return None
        f = c.call_async(Trigger.Request())
        rclpy.spin_until_future_complete(self, f, timeout_sec=10.0)
        return f.result()

    def spin(self, sec):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            rclpy.spin_once(self, timeout_sec=0.05)

    def wait(self, targets, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            rclpy.spin_once(self, timeout_sec=0.05)
            if self.st.get('state') in targets:
                return self.st['state']
        return f"TIMEOUT({self.st.get('state')})"


def run(v, mission, expect_state, expect_steps):
    v.steps = []
    r = v.call(mission)
    if r is None:
        return False, f'{mission}: 서비스 없음'
    if not r.success:
        return False, f'{mission} 거부: {r.message}'
    got = v.wait({expect_state, 'FAULT'}, timeout=90.0)
    if got != expect_state:
        return False, (f'{mission}: {got} (기대 {expect_state}) '
                       f'— {v.st.get("fault_reason", "")}')
    missing = [s for s in expect_steps if s not in v.steps]
    if missing:
        return False, f'{mission}: 건너뛴 단계 {missing} (밟은 것 {v.steps})'
    return True, f'{mission}: {expect_state} — 단계 {len(v.steps)}개 정상 통과'


def main():
    rclpy.init()
    v = Verifier()
    v.spin(4.0)                       # 텔레메트리·오도메트리 정착 대기
    results = []

    results.append(run(v, 'start', 'ARRIVED',
                       ['LEG1 도크→코너', 'TURN 우 90°', 'LEG2 코너→도착']))
    v.spin(1.0)
    results.append(run(v, 'return', 'DOCKED',
                       ['UTURN 우 180°', 'RET_LEG2 도착→코너',
                        'TURN 좌 90°', 'RET_LEG1 코너→도크']))

    # abort 로 즉시 중단되는가
    v.spin(1.0)
    r = v.call('start')
    ok_abort = False
    if r and r.success:
        v.spin(2.0)
        v.call('abort')
        s = v.wait({'FAULT', 'IDLE'}, timeout=5.0)
        ok_abort = s in ('FAULT', 'IDLE')
    results.append((ok_abort, f'abort: {"중단됨" if ok_abort else "실패"}'))

    # FAULT 는 해제 전까지 재시작을 막는가
    v.spin(0.5)
    blocked = v.call('start')
    ok_latch = blocked is not None and not blocked.success
    results.append((ok_latch,
                    f'FAULT 래치: {"재시작 거부됨" if ok_latch else "거부 안 함"}'))
    rr = v.call('reset_fault')
    ok_reset = rr is not None and rr.success
    results.append((ok_reset, f'reset_fault: {"해제됨" if ok_reset else "실패"}'))

    print('\n' + '=' * 70)
    bad = 0
    for ok, msg in results:
        print(f'  {"PASS" if ok else "FAIL"}  {msg}')
        bad += 0 if ok else 1
    print('=' * 70)
    print(f'{len(results) - bad}/{len(results)} 통과')
    rclpy.shutdown()
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
