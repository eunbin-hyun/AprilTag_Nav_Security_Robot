"""관제 화면 송출률 게이트 시험.

송출률은 **웹팀과의 계약**이다 (`jetson_to_web_interface_v2.md` §10.1:
"관제 화면용 프레임률은 우선 5~10 fps로 제한한다"). 조용히 깨지면 대역폭이
두 배로 나가고 브라우저가 뒤로 밀린다. 그래서 시험으로 고정한다.

⚠ 프론트가 제안한 "N 장에 한 번"(프레임 개수 modulo) 방식은 카메라 fps 가
  흔들릴 때 계약을 벗어난다. 아래 마지막 두 시험이 그 차이를 보인다.
"""
from robot_perception.tag_localizer_cv import emit_due

CONTRACT_MIN, CONTRACT_MAX = 5.0, 10.0


def run_gate(camera_fps: float, target_fps: float, seconds: float = 10.0):
    """카메라가 camera_fps 로 돌 때 게이트를 통과한 프레임 수 -> 출력 fps."""
    min_dt = 1.0 / target_fps if target_fps > 0 else 0.0
    dt = 1.0 / camera_fps
    last = None
    emitted = 0
    n = int(seconds * camera_fps)
    for i in range(1, n + 1):
        now = i * dt
        if emit_due(now, last, min_dt):
            last = now
            emitted += 1
    return emitted / seconds


def run_modulo(camera_fps: float, every: int, seconds: float = 10.0):
    """프론트가 제안한 개수 기반 방식 — 비교용."""
    n = int(seconds * camera_fps)
    return sum(1 for i in range(1, n + 1) if i % every == 0) / seconds


# ── 기본 동작 ─────────────────────────────────────────────

def test_no_limit_when_zero():
    """0 이면 제한하지 않는다 (기존 동작 유지 경로)."""
    assert emit_due(0.0, 0.0, 0.0)
    assert emit_due(1e-9, 0.0, 0.0)


def test_blocks_before_interval():
    assert not emit_due(0.10, 0.0, 1.0 / 7.0)      # 0.143 s 아직 안 됨


def test_passes_at_interval():
    assert emit_due(1.0 / 7.0, 0.0, 1.0 / 7.0)


def test_first_frame_passes():
    """아직 한 번도 안 보냈으면 즉시 통과해야 한다.

    처음엔 last 를 0.0 으로 뒀는데, 그게 통과한 이유는 time.monotonic() 이
    항상 큰 값이라는 암묵적 가정 덕이었다. None 으로 명시한다.
    """
    assert emit_due(0.05, None, 1.0 / 7.0)
    assert emit_due(0.0, None, 1.0 / 7.0)


# ── 계약 준수 ─────────────────────────────────────────────

def test_output_rate_matches_target():
    got = run_gate(camera_fps=20.0, target_fps=7.0)
    assert CONTRACT_MIN <= got <= CONTRACT_MAX, got


def test_contract_holds_across_camera_rates():
    """카메라가 흔들려도 출력은 계약 안에 있어야 한다.

    노출 변경·USB 경합·CPU 부하로 카메라 fps 는 실제로 흔들린다.
    """
    for cam in (12.0, 15.0, 20.0, 25.0, 30.0):
        got = run_gate(camera_fps=cam, target_fps=7.0)
        assert CONTRACT_MIN <= got <= CONTRACT_MAX, f'{cam} fps -> {got}'


def test_never_exceeds_target():
    """상한을 넘으면 대역폭 계약이 깨진다."""
    for cam in (20.0, 60.0, 200.0):
        assert run_gate(camera_fps=cam, target_fps=7.0) <= 7.0 + 0.2


# ── 개수 기반과의 차이 (프론트 제안 검토) ──────────────────

def test_modulo_breaks_contract_when_camera_slows():
    """`% 3` 은 카메라가 12 fps 로 떨어지면 4 fps 가 되어 계약을 벗어난다."""
    assert run_modulo(camera_fps=20.0, every=3) > CONTRACT_MIN     # 6.7 OK
    slow = run_modulo(camera_fps=12.0, every=3)
    assert slow < CONTRACT_MIN, slow                               # 4.0 위반


def test_time_based_survives_the_same_slowdown():
    """같은 조건에서 시간 기반은 계약을 지킨다."""
    assert run_gate(camera_fps=12.0, target_fps=7.0) >= CONTRACT_MIN


def test_modulo_overshoots_when_camera_speeds_up():
    """반대 방향도 문제다 — 카메라가 빠르면 `% 3` 이 상한을 넘는다."""
    assert run_modulo(camera_fps=60.0, every=3) > CONTRACT_MAX      # 20 위반
    assert run_gate(camera_fps=60.0, target_fps=7.0) <= CONTRACT_MAX
