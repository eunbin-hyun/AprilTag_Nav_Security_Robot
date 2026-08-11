#!/usr/bin/env python3
"""AprilTag 검출 시험 — 카메라가 태그를 읽는지 확인하고 거리를 대조한다.

⚠ **이 도구는 `cv2.aruco` 를 쓴다. 최종 시스템의 `apriltag_ros` 와 다르다.**
  검출기 구현이 달라서 검출률·pose 정밀도가 같지 않다. 이 시험으로 확인되는
  것은 "카메라·광학·조명·인쇄물이 태그를 읽을 수 있는가" 와 "거리 추정이
  대략 맞는가" 다. 최종 pose 정확도는 캘리브레이션 후 apriltag_ros 로 다시
  측정해야 한다.

⚠ 카메라 캘리브레이션 전이므로 **거리는 근사값**이다. 화각으로 계산한 공칭
  초점거리를 쓴다. 실측 거리와 비교해서 오차를 확인하는 것이 이 도구의 목적
  중 하나다.

    # 브라우저 실시간 (검출 박스 오버레이)
    ./tag_detect_test.py
    → http://localhost:8080

    # 1장 찍어서 주석 달린 JPEG 저장 (VSCode 로 확인)
    ./tag_detect_test.py --snapshot ~/tagshot.jpg

    # 1080p, 태그 크기 지정, 실측 거리 대조
    ./tag_detect_test.py --width 1920 --height 1080 --tag-mm 160 --truth-m 1.0

거리 추정:
    화면상 태그 폭 p [px] = f * S / d   =>   d = f * S / p
    f = (해상도폭/2) / tan(수평화각/2)
    Brio 100: 58° 대각 -> 수평 약 52° -> f = 1325 px @1280, 1988 px @1920
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

# Brio 100 실측 사양: 58° 대각. 16:9 로 환산한 수평 화각.
DIAG_FOV_DEG = 58.0
BOUNDARY = 'frameboundary'
CAMERA = None


def horizontal_fov(diag_deg: float, w: int, h: int) -> float:
    """대각 화각에서 수평 화각을 낸다."""
    d = math.hypot(w, h)
    return 2.0 * math.atan(math.tan(math.radians(diag_deg) / 2.0) * (w / d))


def focal_px(w: int, h: int) -> float:
    return (w / 2.0) / math.tan(horizontal_fov(DIAG_FOV_DEG, w, h) / 2.0)


def apply_exposure(device: str, exposure: int):
    """auto_exposure=1 을 먼저 넣어야 exposure_time_absolute 가 활성화된다."""
    for a in (['-c', 'auto_exposure=1'],
              ['-c', f'exposure_time_absolute={exposure}'],
              ['-c', 'exposure_dynamic_framerate=0'],
              ['-c', 'backlight_compensation=0'],
              ['-c', 'white_balance_automatic=0']):
        subprocess.run(['v4l2-ctl', '-d', device] + a,
                       check=False, capture_output=True)


class Detector:
    """카메라 + AprilTag 검출. 최신 주석 프레임 한 장만 유지한다."""

    def __init__(self, args):
        self.a = args
        self.cap = cv2.VideoCapture(args.device, cv2.CAP_V4L2)
        if not self.cap.isOpened():
            raise SystemExit(
                f'{args.device} 를 열 수 없다.\n'
                '  - 연결 확인: usb_map.sh\n'
                '  - 점유 확인: fuser -v /dev/video0')
        # MJPG 필수. YUYV 는 1280x720 에서 5 fps 뿐이다 (USB 2.0 대역폭).
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        self.w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.f = focal_px(self.w, self.h)
        apply_exposure(args.device, args.exposure)

        d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
        p = cv2.aruco.DetectorParameters()
        self.det = cv2.aruco.ArucoDetector(d, p)

        self.tag_m = args.tag_mm / 1000.0
        print(f'카메라 {self.w}x{self.h}  공칭 초점거리 {self.f:.0f} px  '
              f'수평화각 {math.degrees(horizontal_fov(DIAG_FOV_DEG, self.w, self.h)):.1f}°')
        print(f'태그 {args.tag_mm:.0f} mm · 노출 {args.exposure}')
        print('검출 가능 거리(계산): '
              f'60px={self.f * self.tag_m / 60:.2f} m  '
              f'40px={self.f * self.tag_m / 40:.2f} m\n')

        self._jpeg = None
        self._lock = threading.Lock()
        self._frames = 0
        self._stop = threading.Event()
        self.stats = {}                        # id -> 검출 횟수
        self.last = []                         # 최근 검출 목록

    def detect(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        corners, ids, _ = self.det.detectMarkers(gray)
        out = []
        if ids is None:
            return out
        cx0 = self.w / 2.0
        for c, i in zip(corners, ids.flatten()):
            pts = c.reshape(4, 2)
            # 네 변 길이의 평균을 태그 폭으로 쓴다. 비스듬히 보면 변마다
            # 길이가 달라지므로 한 변만 쓰면 과소평가된다.
            edges = [float(np.linalg.norm(pts[k] - pts[(k + 1) % 4]))
                     for k in range(4)]
            width = sum(edges) / 4.0
            ctr = pts.mean(axis=0)
            dist = self.f * self.tag_m / width if width > 0 else 0.0
            # 화면 중심에서의 수평 편차 -> 대략적인 방위각
            bearing = math.degrees(math.atan2(ctr[0] - cx0, self.f))
            out.append({'id': int(i), 'width': width, 'dist': dist,
                        'bearing': bearing, 'pts': pts, 'center': ctr,
                        'skew': (max(edges) - min(edges)) / width})
        return out

    def annotate(self, frame, dets):
        for d in dets:
            pts = d['pts'].astype(int)
            known = d['id'] in self.a.ids
            col = (0, 220, 0) if known else (0, 140, 255)
            cv2.polylines(frame, [pts], True, col, 3)
            x, y = int(d['center'][0]), int(d['center'][1])
            lines = [f"ID {d['id']}" + ('' if known else ' UNREG'),
                     f"{d['width']:.0f} px",
                     f"~{d['dist']:.2f} m",
                     f"{d['bearing']:+.1f} deg"]
            if self.a.truth_m:
                err = (d['dist'] - self.a.truth_m) / self.a.truth_m * 100.0
                lines.append(f'err {err:+.1f}%')
            for k, t in enumerate(lines):
                cv2.putText(frame, t, (x - 60, y - 40 + k * 26),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4)
                cv2.putText(frame, t, (x - 60, y - 40 + k * 26),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, col, 2)
        hdr = f'{self.w}x{self.h} f={self.f:.0f}px tag={self.a.tag_mm:.0f}mm'
        if not dets:
            hdr += '   NO TAG'
        cv2.putText(frame, hdr, (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, (0, 0, 0), 4)
        cv2.putText(frame, hdr, (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, (255, 255, 255), 2)
        return frame

    def grab(self):
        ok, frame = self.cap.read()
        if not ok:
            return None, []
        dets = self.detect(frame)
        for d in dets:
            self.stats[d['id']] = self.stats.get(d['id'], 0) + 1
        self.last = dets
        return self.annotate(frame, dets), dets

    def start_stream(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        next_log = time.monotonic() + 1.0
        while not self._stop.is_set():
            frame, dets = self.grab()
            if frame is None:
                continue
            ok, buf = cv2.imencode('.jpg', frame,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok:
                with self._lock:
                    self._jpeg = buf.tobytes()
                    self._frames += 1
            now = time.monotonic()
            if now >= next_log:
                next_log = now + 1.0
                if dets:
                    for d in sorted(dets, key=lambda x: x['id']):
                        extra = ''
                        if self.a.truth_m:
                            e = (d['dist'] - self.a.truth_m)
                            extra = (f"  오차 {e * 1000:+.0f} mm "
                                     f"({e / self.a.truth_m * 100:+.1f}%)")
                        print(f"  ID {d['id']:>2}  {d['width']:>5.0f} px  "
                              f"~{d['dist']:>5.2f} m  "
                              f"{d['bearing']:>+6.1f}°  "
                              f"왜곡 {d['skew'] * 100:>4.1f}%{extra}",
                              flush=True)
                else:
                    print('  검출 없음', flush=True)

    def latest(self):
        with self._lock:
            return self._jpeg

    @property
    def frames(self):
        with self._lock:
            return self._frames

    def close(self):
        self._stop.set()
        time.sleep(0.1)
        self.cap.release()


PAGE = """<!doctype html><meta charset=utf-8>
<title>AprilTag detect test</title>
<style>
 body{margin:0;background:#111;color:#ddd;font:14px system-ui;text-align:center}
 img{max-width:100%;height:auto;display:block;margin:0 auto}
 p{padding:6px}
</style>
<p>AprilTag 검출 시험 &mdash; 초록 = 등록 ID, 주황 = 미등록 ID</p>
<img src="/stream">
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != '/stream':
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Cache-Control', 'no-cache, private')
        self.send_header('Content-Type',
                         f'multipart/x-mixed-replace; boundary={BOUNDARY}')
        self.end_headers()
        last = -1
        try:
            while True:
                n = CAMERA.frames
                if n == last:
                    continue
                last = n
                jpeg = CAMERA.latest()
                if jpeg is None:
                    continue
                self.wfile.write(f'--{BOUNDARY}\r\n'.encode())
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', str(len(jpeg)))
                self.end_headers()
                self.wfile.write(jpeg)
                self.wfile.write(b'\r\n')
        except (BrokenPipeError, ConnectionResetError):
            pass


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return '<Jetson_IP>'


def main(argv=None):
    global CAMERA
    ap = argparse.ArgumentParser()
    ap.add_argument('--device', default='/dev/video0')
    ap.add_argument('--width', type=int, default=1280)
    ap.add_argument('--height', type=int, default=720)
    ap.add_argument('--exposure', type=int, default=150)
    ap.add_argument('--tag-mm', type=float, default=160.0,
                    help='태그 검은 사각형 바깥 변 [mm]. 인쇄물 실측값을 넣어라')
    ap.add_argument('--truth-m', type=float, default=None,
                    help='실측 거리 [m]. 넣으면 추정 오차를 표시한다')
    ap.add_argument('--ids', default='1,2,3,4',
                    help='등록 ID (초록 표시). 나머지는 주황')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--snapshot', default=None,
                    help='1장 찍어 주석 JPEG 로 저장하고 종료')
    ap.add_argument('--skip', type=int, default=20,
                    help='snapshot 전에 버릴 프레임 수 (노출 안정용)')
    a = ap.parse_args(argv)
    a.ids = {int(x) for x in a.ids.split(',') if x.strip()}

    CAMERA = Detector(a)
    try:
        if a.snapshot:
            for _ in range(a.skip):
                CAMERA.cap.read()
            frame, dets = CAMERA.grab()
            if frame is None:
                raise SystemExit('프레임을 못 받았다')
            path = os.path.expanduser(a.snapshot)
            cv2.imwrite(path, frame)
            print(f'저장: {path}')
            if not dets:
                print('  검출 없음 — 태그가 화면에 있는지, 조명·거리 확인')
            for d in sorted(dets, key=lambda x: x['id']):
                print(f"  ID {d['id']:>2}  {d['width']:>5.0f} px  "
                      f"~{d['dist']:>5.2f} m  {d['bearing']:>+6.1f}°  "
                      f"왜곡 {d['skew'] * 100:.1f}%")
            return 0

        CAMERA.start_stream()
        server = ThreadingHTTPServer(('0.0.0.0', a.port), Handler)
        server.daemon_threads = True
        print(f'  http://localhost:{a.port}      (VSCode 포트 전달)')
        print(f'  http://{local_ip()}:{a.port}   (같은 네트워크)')
        print('\nCtrl-C 로 종료\n', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('\n종료 중...')
        finally:
            server.shutdown()
    finally:
        if CAMERA.stats:
            print('\n═══ 검출 누적 ═══')
            for i in sorted(CAMERA.stats):
                mark = '' if i in a.ids else '  ← 미등록'
                print(f'  ID {i:>2}: {CAMERA.stats[i]}회{mark}')
        CAMERA.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
