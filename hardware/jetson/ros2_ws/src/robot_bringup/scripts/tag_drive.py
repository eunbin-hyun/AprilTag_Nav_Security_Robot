#!/usr/bin/env python3
"""AprilTag 를 보고 STM32 에 주행 명령을 내는 단독 프로그램 (ROS 불필요).

카메라로 태그를 검출해 태그 중심선에 정렬하며 접근하고, 정지 거리에 닿으면
멈춘다. 나가는 CMD_DRIVE 를 매번 터미널에 찍는다.

UART 구조는 drive_test_suite.py 에서 검증된 것을 그대로 쓴다:
  - 이 프로세스 하나가 포트를 소유한다 (TX 스케줄러 + RX 파서)
  - 모든 송신 프레임이 하나의 SEQ 카운터를 공유하고 매 프레임 +1
  - 상태머신이 serial write 를 직접 하지 않는다
  - 어느 경로로 끝나도 CMD_STOP 을 보낸다

── 안전 3단계 ──────────────────────────────────────────────
  (기본)              STM32 미연결. 카메라만 돌고 명령은 로그만 찍는다.
  --port PORT         연결해서 텔레메트리를 읽는다. **neutral 만 송신**하고
                      주행 명령은 여전히 로그만 찍는다. 모터가 돌지 않는다.
  --port PORT --drive 실제로 주행 명령을 보낸다. 바퀴를 띄우거나 주변을
                      비운 상태에서만 쓴다.

── 사용 ────────────────────────────────────────────────────
    # 차량 없이 카메라만 (지금 상황)
    ./tag_drive.py --tag-id 2 --preview-port 8090

    # fake STM32 로 전체 루프 확인
    socat pty,raw,echo=0,link=/tmp/ttyJETSON pty,raw,echo=0,link=/tmp/ttySTM32 &
    python3 -m stm32_bridge.fake_stm32 --port /tmp/ttySTM32 &
    ./tag_drive.py --port /tmp/ttyJETSON --drive --tag-id 2

    # 실장비 (바퀴 공중)
    ./tag_drive.py --port /dev/ttyUSB0 --drive --tag-id 2

⚠ 오도메트리(yaw·주행거리)가 없으면 회전·직진유지를 못 한다. 이 프로그램은
  **태그 접근·정렬·정지**만 한다. 코너 90° 회전과 구간 거리 감시는 yaw 가
  필요하므로 route_runner(ROS) 쪽에서 다룬다.

⚠ 캘리브레이션 전이면 pose 가 근사값이다. 화각 기반 공칭 내부 파라미터를
  쓰므로 거리에 계통 오차가 남는다. 정렬 동작 확인에는 쓸 수 있다.
"""
import argparse
import math
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..', '..', 'stm32_bridge'))
sys.path.insert(0, os.path.join(_HERE, '..', '..', 'robot_perception'))

from stm32_bridge import protocol as P                          # noqa: E402
from stm32_bridge.parser import FrameParser                     # noqa: E402
from robot_perception.tag_geometry import (                     # noqa: E402
    GateConfig, SteeringConfig, TagGeometryConfig, TagObservation, TagTracker,
    clamp_steering, path_errors, steering_for_path, tag_normal,
)

try:
    import serial
except ImportError:
    serial = None

DIAG_FOV_DEG = 58.0            # Brio 100
TX_HZ = 20.0
# 검증된 광학->차량 좌표 변환 (tf2_echo 로 확인).
#   광학 x -> 차량 -y (우)   광학 y -> 차량 -z (하)   광학 z -> 차량 +x (전방)
# 카메라를 수평·정면으로 달았을 때만 성립한다. 기울여 달면 못 쓴다.
OPTICAL_TO_BASE_Q = (-0.5, 0.5, -0.5, 0.5)      # (x, y, z, w)


# ══════════════════ 카메라 ══════════════════

def nominal_intrinsics(w, h, diag_deg=DIAG_FOV_DEG):
    d = math.hypot(w, h)
    hfov = 2.0 * math.atan(math.tan(math.radians(diag_deg) / 2.0) * (w / d))
    f = (w / 2.0) / math.tan(hfov / 2.0)
    return np.array([[f, 0, w / 2.0], [0, f, h / 2.0], [0, 0, 1.0]],
                    dtype=np.float64), f


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def qrot(q, v):
    qx, qy, qz, qw = q
    vx, vy, vz = v
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    return (vx + qw * tx + (qy * tz - qz * ty),
            vy + qw * ty + (qz * tx - qx * tz),
            vz + qw * tz + (qx * ty - qy * tx))


def mat_to_quat(R):
    tr = R[0, 0] + R[1, 1] + R[2, 2]
    if tr > 0.0:
        s = math.sqrt(tr + 1.0) * 2.0
        return ((R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s,
                (R[1, 0] - R[0, 1]) / s, 0.25 * s)
    i = int(np.argmax([R[0, 0], R[1, 1], R[2, 2]]))
    if i == 0:
        s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2.0
        return (0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s,
                (R[2, 1] - R[1, 2]) / s)
    if i == 1:
        s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2.0
        return ((R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s,
                (R[0, 2] - R[2, 0]) / s)
    s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2.0
    return ((R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s,
            (R[1, 0] - R[0, 1]) / s)


class TagCamera:
    """카메라 + AprilTag 검출 + (선택) 브라우저 미리보기."""

    def __init__(self, a):
        self.a = a
        self.cap = cv2.VideoCapture(a.device, cv2.CAP_V4L2)
        if not self.cap.isOpened():
            raise SystemExit(
                f'{a.device} 를 열 수 없다.\n'
                '  - 연결 확인:  usb_map.sh\n'
                '  - 점유 확인:  fuser -v /dev/video0  (한 프로세스만 열 수 있다)')
        # MJPG 필수. YUYV 는 1280x720 에서 5 fps 뿐이다 (USB 2.0 대역폭).
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, a.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, a.height)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        self.w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.K, self.focal = nominal_intrinsics(self.w, self.h)
        self.dist = np.zeros((5, 1))
        self._set_exposure(a.device, a.exposure)

        d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
        self.det = cv2.aruco.ArucoDetector(d, cv2.aruco.DetectorParameters())
        s = a.tag_mm / 2000.0
        self.obj = np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]],
                            dtype=np.float64)

        self.tracker = TagTracker(
            a.tag_id,
            GateConfig(allowed_ids=(a.tag_id,), min_decision_margin=0.0,
                       min_pixel_width=a.min_px, min_range_m=0.15,
                       max_range_m=8.0, lost_frames=a.lost_frames),
            SteeringConfig(kp_cross=a.kp_cross, kp_heading=a.kp_heading,
                           lpf_alpha=a.lpf),
            TagGeometryConfig(normal_flip=a.normal_flip))
        self.frames = 0
        self._jpeg = None
        self._jpeg_n = 0
        self._jlock = threading.Lock()
        self._preview = a.preview_port > 0
        if self._preview:
            self._serve(a.preview_port)

    @staticmethod
    def _set_exposure(dev, exposure):
        # auto_exposure=1 을 먼저 넣어야 exposure_time_absolute 가 활성화된다.
        for args in (['-c', 'auto_exposure=1'],
                     ['-c', f'exposure_time_absolute={exposure}'],
                     ['-c', 'exposure_dynamic_framerate=0'],
                     ['-c', 'backlight_compensation=0'],
                     ['-c', 'white_balance_automatic=0']):
            subprocess.run(['v4l2-ctl', '-d', dev] + args,
                           check=False, capture_output=True)

    def read(self):
        """한 프레임 처리해서 TagTarget 을 돌려준다."""
        ok, frame = self.cap.read()
        if not ok:
            return None, None
        self.frames += 1
        corners, ids, _ = self.det.detectMarkers(
            cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        obs = None
        if ids is not None:
            for c, i in zip(corners, ids.flatten()):
                if int(i) != self.a.tag_id:
                    continue
                obs = self._observe(c.reshape(4, 2).astype(np.float64))
                break
        target = self.tracker.update(obs, now=time.monotonic())
        if self._preview:
            self._render(frame, corners, ids, target)
        return target, frame

    def _observe(self, pts):
        ok, rvec, tvec = cv2.solvePnP(self.obj, pts, self.K, self.dist,
                                      flags=cv2.SOLVEPNP_IPPE_SQUARE)
        if not ok:
            return None
        R, _ = cv2.Rodrigues(rvec)
        q_opt = mat_to_quat(R)
        t = tvec.reshape(3)
        # 광학 -> 차량. 카메라 장착 오프셋을 더한다.
        bx, by, bz = qrot(OPTICAL_TO_BASE_Q, (float(t[0]), float(t[1]),
                                              float(t[2])))
        bq = qmul(OPTICAL_TO_BASE_Q, q_opt)
        edges = [float(np.linalg.norm(pts[k] - pts[(k + 1) % 4]))
                 for k in range(4)]
        return TagObservation(
            tag_id=self.a.tag_id, stamp=time.monotonic(),
            tx=bx + self.a.cam_x, ty=by + self.a.cam_y, tz=bz + self.a.cam_z,
            qx=bq[0], qy=bq[1], qz=bq[2], qw=bq[3],
            decision_margin=100.0, hamming=0,
            pixel_width=sum(edges) / 4.0)

    # ── 미리보기 ──────────────────────────────────────────
    def _render(self, frame, corners, ids, t):
        if ids is not None:
            for c, i in zip(corners, ids.flatten()):
                pts = c.reshape(4, 2).astype(int)
                mine = int(i) == self.a.tag_id
                col = (0, 220, 0) if mine else (0, 140, 255)
                cv2.polylines(frame, [pts], True, col, 3)
                cx, cy = pts.mean(axis=0).astype(int)
                lines = [f'ID {int(i)}' + ('' if mine else ' OTHER')]
                if mine and t.valid:
                    lines += [f'along {t.along:.2f}m',
                              f'cross {t.cross_track:+.3f}m',
                              f'head {math.degrees(t.heading_error):+.1f}deg',
                              f'{t.pixel_width:.0f}px']
                for k, s in enumerate(lines):
                    for th, cc in ((4, (0, 0, 0)), (2, col)):
                        cv2.putText(frame, s, (cx - 70, cy - 45 + k * 24),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, cc, th)
        hdr = (f'{self.w}x{self.h} f={self.focal:.0f}px '
               f'tag{self.a.tag_id}={self.a.tag_mm:.0f}mm '
               f'{"LOCK" if t and t.valid else "NO TAG"}')
        for th, cc in ((4, (0, 0, 0)), (2, (255, 255, 255))):
            cv2.putText(frame, hdr, (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, cc, th)
        ok, buf = cv2.imencode('.jpg', frame,
                               [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if ok:
            with self._jlock:
                self._jpeg = buf.tobytes()
                self._jpeg_n += 1

    def _serve(self, port):
        cam = self

        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path != '/stream':
                    body = (b'<!doctype html><meta charset=utf-8>'
                            b'<title>tag_drive</title>'
                            b'<body style="margin:0;background:#111">'
                            b'<img src="/stream" style="width:100%">')
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header('Content-Type',
                                 'multipart/x-mixed-replace; boundary=fb')
                self.end_headers()
                last = -1
                try:
                    while True:
                        with cam._jlock:
                            n, j = cam._jpeg_n, cam._jpeg
                        if n == last or j is None:
                            time.sleep(0.01)
                            continue
                        last = n
                        self.wfile.write(b'--fb\r\n')
                        self.send_header('Content-Type', 'image/jpeg')
                        self.send_header('Content-Length', str(len(j)))
                        self.end_headers()
                        self.wfile.write(j)
                        self.wfile.write(b'\r\n')
                except (BrokenPipeError, ConnectionResetError):
                    pass

        srv = ThreadingHTTPServer(('0.0.0.0', port), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        ip = '<Jetson_IP>'
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
        except OSError:
            pass
        print(f'  미리보기  http://localhost:{port}   (또는 http://{ip}:{port})')

    def close(self):
        self.cap.release()


# ══════════════════ UART ══════════════════

class Link:
    """STM32 UART 소유자. drive_test_suite.py 에서 검증된 구조."""

    def __init__(self, port, baud=115200):
        if serial is None:
            raise SystemExit('pyserial 필요: python3 -m pip install pyserial')
        self.ser = serial.Serial(port, baud, timeout=0)
        # 열자마자 쌓인 낡은 프레임을 버린다. 안 그러면 옛 mcu_time 이
        # 재부팅 오탐을 만든다.
        self.ser.reset_input_buffer()
        self.parser = FrameParser()
        self.seq = P.SeqCounter()
        self._slock = threading.Lock()
        self._qlock = threading.Lock()
        self._cmd = (0, 0, False)
        self._stop = threading.Event()
        self.drive = None
        self.last_rx = None
        self.sent = 0
        threading.Thread(target=self._rx, daemon=True).start()
        threading.Thread(target=self._tx, daemon=True).start()

    def next_seq(self):
        with self._qlock:
            return self.seq.next()

    def write(self, frame):
        with self._slock:
            try:
                self.ser.write(frame)
            except serial.SerialException:
                pass

    def set_cmd(self, speed, steering, enable):
        # enable=0 인데 비영이면 STM32 가 INVALID_VALUE 로 거부한다.
        if not enable:
            speed, steering = 0, 0
        with self._qlock:
            self._cmd = (int(speed), int(steering), bool(enable))

    def _tx(self):
        period = 1.0 / TX_HZ
        nxt = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= nxt:
                with self._qlock:
                    sp, st, en = self._cmd
                self.write(P.pack_cmd_drive(self.next_seq(), sp, st, en))
                self.sent += 1
                nxt += period
                if nxt <= now:          # 밀림 보정 (캐치업 폭주 방지)
                    nxt = now + period
            time.sleep(0.002)

    def _rx(self):
        while not self._stop.is_set():
            try:
                d = self.ser.read(4096)
            except serial.SerialException:
                time.sleep(0.05)
                continue
            if d:
                for mid, _s, pl in self.parser.feed(d):
                    if mid == P.TELEMETRY_DRIVE:
                        try:
                            self.drive = P.unpack_telemetry(pl)
                            self.last_rx = time.monotonic()
                        except ValueError:
                            pass
            self.parser.check_timeout()
            time.sleep(0.002)

    @property
    def connected(self):
        return (self.last_rx is not None
                and time.monotonic() - self.last_rx < 0.3)

    def status(self):
        d = self.drive
        if not self.connected or not d:
            return 'STM32 미수신'
        blk = P.arm_blocking_faults(d['active_fault_bits'])
        return (f"{P.STATE_NAMES.get(d['drive_state'], '?'):<9} "
                f"measured={d['measured_speed_mm_s']:>+5} mm/s  "
                f"steer_cmd={d['steering_cmd_cdeg']:>+6}  "
                f"last_seq={d['last_drive_seq']:>3}  "
                f"fault={P.describe_faults(d['active_fault_bits'])}"
                + ('  ← ARM 차단' if blk else ''))

    def close(self):
        # 어느 경로로 끝나도 정지 명령을 보낸다.
        try:
            self.set_cmd(0, 0, False)
            time.sleep(0.15)
            for _ in range(3):
                self.write(P.pack_cmd_stop(self.next_seq(),
                                           P.STOP_LINK_SHUTDOWN))
                time.sleep(0.05)
        except Exception:                                   # noqa: BLE001
            pass
        self._stop.set()
        time.sleep(0.1)
        try:
            self.ser.close()
        except Exception:                                   # noqa: BLE001
            pass


# ══════════════════ 제어 ══════════════════

def decide(t, a, steer_cfg):
    """TagTarget -> (speed_mm_s, steering_cdeg, enable, 사유)."""
    if t is None or not t.valid:
        return 0, 0, False, '태그 없음 → 정지'
    if t.along <= a.stop_m:
        return 0, 0, False, f'도착 (along {t.along:.2f} <= {a.stop_m:.2f} m)'
    # 거리에 따라 감속. 정지 거리 근처에서 v_min 이 되게 한다.
    if t.along >= a.decel_m:
        speed = a.v_max
    else:
        r = (t.along - a.stop_m) / max(1e-6, a.decel_m - a.stop_m)
        speed = int(a.v_min + r * (a.v_max - a.v_min))
    steering = steering_for_path(t.cross_track, t.heading_error, steer_cfg)
    return speed, steering, True, '접근'


def gauge(v, lo, hi, width=21):
    mid = width // 2
    c = [' '] * width
    c[mid] = '|'
    if v:
        span = hi if v > 0 else -lo
        n = max(1, min(mid, int(round(abs(v) / max(1, span) * mid))))
        for k in range(1, n + 1):
            i = mid + k if v > 0 else mid - k
            if 0 <= i < width:
                c[i] = '#'
    return ''.join(c)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='AprilTag 를 보고 STM32 에 주행 명령을 내는 단독 프로그램')
    ap.add_argument('--port', default=None,
                    help='STM32 시리얼 포트. 생략하면 미연결(로그만)')
    ap.add_argument('--drive', action='store_true',
                    help='실제로 주행 명령을 보낸다. 바퀴를 띄운 상태에서만')
    ap.add_argument('--tag-id', type=int, default=2)
    ap.add_argument('--tag-mm', type=float, default=160.0,
                    help='검은 사각형 바깥 변 [mm]. 인쇄물 실측값을 넣어라')
    ap.add_argument('--device', default='/dev/video0')
    ap.add_argument('--width', type=int, default=1280)
    ap.add_argument('--height', type=int, default=720)
    ap.add_argument('--exposure', type=int, default=400)
    ap.add_argument('--preview-port', type=int, default=0)
    # 카메라 장착 오프셋. cam-z 는 2026-08-04 실측 0.215 (태그 중심 높이와 동일).
    # cam-x 는 아직 미실측 임시값.
    ap.add_argument('--cam-x', type=float, default=0.12)
    ap.add_argument('--cam-y', type=float, default=0.0)
    ap.add_argument('--cam-z', type=float, default=0.215)
    # 주행
    ap.add_argument('--v-max', type=int, default=200, help='순항 [mm/s]')
    ap.add_argument('--v-min', type=int, default=120, help='접근 [mm/s]')
    ap.add_argument('--stop-m', type=float, default=0.60)
    ap.add_argument('--decel-m', type=float, default=1.20)
    ap.add_argument('--kp-cross', type=float, default=4000.0)
    ap.add_argument('--kp-heading', type=float, default=1200.0)
    ap.add_argument('--lpf', type=float, default=0.4)
    ap.add_argument('--min-px', type=float, default=30.0)
    ap.add_argument('--lost-frames', type=int, default=5)
    ap.add_argument('--normal-flip', action='store_true',
                    help='태그 법선 부호가 반대로 나오면 켠다')
    ap.add_argument('--rate', type=float, default=4.0, help='로그 출력 [Hz]')
    a = ap.parse_args(argv)

    steer_cfg = SteeringConfig(kp_cross=a.kp_cross, kp_heading=a.kp_heading)
    a.cam_x, a.cam_y, a.cam_z = a.cam_x, a.cam_y, a.cam_z

    mode = ('실제 주행 (명령 송신)' if (a.port and a.drive)
            else 'STM32 연결·neutral 만 송신 (모터 정지)' if a.port
            else 'STM32 미연결 — 명령은 로그만')
    print('═' * 78)
    print(f'  tag_drive  ·  모드: {mode}')
    print(f'  목표 태그 ID {a.tag_id} · {a.tag_mm:.0f} mm · '
          f'정지 {a.stop_m:.2f} m · 감속시작 {a.decel_m:.2f} m')
    print(f'  속도 {a.v_min}~{a.v_max} mm/s · '
          f'조향 한계 {steer_cfg.min_cdeg}~+{steer_cfg.max_cdeg} cdeg (비대칭)')
    print(f'  카메라 오프셋 x={a.cam_x} y={a.cam_y} z={a.cam_z} m  '
          '(차량 조립 후 실측값으로 교체)')
    print('═' * 78)

    cam = TagCamera(a)
    print(f'  카메라 {cam.w}x{cam.h}  공칭 초점거리 {cam.focal:.0f} px')
    print(f'  검출 가능 거리(계산)  '
          f'60px={cam.focal * a.tag_mm / 1000 / 60:.2f} m   '
          f'40px={cam.focal * a.tag_mm / 1000 / 40:.2f} m')
    if a.port:
        print(f'  STM32 포트 {a.port}')

    link = Link(a.port) if a.port else None
    if link:
        # 링크가 살아나고 재무장될 시간을 준다 (neutral 이 기본 명령이다)
        time.sleep(1.5)
        print(f'  링크: {link.status()}')
        if not link.connected:
            print('  ⚠ 텔레메트리 미수신 — 배선·전원·VCP 설정을 확인하라.')
            print('    명령은 계속 송신되지만 STM32 상태를 알 수 없다.')
    print('\nCtrl-C 로 종료\n')

    period = 1.0 / max(0.5, a.rate)
    next_log = 0.0
    prev_reason = None
    try:
        while True:
            t, _frame = cam.read()
            speed, steering, enable, reason = decide(t, a, steer_cfg)
            steering = clamp_steering(steering, steer_cfg)

            if link:
                if a.drive:
                    link.set_cmd(speed, steering, enable)
                else:
                    link.set_cmd(0, 0, False)      # neutral 유지

            now = time.monotonic()
            if now >= next_log or reason != prev_reason:
                next_log = now + period
                prev_reason = reason
                seq_txt = f'seq≈{link.seq._v:>3}' if link else 'seq  -'
                tail = ('→ 송신' if (link and a.drive)
                        else '→ 미송신 (neutral 유지)' if link
                        else '→ 미송신 (STM32 미연결)')
                if t is not None and t.valid:
                    print(f'[{now % 10000:7.1f}s] TAG {t.tag_id}  '
                          f'along={t.along:5.2f} m  '
                          f'cross={t.cross_track:+6.3f} m  '
                          f'head={math.degrees(t.heading_error):+6.1f}°  '
                          f'{t.pixel_width:5.0f} px')
                else:
                    r = t.reason if t is not None else '프레임 없음'
                    print(f'[{now % 10000:7.1f}s] TAG -   검출 없음 ({r})')
                print(f'{"":11} CMD_DRIVE  {seq_txt}  '
                      f'speed={speed:>+5} mm/s  '
                      f'steering={steering:>+6} cdeg '
                      f'({steering / 100.0:>+6.2f}°)  '
                      f'enable={int(enable)}  {tail}')
                print(f'{"":11} 조향 [{gauge(steering, steer_cfg.min_cdeg, steer_cfg.max_cdeg)}]'
                      f'   {reason}')
                if link:
                    print(f'{"":11} STM32: {link.status()}')
                print(flush=True)
            time.sleep(0.02)
    except KeyboardInterrupt:
        print('\n종료 중...')
    finally:
        if link:
            link.close()
            print(f'  CMD_STOP 송신 완료 (총 {link.sent} 프레임)')
        cam.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())