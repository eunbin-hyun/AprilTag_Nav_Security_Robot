#!/usr/bin/env python3
"""카메라 실시간 미리보기를 HTTP MJPEG 스트림으로 내보낸다.

Jetson 을 원격(VSCode/SSH)으로 쓰면 DISPLAY 가 없어서 cheese·qv4l2 같은 GUI
뷰어를 띄울 수 없다. 이 스크립트는 브라우저로 볼 수 있게 해준다. 캘리브레이션
체커보드나 AprilTag 를 조준할 때 화면 없이 하는 것은 사실상 불가능하므로
이 작업에 필요하다.

    python3 camera_preview.py                    # 1280x720, 포트 8080
    python3 camera_preview.py --width 1920 --height 1080
    python3 camera_preview.py --port 8081 --device /dev/robotcam

VSCode 원격이면 포트가 자동 전달되므로 로컬 브라우저에서
    http://localhost:8080
같은 네트워크의 다른 장비면
    http://<Jetson_IP>:8080

⚠ 카메라는 한 번에 한 프로세스만 열 수 있다. 이 스크립트가 돌고 있으면
  ROS 카메라 노드나 v4l2-ctl 스트리밍이 "Device or resource busy" 로 실패한다.
  Ctrl-C 로 끄고 나서 다른 것을 실행하라.

⚠ cv2 가 장치를 열 때 포맷을 다시 설정하므로 camera_setup.sh 로 넣어둔 노출
  설정이 초기화될 수 있다. --exposure 로 여기서 직접 지정하는 편이 확실하다.
"""
import argparse
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2

BOUNDARY = 'frameboundary'


class Camera:
    """단일 캡처 스레드. 최신 JPEG 한 장만 들고 있는다.

    프레임을 큐에 쌓으면 브라우저가 느릴 때 지연이 계속 누적된다. 조준용
    화면에서는 지연이 곧 오조준이므로 최신 프레임만 유지한다.
    """

    def __init__(self, device: str, width: int, height: int, quality: int):
        self.cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        if not self.cap.isOpened():
            raise SystemExit(
                f'{device} 를 열 수 없다.\n'
                '  - 카메라 연결 확인: ls -l /dev/video*\n'
                '  - 다른 프로세스가 점유 중인지 확인: fuser -v /dev/video0')
        # MJPG 필수. YUYV 는 1280x720 에서 5 fps 밖에 안 나온다 (USB 2.0).
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        self.quality = quality

        aw = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        ah = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if (aw, ah) != (width, height):
            print(f'경고: 요청 {width}x{height} -> 실제 {aw}x{ah}', flush=True)
        print(f'카메라 열림: {device}  {aw}x{ah}', flush=True)

        self._jpeg = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._frames = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop.is_set():
            ok, frame = self.cap.read()
            if not ok:
                continue
            ok, buf = cv2.imencode(
                '.jpg', frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
            if not ok:
                continue
            with self._lock:
                self._jpeg = buf.tobytes()
                self._frames += 1

    def latest(self):
        with self._lock:
            return self._jpeg

    @property
    def frames(self):
        with self._lock:
            return self._frames

    def close(self):
        self._stop.set()
        self.cap.release()


CAMERA = None

PAGE = """<!doctype html><meta charset=utf-8>
<title>Jetson camera preview</title>
<style>
 body{margin:0;background:#111;color:#ddd;font:14px system-ui;text-align:center}
 img{max-width:100%;height:auto;display:block;margin:0 auto}
 p{padding:6px}
</style>
<p>Jetson camera preview &mdash; 새로고침하면 스트림이 다시 시작됩니다</p>
<img src="/stream">
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass                                    # 요청마다 로그를 찍으면 시끄럽다

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
        self.send_header('Age', '0')
        self.send_header('Cache-Control', 'no-cache, private')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Content-Type',
                         f'multipart/x-mixed-replace; boundary={BOUNDARY}')
        self.end_headers()
        last = -1
        try:
            while True:
                # 같은 프레임을 반복 전송하지 않는다 (대역폭·CPU 낭비)
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
            pass                                # 브라우저 탭 닫힘 — 정상


def apply_exposure(device: str, exposure: int):
    """v4l2-ctl 로 수동 노출을 건다.

    cv2 로 여는 것만으로는 노출 모드가 자동으로 남는다. auto_exposure=1 을
    먼저 넣어야 exposure_time_absolute 의 inactive 플래그가 풀린다 — 순서가
    중요하다.
    """
    for args in (['-c', 'auto_exposure=1'],
                 ['-c', f'exposure_time_absolute={exposure}'],
                 ['-c', 'exposure_dynamic_framerate=0'],
                 ['-c', 'backlight_compensation=0'],
                 ['-c', 'white_balance_automatic=0']):
        subprocess.run(['v4l2-ctl', '-d', device] + args,
                       check=False, capture_output=True)
    print(f'수동 노출 적용: exposure_time_absolute={exposure}', flush=True)


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
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--quality', type=int, default=80,
                    help='전송 JPEG 품질 1~100 (기본 80)')
    ap.add_argument('--exposure', type=int, default=None,
                    help='수동 노출값 5~2500. 지정하지 않으면 자동 노출 유지')
    args = ap.parse_args(argv)

    CAMERA = Camera(args.device, args.width, args.height, args.quality)
    if args.exposure is not None:
        apply_exposure(args.device, args.exposure)

    server = ThreadingHTTPServer(('0.0.0.0', args.port), Handler)
    server.daemon_threads = True
    print(f'\n  http://localhost:{args.port}      (VSCode 포트 전달)')
    print(f'  http://{local_ip()}:{args.port}   (같은 네트워크)')
    print('\nCtrl-C 로 종료\n', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n종료 중...')
    finally:
        server.shutdown()
        CAMERA.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
