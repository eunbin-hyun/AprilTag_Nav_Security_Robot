"""tag_localizer_cv — 실제 카메라로 AprilTag 를 검출해 /tag/target 을 낸다.

⚠ **임시 노드다. `apriltag_ros` 가 설치되면 교체한다.**
  `cv2.aruco` 의 `DICT_APRILTAG_36h11` 을 쓴다. 검출기 구현이 apriltag C
  라이브러리와 달라서 검출률·pose 정밀도가 같지 않다. 다만 하류(tag_geometry,
  route_logic)는 완전히 동일하므로, 교체 시 이 파일의 검출부만 바뀐다.

⚠ **캘리브레이션 전이면 pose 가 근사값이다.** `camera_info_url` 을 주지 않으면
  화각에서 계산한 공칭 내부 파라미터(왜곡 0)를 쓴다. 거리·각도에 계통 오차가
  남는다. 체인 동작 확인에는 쓸 수 있고, 최종 정확도는 캘리브레이션 후
  다시 측정해야 한다.

좌표 변환은 **tf2 에 맡긴다.** 광학 좌표계 규약(z 전방/x 우/y 하)이 부호 실수가
나는 지점이라 직접 계산하지 않는다. `base_link -> camera_optical_frame` 정적
변환이 발행되어 있어야 한다:

    ros2 run tf2_ros static_transform_publisher \\
      --x 0.12 --y 0 --z 0.215 --roll -1.5708 --pitch 0 --yaw -1.5708 \\
      --frame-id base_link --child-frame-id camera_optical_frame

실행:
    ros2 run robot_perception tag_localizer_cv --ros-args \\
      -p tag_size_m:=0.160 -p width:=1280 -p height:=720 -p exposure:=400
"""
import json
import math
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

import tf2_ros
from geometry_msgs.msg import TransformStamped

from .tag_geometry import (
    GateConfig, SteeringConfig, TagGeometryConfig, TagObservation, TagTracker,
)

DIAG_FOV_DEG = 58.0            # Brio 100 사양
PREVIEW_PAGE = """<!doctype html><meta charset=utf-8>
<title>tag_localizer_cv</title>
<style>
 body{margin:0;background:#111;color:#ddd;font:14px system-ui;text-align:center}
 img{max-width:100%;height:auto;display:block;margin:0 auto}
 p{padding:6px}
</style>
<p>tag_localizer_cv &mdash; green = registered ID, orange = unregistered</p>
<img src="/stream">
"""
OPTICAL_FRAME = 'camera_optical_frame'
BASE_FRAME = 'base_link'


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return '<Jetson_IP>'


def nominal_camera_matrix(w: int, h: int, diag_deg: float = DIAG_FOV_DEG):
    """화각으로 공칭 내부 파라미터를 만든다. 캘리브레이션 전 임시값."""
    d = math.hypot(w, h)
    hfov = 2.0 * math.atan(math.tan(math.radians(diag_deg) / 2.0) * (w / d))
    f = (w / 2.0) / math.tan(hfov / 2.0)
    return np.array([[f, 0.0, w / 2.0],
                     [0.0, f, h / 2.0],
                     [0.0, 0.0, 1.0]], dtype=np.float64), f


def emit_due(now: float, last, min_dt: float) -> bool:
    """관제 화면으로 프레임을 굽고 보낼 차례인가.

    `last is None` 은 "아직 한 번도 안 보냈다" 는 뜻이고 즉시 통과한다.
    처음에 0.0 을 넣어 뒀는데, 그게 통과하는 이유가 `time.monotonic()` 이
    부팅 후 경과라 항상 크다는 **암묵적 가정**이었다. 시험이 그걸 잡았다 —
    기준시각이 0 부터 시작하는 환경에서는 첫 화면이 늦게 떴을 것이다.

    **시간 기반이다.** "N 장에 한 번" 방식(프레임 개수 modulo)은 출력 fps 가
    카메라 fps 에 비례해 흔들린다 — 카메라는 노출·USB 경합·CPU 부하로 20 에서
    12 까지 떨어질 수 있고, `% 3` 이면 그때 출력이 6.7 에서 4 로 내려가
    인터페이스 계약(5~10 fps)을 벗어난다. 시간 기반은 카메라가 흔들려도
    출력이 상한에 붙어 있는다.

    min_dt <= 0 이면 제한하지 않는다.
    """
    if last is None or min_dt <= 0.0:
        return True
    return now - last >= min_dt


class TagLocalizerCv(Node):
    def __init__(self):
        super().__init__('tag_localizer_cv')
        self.declare_parameter('device', '/dev/video0')
        self.declare_parameter('width', 1280)
        self.declare_parameter('height', 720)
        self.declare_parameter('exposure', 400)
        self.declare_parameter('tag_size_m', 0.160)
        self.declare_parameter('allowed_ids', [1, 2, 3, 4])
        self.declare_parameter('min_decision_margin', 0.0)
        self.declare_parameter('min_pixel_width', 30.0)
        self.declare_parameter('normal_flip', False)
        self.declare_parameter('publish_tf', True)
        # 카메라는 한 프로세스만 열 수 있으므로 미리보기를 이 노드가 제공한다.
        # 0 이면 비활성. 켜면 http://localhost:<port> 로 검출 오버레이를 본다.
        self.declare_parameter('preview_port', 0)
        # 관제 화면으로 나가는 JPEG 송출률 [fps]. 0 이면 제한 없음.
        # ⚠ 카메라 프레임률과 태그 인식은 건드리지 않는다 — 그걸 낮추면
        #   주행 제어 주기가 같이 느려진다. 굽는 횟수만 줄인다.
        # 프레임 개수 기반(N장에 1번)이 아니라 **시간 기반**이다. 카메라 fps 는
        # 노출·USB 경합으로 흔들리는데, 개수 기반이면 출력 fps 가 그에 비례해
        # 흔들려서 계약(5~10 fps)을 못 지킨다.
        self.declare_parameter('preview_fps', 7.0)
        # 오버레이 정보량. minimal = DETECT/NO TAG 색만, debug = 진단값 포함.
        # 진단값(초점거리·태그크기)은 거리 오차의 원인 두 개라 개발 중엔 필요하다.
        self.declare_parameter('preview_overlay', 'minimal')
        self.declare_parameter('preview_jpeg_quality', 80)
        # 송출 전 가로폭 상한 [px]. 0 이면 원본 그대로.
        # 대역폭을 줄이는 가장 큰 레버다 (720p q80 = 프레임당 약 76 KB).
        self.declare_parameter('preview_max_width', 0)

        g = self.get_parameter
        self.tag_size = g('tag_size_m').value
        self.ids = [int(i) for i in g('allowed_ids').value]
        self.publish_tf = g('publish_tf').value

        # cv2.aruco 는 decision_margin 을 주지 않는다. 그래서 기본 임계를 0
        # 으로 두고 픽셀 폭·hamming·법선으로만 게이트한다. apriltag_ros 로
        # 교체하면 decision_margin 게이트를 켠다.
        self.gate = GateConfig(
            allowed_ids=tuple(self.ids),
            min_decision_margin=g('min_decision_margin').value,
            min_pixel_width=g('min_pixel_width').value)
        self.geom = TagGeometryConfig(normal_flip=g('normal_flip').value)
        self.steer = SteeringConfig()
        self.trackers = {i: TagTracker(i, self.gate, self.steer, self.geom)
                         for i in self.ids}

        w, h = g('width').value, g('height').value
        self.cam_mtx, self.focal = nominal_camera_matrix(w, h)
        self.dist = np.zeros((5, 1), dtype=np.float64)

        dev = g('device').value
        self.cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        if not self.cap.isOpened():
            raise RuntimeError(
                f'{dev} 를 열 수 없다. 카메라는 한 프로세스만 열 수 있다.\n'
                f'  점유 확인:  fuser -v {dev}\n'
                '  이전 launch 정리:  pkill -f tag_drive.launch; '
                'pkill -f tag_localizer_cv\n'
                f'  연결 확인:  ls -l {dev}')
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        self.w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self._apply_exposure(dev, g('exposure').value)

        d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
        self.det = cv2.aruco.ArucoDetector(d, cv2.aruco.DetectorParameters())
        # 마커 로컬 3D 코너. cv2.aruco 의 코너 순서(좌상->우상->우하->좌하)에
        # 맞춘다. z=0 평면이고 법선은 +z 다 (tag_geometry 기본 규약과 일치).
        s = self.tag_size / 2.0
        self.obj = np.array([[-s, s, 0.0], [s, s, 0.0],
                             [s, -s, 0.0], [-s, -s, 0.0]], dtype=np.float64)

        self.pub = self.create_publisher(String, '/tag/target', 5)
        self.pub_diag = self.create_publisher(String, '/tag/diagnostics', 5)
        self.tf_buf = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buf, self)
        self.tf_bcast = tf2_ros.TransformBroadcaster(self)

        self._tf_warned = False
        self.frames = 0
        self.detections = 0
        self._jpeg = None
        self._jpeg_lock = threading.Lock()
        self._jpeg_n = 0
        fps = float(g('preview_fps').value)
        self._preview_min_dt = (1.0 / fps) if fps > 0.0 else 0.0
        self._preview_t = None
        self.preview_debug = (
            str(g('preview_overlay').value).lower() == 'debug')
        self.preview_quality = int(g('preview_jpeg_quality').value)
        self.preview_max_w = int(g('preview_max_width').value)
        self._overlay = {}          # tag_id -> 표시용 값
        self._start_preview(int(g('preview_port').value))
        self.create_timer(0.05, self.spin_once_camera)
        self.create_timer(2.0, self.report)
        self.get_logger().info(
            f'{self.w}x{self.h}  공칭 초점거리 {self.focal:.0f} px  '
            f'태그 {self.tag_size * 1000:.0f} mm  ID {self.ids}')
        self.get_logger().warn(
            'cv2.aruco + 공칭 내부 파라미터 사용 — pose 는 근사값이다. '
            '캘리브레이션 후 apriltag_ros 로 교체할 것')

    @staticmethod
    def _apply_exposure(dev: str, exposure: int):
        for a in (['-c', 'auto_exposure=1'],
                  ['-c', f'exposure_time_absolute={exposure}'],
                  ['-c', 'exposure_dynamic_framerate=0'],
                  ['-c', 'backlight_compensation=0'],
                  ['-c', 'white_balance_automatic=0']):
            subprocess.run(['v4l2-ctl', '-d', dev] + a,
                           check=False, capture_output=True)

    # ── 검출 → pose ────────────────────────────────────────
    def spin_once_camera(self):
        ok, frame = self.cap.read()
        if not ok:
            return
        self.frames += 1
        stamp = self.get_clock().now()
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        corners, ids, _ = self.det.detectMarkers(gray)

        seen = {}
        if ids is not None:
            for c, i in zip(corners, ids.flatten()):
                tid = int(i)
                if tid not in self.trackers:
                    continue
                pts = c.reshape(4, 2).astype(np.float64)
                obs = self._solve(tid, pts, stamp)
                if obs is not None:
                    seen[tid] = obs

        now_s = time.monotonic()
        shown = {}
        for tid, tr in self.trackers.items():
            target = tr.update(seen.get(tid), now=now_s)
            if target.valid:
                self.detections += 1
                self.pub.publish(String(data=json.dumps(
                    _target_to_dict(target))))
                shown[tid] = target
        self._overlay = shown
        # 송출 게이트. 그리기와 굽기를 함께 건너뛴다 — putText·polylines 도
        # 공짜가 아니다. frame 은 이 뒤로 쓰이지 않으므로 건너뛰어도 안전하다.
        if self._preview_on:
            if emit_due(now_s, self._preview_t, self._preview_min_dt):
                self._preview_t = now_s
                self._render(frame, corners, ids, shown)

    def _solve(self, tid: int, pts, stamp):
        """PnP 로 카메라 광학 좌표계의 태그 pose 를 구하고 base_link 로 옮긴다."""
        ok, rvec, tvec = cv2.solvePnP(
            self.obj, pts, self.cam_mtx, self.dist,
            flags=cv2.SOLVEPNP_IPPE_SQUARE)
        if not ok:
            return None
        edges = [float(np.linalg.norm(pts[k] - pts[(k + 1) % 4]))
                 for k in range(4)]
        px = sum(edges) / 4.0

        R, _ = cv2.Rodrigues(rvec)
        q = _mat_to_quat(R)
        t = tvec.reshape(3)

        # 광학 좌표계 -> base_link. tf2 가 규약 회전을 처리한다.
        if self.publish_tf:
            self._broadcast(tid, t, q, stamp)
        try:
            tf = self.tf_buf.lookup_transform(
                BASE_FRAME, OPTICAL_FRAME, rclpy.time.Time())
        except (tf2_ros.LookupException, tf2_ros.ConnectivityException,
                tf2_ros.ExtrapolationException) as e:
            if not self._tf_warned:
                self._tf_warned = True
                self.get_logger().error(
                    f'{BASE_FRAME} <- {OPTICAL_FRAME} 변환이 없다: {e}. '
                    'static_transform_publisher 를 띄워라 (docstring 참고)')
            return None
        bx, by, bz, bq = _compose(tf, t, q)
        return TagObservation(
            tag_id=tid, stamp=time.monotonic(),
            tx=bx, ty=by, tz=bz,
            qx=bq[0], qy=bq[1], qz=bq[2], qw=bq[3],
            decision_margin=100.0,          # cv2.aruco 는 제공하지 않는다
            hamming=0, pixel_width=px)

    def _broadcast(self, tid, t, q, stamp):
        m = TransformStamped()
        m.header.stamp = stamp.to_msg()
        m.header.frame_id = OPTICAL_FRAME
        m.child_frame_id = f'tag_{tid}'
        m.transform.translation.x = float(t[0])
        m.transform.translation.y = float(t[1])
        m.transform.translation.z = float(t[2])
        m.transform.rotation.x, m.transform.rotation.y = float(q[0]), float(q[1])
        m.transform.rotation.z, m.transform.rotation.w = float(q[2]), float(q[3])
        self.tf_bcast.sendTransform(m)

    # ── 미리보기 (HTTP MJPEG) ──────────────────────────────
    def _start_preview(self, port: int):
        """카메라를 이 노드가 점유하므로 미리보기도 여기서 제공한다.

        원격(VSCode/SSH)에서는 DISPLAY 가 없어 GUI 창을 못 띄운다. 브라우저로
        보게 만든다. 0 이면 비활성이고 JPEG 인코딩도 하지 않는다.
        """
        self._preview_on = port > 0
        if not self._preview_on:
            return
        node = self

        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def do_HEAD(self):
                """`curl -I` 로 살아있는지 확인할 수 있게 한다.

                이게 없으면 BaseHTTPRequestHandler 가 501 Unsupported method
                를 돌려준다 — 서비스가 정상인데 헬스체크가 실패로 보인다
                (`docs/tag_perception_상시실행_작업절차.md` §9 의 검증 명령이
                `curl -I` 다). 본문 없이 헤더만 보낸다.

                /stream 은 끝나지 않는 multipart 라 HEAD 로도 Content-Length
                를 줄 수 없다. 헤더만 주고 끊는다.
                """
                if self.path in ('/', '/index.html'):
                    body = PREVIEW_PAGE.encode()
                    self.send_response(200)
                    self.send_header('Content-Type',
                                     'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    return
                if self.path == '/stream':
                    self.send_response(200)
                    self.send_header('Cache-Control', 'no-cache, private')
                    self.send_header(
                        'Content-Type',
                        'multipart/x-mixed-replace; boundary=frameboundary')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    return
                self.send_error(404)

            def do_GET(self):
                if self.path in ('/', '/index.html'):
                    body = PREVIEW_PAGE.encode()
                    self.send_response(200)
                    self.send_header('Content-Type',
                                     'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path != '/stream':
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Cache-Control', 'no-cache, private')
                self.send_header(
                    'Content-Type',
                    'multipart/x-mixed-replace; boundary=frameboundary')
                self.end_headers()
                last = -1
                try:
                    while True:
                        with node._jpeg_lock:
                            n, jpeg = node._jpeg_n, node._jpeg
                        if n == last or jpeg is None:
                            time.sleep(0.01)
                            continue
                        last = n
                        self.wfile.write(b'--frameboundary\r\n')
                        self.send_header('Content-Type', 'image/jpeg')
                        self.send_header('Content-Length', str(len(jpeg)))
                        self.end_headers()
                        self.wfile.write(jpeg)
                        self.wfile.write(b'\r\n')
                except (BrokenPipeError, ConnectionResetError):
                    pass

        srv = ThreadingHTTPServer(('0.0.0.0', port), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.get_logger().info(
            f'미리보기: http://localhost:{port}  (또는 '
            f'http://{_local_ip()}:{port})')

    def _render(self, frame, corners, ids, shown):
        """검출 오버레이. cv2.putText 는 한글을 못 그리므로 ASCII 만 쓴다."""
        if ids is not None:
            for c, i in zip(corners, ids.flatten()):
                tid = int(i)
                pts = c.reshape(4, 2).astype(int)
                known = tid in self.trackers
                col = (0, 220, 0) if known else (0, 140, 255)
                cv2.polylines(frame, [pts], True, col, 3)
                cx, cy = pts.mean(axis=0).astype(int)
                lines = [f'ID {tid}' + ('' if known else ' UNREG')]
                t = shown.get(tid)
                if t is not None:
                    lines += [f'along {t.along:.2f}m',
                              f'cross {t.cross_track:+.3f}m',
                              f'head  {math.degrees(t.heading_error):+.1f}deg',
                              f'{t.pixel_width:.0f}px']
                for k, txt in enumerate(lines):
                    for th, cc in ((4, (0, 0, 0)), (2, col)):
                        cv2.putText(frame, txt, (cx - 70, cy - 45 + k * 24),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, cc, th)
        # 검출 여부는 **글자가 아니라 색으로** 알린다. DETECT 와 NO TAG 가 같은
        # 색이면 발표 중 힐끗 봐서는 구분이 안 된다. OpenCV 는 BGR 이다.
        lock = bool(shown)
        if self.preview_debug:
            hdr = (f'{self.w}x{self.h} f={self.focal:.0f}px '
                   f'tag={self.tag_size * 1000:.0f}mm '
                   f'{"DETECT" if lock else "NO TAG"}')
        else:
            hdr = 'DETECT' if lock else 'NO TAG'
        col = (0, 255, 0) if lock else (0, 0, 255)
        for th, cc in ((4, (0, 0, 0)), (2, col)):
            cv2.putText(frame, hdr, (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, cc, th)
        if self.preview_max_w and frame.shape[1] > self.preview_max_w:
            sc = self.preview_max_w / frame.shape[1]
            frame = cv2.resize(frame, None, fx=sc, fy=sc,
                               interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(
            '.jpg', frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), self.preview_quality])
        if ok:
            with self._jpeg_lock:
                self._jpeg = buf.tobytes()
                self._jpeg_n += 1

    def report(self):
        d = {'frames': self.frames, 'valid_targets': self.detections,
             'rejected': {str(i): dict(t.rejected)
                          for i, t in self.trackers.items() if t.rejected}}
        self.pub_diag.publish(String(data=json.dumps(d, ensure_ascii=False)))

    def destroy_node(self):
        try:
            self.cap.release()
        except Exception:                                   # noqa: BLE001
            pass
        super().destroy_node()


# ── 보조 수학 ──────────────────────────────────────────────

def _mat_to_quat(R):
    """회전행렬 -> (x, y, z, w)."""
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


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def _qrot(q, v):
    qx, qy, qz, qw = q
    vx, vy, vz = v
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    return (vx + qw * tx + (qy * tz - qz * ty),
            vy + qw * ty + (qz * tx - qx * tz),
            vz + qw * tz + (qx * ty - qy * tx))


def _compose(tf, t, q):
    """base_link <- optical 변환을 태그 pose 에 적용한다."""
    tr = tf.transform.translation
    ro = tf.transform.rotation
    fq = (ro.x, ro.y, ro.z, ro.w)
    rx, ry, rz = _qrot(fq, (float(t[0]), float(t[1]), float(t[2])))
    return (rx + tr.x, ry + tr.y, rz + tr.z, _qmul(fq, q))


def _target_to_dict(t):
    return {
        'tag_id': t.tag_id, 'valid': t.valid, 'reason': t.reason,
        'd_x': t.d_x, 'd_y': t.d_y, 'range_m': t.range_m,
        'bearing': t.bearing, 'cross_track': t.cross_track,
        'heading_error': t.heading_error, 'along': t.along,
        'normal_yaw': t.normal_yaw, 'decision_margin': t.decision_margin,
        'pixel_width': t.pixel_width, 'age_s': t.age_s,
    }


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = TagLocalizerCv()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
