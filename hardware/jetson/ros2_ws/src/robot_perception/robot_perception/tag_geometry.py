"""AprilTag 관측 -> 주행 제어 오차량 변환 (순수 Python, ROS import 금지).

ROS 없이 단위시험하기 위해 분리했다. `stm32_bridge` 의 `protocol.py` /
`parser.py` 와 같은 관례다.

역할 분담:
  - **tf2 가 담당**: `camera_optical_frame` -> `base_link` 변환. 광학 좌표계
    규약(z 전방/x 우/y 하)이 부호 실수가 나는 지점이므로 우리가 계산하지 않는다.
  - **이 모듈이 담당**: tf2 가 돌려준 "base_link 기준 태그 pose" 로부터 제어에
    필요한 오차량을 만들고, 품질 게이트·필터·소실 판정을 한다.

좌표계 (REP-103):
  base_link  x 전방 / y 좌 / z 상
  로봇은 항상 원점, heading 0 으로 본다.

제어 오차 정의 — **경로 추종(path following) 형식**을 쓴다. "경로" 는 태그
중심선, 즉 태그 중심을 지나 태그 법선 방향으로 뻗은 직선이다.

  cross_track    경로에서 로봇의 횡방향 이탈. **양수 = 로봇이 경로 왼쪽**
  heading_error  로봇 heading - 경로 heading. **양수 = 로봇이 왼쪽을 향함**
  along          경로 방향으로 로봇에서 태그까지의 거리

두 오차가 **같은 부호일 때 같은 방향(우조향)으로 보정**되도록 정의했다. 그래서
제어식이 `steering = -(Kp_e * cross_track + Kp_h * heading_error)` 로 단순해진다.

`cross_track` 만 쓰면 중심선에 **비스듬히** 도착한다. `heading_error` 항이
자세를 맞춘다. 두 항이 다 필요하다.

⚠ 태그 좌표계에서 어느 축이 "면 법선" 인지는 검출기 규약에 달렸다. 기본값은
  apriltag 관례인 **+z** 이고, `TagGeometryConfig.normal_axis` / `normal_flip`
  으로 바꿀 수 있다. **실장비에서 반드시 경험적으로 확인해야 한다** — 태그를
  카메라 정면 1 m 에 놓고 `cross_track ~ 0`, `heading_error ~ 0`, `along ~ 1.0`
  이 나오는지 본다. 부호가 반대면 `normal_flip` 을 켠다.
"""
import math
from dataclasses import dataclass, replace


# ── 설정 ───────────────────────────────────────────────────

@dataclass(frozen=True)
class TagGeometryConfig:
    """태그 좌표계 규약. 실장비 확인 후 확정한다."""

    normal_axis: tuple = (0.0, 0.0, 1.0)   # 태그 로컬 좌표에서 면 법선 축
    normal_flip: bool = False              # 법선이 반대로 나오면 True


@dataclass(frozen=True)
class GateConfig:
    """품질 게이트. 임계값은 정적 정확도 표 측정 후 확정한다."""

    allowed_ids: tuple = (1, 2, 3, 4)
    max_hamming: int = 0                   # 비트 보정된 검출은 배제
    min_decision_margin: float = 30.0      # 실측 후 조정 필요
    min_pixel_width: float = 30.0          # 이하면 pose 를 믿을 수 없다
    min_range_m: float = 0.25              # 고정초점 한계 + 수직 FOV
    max_range_m: float = 6.0
    # 법선의 수평 성분. 태그가 눕거나 심하게 기울면 작아진다.
    # 수직으로 세운 태그는 1.0 에 가깝다.
    min_normal_horizontal: float = 0.5
    # 프레임 간 이동 속도 상한 [m/s]. 물리적으로 불가능한 점프를 배제한다.
    max_jump_speed_m_s: float = 1.5
    jump_tolerance_m: float = 0.05         # 측정 노이즈 여유
    lost_frames: int = 5                   # 연속 미검출 -> invalid


@dataclass(frozen=True)
class SteeringConfig:
    """조향 제어. 한계는 비대칭이다 (vehicle_config.h 실측)."""

    min_cdeg: int = -2869                  # 우 -28.69°
    max_cdeg: int = 1955                   # 좌 +19.55°
    kp_cross: float = 4000.0               # cross_track[m] -> cdeg
    kp_heading: float = 1200.0             # heading_error[rad] -> cdeg
    kp_yaw_hold: float = 1500.0            # yaw 오차[rad] -> cdeg (직진 유지)
    lpf_alpha: float = 0.4                 # 1차 저역 필터 (1.0 = 필터 없음)


# ── 입출력 ─────────────────────────────────────────────────

@dataclass(frozen=True)
class TagObservation:
    """한 프레임의 태그 관측.

    pose 는 **base_link 기준** 이다 (tf2 가 변환해서 넘겨준 값).
    품질 값은 `/detections` 에서 온다 — pose 는 TF 에만, 품질은 메시지에만
    있으므로 소비 노드가 타임스탬프로 조인해서 이 구조를 만든다.
    """

    tag_id: int
    stamp: float                           # 검출 시각 [s]
    tx: float                              # base_link 기준 태그 위치 [m]
    ty: float
    tz: float
    qx: float = 0.0                        # base_link 기준 태그 자세
    qy: float = 0.0
    qz: float = 0.0
    qw: float = 1.0
    decision_margin: float = 0.0
    hamming: int = 0
    pixel_width: float = 0.0


@dataclass(frozen=True)
class TagTarget:
    """제어가 소비하는 결과."""

    tag_id: int = 0
    valid: bool = False
    reason: str = 'no observation'         # invalid 사유 (진단용)
    d_x: float = 0.0                       # base_link 기준
    d_y: float = 0.0
    range_m: float = 0.0
    bearing: float = 0.0                   # atan2(d_y, d_x). + = 태그가 왼쪽
    cross_track: float = 0.0               # + = 로봇이 경로 왼쪽
    heading_error: float = 0.0             # + = 로봇이 왼쪽을 향함
    along: float = 0.0                     # 경로 방향 태그까지 거리
    normal_yaw: float = 0.0
    decision_margin: float = 0.0
    pixel_width: float = 0.0
    age_s: float = 0.0


# ── 기하 ───────────────────────────────────────────────────

def wrap_pi(a: float) -> float:
    """각도를 [-pi, pi] 로 정규화.

    정확히 ±pi 인 입력은 부동소수점 부호에 따라 +pi 또는 -pi 가 나온다. 같은
    각도이므로 제어에는 무해하다. 180° 판정(U턴 종료)은 절댓값으로 하므로
    영향받지 않는다.
    """
    return math.atan2(math.sin(a), math.cos(a))


def quat_rotate(qx: float, qy: float, qz: float, qw: float,
                vx: float, vy: float, vz: float):
    """쿼터니언으로 벡터를 회전한다. (x, y, z, w) 순서."""
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    return (vx + qw * tx + (qy * tz - qz * ty),
            vy + qw * ty + (qz * tx - qx * tz),
            vz + qw * tz + (qx * ty - qy * tx))


def tag_normal(obs: TagObservation, cfg: TagGeometryConfig = None):
    """태그 면 법선을 base_link 기준으로. (yaw, 수평성분크기) 반환.

    수평성분크기는 태그가 눕거나 기울었는지 판정하는 데 쓴다. 수직으로 세운
    태그를 정면에서 보면 1.0 에 가깝고, 바닥에 눕히면 0 에 가까워진다.
    """
    cfg = cfg or TagGeometryConfig()
    nx, ny, _nz = quat_rotate(obs.qx, obs.qy, obs.qz, obs.qw,
                              *cfg.normal_axis)
    if cfg.normal_flip:
        nx, ny = -nx, -ny
    return math.atan2(ny, nx), math.hypot(nx, ny)


def path_errors(tx: float, ty: float, normal_yaw: float):
    """태그 중심선을 경로로 보고 (cross_track, heading_error, along).

    경로 방향은 법선의 반대다 — 법선은 태그에서 로봇 쪽을 향하고, 로봇은 그
    반대로 태그를 향해 달린다.
    """
    path_heading = wrap_pi(normal_yaw + math.pi)
    dx, dy = math.cos(path_heading), math.sin(path_heading)
    # 로봇(원점) - 태그
    wx, wy = -tx, -ty
    cross_track = dx * wy - dy * wx
    heading_error = wrap_pi(-path_heading)     # 로봇 heading 은 0
    along = tx * dx + ty * dy
    return cross_track, heading_error, along


# ── 조향 ───────────────────────────────────────────────────

def clamp_steering(cdeg: float, cfg: SteeringConfig = None) -> int:
    """조향 명령을 실측 한계로 clamp. **좌우가 비대칭이다.**

    대칭 clamp 를 쓰면 한쪽에서 한계를 넘긴 명령이 나가고, STM32 가
    `COMMAND_LIMIT` 으로 **거부**한다. `CMD_DRIVE` 는 `COMMAND_RESULT` 가
    없어서 조용히 안 꺾인다. 2026-07-31 fake_stm32 시험으로 실증됨.
    """
    cfg = cfg or SteeringConfig()
    return int(round(max(cfg.min_cdeg, min(cfg.max_cdeg, cdeg))))


def steering_for_path(cross_track: float, heading_error: float,
                      cfg: SteeringConfig = None) -> int:
    """경로 추종 조향. 두 오차가 같은 방향으로 보정된다."""
    cfg = cfg or SteeringConfig()
    raw = -(cfg.kp_cross * cross_track + cfg.kp_heading * heading_error)
    return clamp_steering(raw, cfg)


def steering_for_heading_hold(yaw_error: float,
                              cfg: SteeringConfig = None) -> int:
    """직진 구간 heading 유지 조향.

    yaw_error = wrap(목표 yaw - 현재 yaw). 양수면 왼쪽으로 더 돌아야 하므로
    좌조향(양수)이 나간다.
    """
    cfg = cfg or SteeringConfig()
    return clamp_steering(cfg.kp_yaw_hold * wrap_pi(yaw_error), cfg)


def turn_steering(radius_m: float, wheelbase_m: float, left: bool,
                  cfg: SteeringConfig = None) -> int:
    """원하는 회전반경에 해당하는 조향각. 자전거 모델 tan(d) = L / R."""
    cfg = cfg or SteeringConfig()
    if radius_m <= 0.0:
        raise ValueError('radius_m 은 양수여야 한다')
    mag = math.degrees(math.atan(wheelbase_m / radius_m)) * 100.0
    return clamp_steering(mag if left else -mag, cfg)


# ── 게이트 ─────────────────────────────────────────────────

def gate(obs: TagObservation, normal_h: float,
         cfg: GateConfig = None) -> str:
    """통과하면 '', 실패하면 사유 문자열.

    ID 화이트리스트가 여기 있는 이유: `apriltag_ros` 의 `tag.ids` 는 optional
    이라 **선언하지 않은 ID 도 검출된다.** 설정만으로는 화이트리스트가 되지
    않으므로 소비 측에서 반드시 따로 걸어야 한다.
    """
    cfg = cfg or GateConfig()
    if obs.tag_id not in cfg.allowed_ids:
        return f'ID {obs.tag_id} 미등록'
    if obs.hamming > cfg.max_hamming:
        return f'hamming {obs.hamming} > {cfg.max_hamming}'
    if obs.decision_margin < cfg.min_decision_margin:
        return f'decision_margin {obs.decision_margin:.1f} 부족'
    if obs.pixel_width < cfg.min_pixel_width:
        return f'태그 폭 {obs.pixel_width:.0f}px 부족'
    r = math.hypot(obs.tx, obs.ty)
    if r < cfg.min_range_m:
        return f'너무 가까움 {r:.2f} m'
    if r > cfg.max_range_m:
        return f'너무 멂 {r:.2f} m'
    if normal_h < cfg.min_normal_horizontal:
        return f'태그가 기울었음 (법선 수평성분 {normal_h:.2f})'
    return ''


# ── 추적기 ─────────────────────────────────────────────────

class TagTracker:
    """게이트 + 속도 게이트 + 저역 필터 + 소실 판정.

    태그별로 하나씩 둔다. `update()` 를 매 프레임 호출하고, 그 프레임에 해당
    태그가 없으면 `obs=None` 으로 호출한다.
    """

    def __init__(self, tag_id: int, gate_cfg: GateConfig = None,
                 steer_cfg: SteeringConfig = None,
                 geom_cfg: TagGeometryConfig = None):
        self.tag_id = tag_id
        self.gate_cfg = gate_cfg or GateConfig()
        self.steer_cfg = steer_cfg or SteeringConfig()
        self.geom_cfg = geom_cfg or TagGeometryConfig()
        self._last = None          # 마지막 valid TagTarget
        self._last_stamp = None
        self._misses = 0
        self.rejected = {}         # 사유별 누적 (진단용)

    def _reject(self, reason: str) -> TagTarget:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1
        self._misses += 1
        if self._last is not None and self._misses <= self.gate_cfg.lost_frames:
            # 짧은 소실은 마지막 값을 유지한다. 역광에 순간 놓치는 경우가
            # 흔하고, 그때마다 invalid 로 떨구면 제어가 덜컹거린다.
            return replace(self._last, valid=True, reason=f'유지({reason})')
        self._last = None
        return TagTarget(tag_id=self.tag_id, valid=False, reason=reason)

    def update(self, obs: TagObservation = None,
               now: float = None) -> TagTarget:
        if obs is None:
            return self._reject('미검출')
        if obs.tag_id != self.tag_id:
            return self._reject(f'ID 불일치 {obs.tag_id}')

        normal_yaw, normal_h = tag_normal(obs, self.geom_cfg)
        why = gate(obs, normal_h, self.gate_cfg)
        if why:
            return self._reject(why)

        # 속도 게이트: 프레임 간 이동이 물리적으로 가능한 범위인지
        r = math.hypot(obs.tx, obs.ty)
        if self._last is not None and self._last_stamp is not None:
            dt = obs.stamp - self._last_stamp
            if dt > 0:
                limit = (self.gate_cfg.max_jump_speed_m_s * dt
                         + self.gate_cfg.jump_tolerance_m)
                if abs(r - self._last.range_m) > limit:
                    return self._reject(
                        f'점프 {abs(r - self._last.range_m):.2f} m '
                        f'> {limit:.2f} m')

        ct, he, along = path_errors(obs.tx, obs.ty, normal_yaw)
        target = TagTarget(
            tag_id=obs.tag_id, valid=True, reason='',
            d_x=obs.tx, d_y=obs.ty, range_m=r,
            bearing=math.atan2(obs.ty, obs.tx),
            cross_track=ct, heading_error=he, along=along,
            normal_yaw=normal_yaw,
            decision_margin=obs.decision_margin,
            pixel_width=obs.pixel_width,
            age_s=0.0 if now is None else max(0.0, now - obs.stamp))

        # 1차 저역 필터. pose 는 노이즈가 있어서 그대로 쓰면 조향이 떨린다.
        a = self.steer_cfg.lpf_alpha
        if self._last is not None and 0.0 < a < 1.0:
            p = self._last
            target = replace(
                target,
                d_x=a * target.d_x + (1 - a) * p.d_x,
                d_y=a * target.d_y + (1 - a) * p.d_y,
                range_m=a * target.range_m + (1 - a) * p.range_m,
                cross_track=a * target.cross_track + (1 - a) * p.cross_track,
                along=a * target.along + (1 - a) * p.along,
                heading_error=p.heading_error + a * wrap_pi(
                    target.heading_error - p.heading_error),
                bearing=p.bearing + a * wrap_pi(
                    target.bearing - p.bearing))

        self._last = target
        self._last_stamp = obs.stamp
        self._misses = 0
        return target

    def steering(self, target: TagTarget) -> int:
        """이 태그를 향한 경로 추종 조향값."""
        if not target.valid:
            return 0
        return steering_for_path(target.cross_track, target.heading_error,
                                 self.steer_cfg)
