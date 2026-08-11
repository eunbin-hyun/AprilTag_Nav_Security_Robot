# person_guard — 주행 중 사람 감지 · 안전 정지 뼈대

싸큐리티 로봇이 주행하다 앞에 사람이 들어오면 정지 신호를 내는 모듈입니다.
사전학습 YOLO 의 `person` 클래스를 그대로 쓰기 때문에 **추가 학습이 필요 없습니다**
(AI 활용 지도 P0 4번 항목).

정지 존 판정은 "프레임 하단 일정 영역에 person 박스가 들어오면 정지"라는 규칙 기반이고,
스티커 검출과 같은 confidence 3단 임계(P0 2번)를 씁니다.

---

## 1. 구조

| 파일 | 하는 일 |
|---|---|
| `config.py` | 정지 존 사각형(비율)·confidence 임계·디바운스 설정값 |
| `types.py` | `Detection` / `PersonHit` / `FrameResult` 자료 구조 |
| `zone.py` | 정지 존 겹침 계산, 3단 임계, 프레임 간 디바운스 상태기계 (순수 파이썬) |
| `detector.py` | ultralytics YOLO 껍데기. person 클래스만 필터 |
| `pipeline.py` | 이미지·영상 → 판정 → 보고서(dict) |
| `cli.py` | 명령줄 진입점 |
| `tests/` | 단위시험 33건. ultralytics·opencv 없이 돕니다 |
| `scripts/smoke_jupyter02.sh` | GPU 서버 스모크 (6절 실측을 낸 스크립트) |

의존 방향이 `zone → config` 한 갈래라, 검출기를 TensorRT 엔진으로 갈아도 판정 로직은
그대로 씁니다. 검출기 계약은 `detect(frame) -> (검출목록, 추론 밀리초, (가로, 세로))` 하나입니다.

## 2. 판정 규칙

1. YOLO 로 `person` 박스를 뽑습니다. `warn_conf` 아래 검출은 아예 버립니다.
2. 박스와 정지 존이 겹친 넓이를 **박스 넓이로 나눈 비율**이 `overlap_ratio` 이상이면 "존 안"입니다.
   박스 넓이 기준이라 멀리 있는 작은 사람도 같은 잣대로 잡힙니다.
3. 존 안 사람의 confidence 로 3단 판정을 합니다.
   - `>= stop_conf` → **STOP** (정지)
   - `warn_conf ~ stop_conf` → **SLOW** (감속·재확인)
   - `< warn_conf` → 무시
4. 한 명이라도 STOP 이면 그 프레임은 STOP 입니다.
5. 영상에서는 `stop_frames` 번 연속 STOP 이어야 실제로 멈추고, `release_frames` 번 연속
   STOP 이 아니어야 풀립니다. 한 프레임 튀는 오탐으로 로봇이 급정지하지 않게 하는 장치입니다.
   SLOW 는 늦게 걸리면 의미가 없어서 즉시 반영합니다.
   이미지 한 장을 볼 때는 디바운스를 안 탑니다.

정지 존 기본값은 `x 0.20~0.80`, `y 0.65~1.00` 입니다. 프레임 하단 35%·가로 중앙 60% 로,
로봇 진행 방향 바로 앞입니다. 카메라 각도가 정해지면 이 값을 실측으로 다시 잡아야 합니다.

## 3. 쓰는 법

```bash
# 이미지 한 장
python -m ai.person_guard --source frame.jpg --weights yolov8n.pt --json out/result.json

# GPU 서버처럼 카드가 여럿이면 물리 번호를 고정
python -m ai.person_guard --source frame.jpg --gpu-index 6

# 영상 (5프레임마다, 최대 200장)
python -m ai.person_guard --source drive.mp4 --stride 5 --max-frames 200

# 설정 파일로 (config.example.json 참고)
python -m ai.person_guard --source frame.jpg --config ai/person_guard/config.example.json
```

명령줄 옵션이 설정 파일 값을 덮습니다. 콘솔에는 요약이 찍히고, `--json` 을 주면
프레임별 판정이 기계가 읽는 형태로 저장됩니다.

파이썬에서 직접 쓸 때는 이렇게 합니다.

```python
from ai.person_guard import GuardConfig
from ai.person_guard.detector import pin_gpu
from ai.person_guard.pipeline import PersonGuard

pin_gpu("6")          # torch import 앞에서 불러야 먹습니다
guard = PersonGuard(GuardConfig(weights="yolov8n.pt"))
report = guard.run_image("frame.jpg")
print(report["summary"]["final_decision"])   # STOP / SLOW / CLEAR
```

## 4. 결과 JSON 모양

```json
{
  "source": "frame.jpg",
  "mode": "image",
  "config": { "...": "돌릴 때 쓴 설정 전부" },
  "frames": [
    {
      "frame_index": 0,
      "timestamp_ms": null,
      "decision": "STOP",
      "raw_decision": "STOP",
      "trigger_count": 1,
      "inference_ms": 33.8,
      "persons": [
        {"bbox": [280.0, 330.0, 380.0, 470.0], "conf": 0.88,
         "overlap_ratio": 1.0, "in_zone": true, "level": "stop"}
      ]
    }
  ],
  "summary": {
    "frame_count": 1, "person_total": 1, "in_zone_total": 1,
    "stop_frames": 1, "slow_frames": 0, "clear_frames": 0,
    "first_stop_frame": 0, "final_decision": "STOP", "inference_ms_avg": 33.8
  }
}
```

`raw_decision` 은 디바운스 걸기 전 값입니다. 임계를 튜닝할 때 어디서 튀는지 볼 수 있습니다.

`inference_ms` 는 `predict()` 호출 시간만 잽니다. 가중치 로드는 안 들어가지만 **CUDA 초기화가
첫 호출 안에서 일어나서 첫 장은 크게 나옵니다** (아래 실측 참고). 성능을 볼 때는 두 번째 호출부터
보세요.

## 5. 시험

```bash
python -m unittest discover -s ai/person_guard/tests -t .
```

ultralytics·opencv 를 안 물어서 아무 파이썬에서나 돕니다. 검출기는 스텁으로 갈아 끼웁니다.

GPU 서버 스모크는 `scripts/smoke_jupyter02.sh` 입니다. jupyter02 터미널에서 그대로 돌리면
단위시험 → 이미지 4장 판정 → 존 확장 대조 → 뒷정리까지 한 번에 갑니다.

## 6. GPU 서버 실측 (2026-07-28, jupyter02 · Tesla V100 6번 카드)

환경은 python39 env (Python 3.9.23 / torch 2.5.1+cu124 / ultralytics 8.2.84), 가중치는
사전학습 `yolov8n.pt`, 입력은 coco8 val 4장입니다.

| 이미지 | person | confidence | 존 겹침 | 판정 |
|---|---|---|---|---|
| 000000000036.jpg | 1명 | 0.908 | 0.335 | **STOP** |
| 000000000042.jpg | 0명 | — | — | CLEAR |
| 000000000049.jpg | 2명 | 0.472 / 0.403 | 1.00 / 1.00 | **SLOW** |
| 000000000061.jpg | 0명 | — | — | CLEAR |

- 36번은 사람이 크게 잡혀서(박스 173,159~467,639) 하단 존을 33% 물었고 confidence 도 높아 정지입니다.
  같은 이미지를 존만 화면 전체로 넓히면 겹침이 1.00 으로 올라가고 판정은 그대로 STOP 입니다.
- 49번은 두 명 다 존 안이지만 confidence 가 0.35~0.60 사이라 3단 임계가 감속으로 잡았습니다.
  설계 의도대로 "애매하면 멈추지 말고 늦추고 재확인"이 실제로 갈렸습니다.
- 단위시험 33건은 서버 python 3.9 에서도 전부 통과했습니다.
- 추론 시간은 첫 호출 **1922ms**, 두 번째부터 **9~13ms** 입니다. 첫 장이 큰 건 CUDA 초기화
  때문이고, 실제 주행 성능은 warm 값으로 봐야 합니다.
- 뒷정리 후 GPU 10장 전부 0 MiB, compute app 0건, 남은 프로세스·터미널 0 입니다.

## 7. 젯슨 배포 때 바꿀 점

지금은 GPU 서버에서 `.pt` 로 추론하는 데까지입니다. 젯슨 오린나노 실물에 올릴 때
바꿔야 할 게 네 가지입니다.

**(1) TensorRT 변환 — 젯슨 실물에서만 됩니다.**
TensorRT 엔진은 만든 기계의 GPU·드라이버·TensorRT 버전에 묶여서, GPU 서버(V100)에서
만든 엔진이 젯슨에서 안 돕니다. 젯슨 위에서 `YOLO("yolov8n.pt").export(format="engine", half=True)`
로 직접 뽑아야 합니다. 그 다음 `--weights yolov8n.engine` 으로 바꾸면 나머지는 그대로입니다.
조사 정본 실측 참고치는 FP16 60fps · INT8 63fps 입니다.

**(2) 카메라 입력 — 파일 대신 스트림.**
`pipeline.iter_video()` 가 그 자리입니다. `cv2.VideoCapture(경로)` 를 GStreamer 파이프라인
문자열(CSI 카메라)이나 장치 번호(USB 카메라)로 바꾸면 됩니다. 지금은 파일을 끝까지 읽고 멈추지만,
실서비스에서는 무한 루프로 돌면서 판정이 바뀔 때만 로봇 제어에 신호를 보내는 형태가 됩니다.
`iter_video()` 가 제너레이터라서 이 배선을 그대로 받습니다.

**(3) GPU 고정은 뺍니다.**
`pin_gpu()` 는 카드가 여럿인 GPU 서버용입니다. 젯슨은 GPU 가 하나라 부를 일이 없습니다.

**(4) 정지 존 좌표 재측정 + 모델 동거 계획.**
카메라를 로봇에 달고 나면 존 사각형을 실측으로 다시 잡아야 합니다. 사람이 "정지해야 하는 거리"에
섰을 때 박스가 어디 오는지 보고 `y_min` 을 정하는 식입니다.
그리고 조사 정본 항목 8대로, 스티커 검출 모델과 person 모델을 동시에 상시 구동하면 오린나노
8GB 통합 메모리에서 경합이 납니다. **person 은 상시, 스티커는 퇴실 게이트 이벤트에만** 트리거하는
분리 운영이 전제입니다.

## 8. 아직 안 한 것

- ByteTrack 추적·라인 크로싱 카운팅(P1 5번)은 이 모듈 범위 밖입니다. 존 침입 판정만 합니다.
- 거리 추정을 안 합니다. "화면에서 어디 있나"로만 판단해서, 카메라 각도가 바뀌면 존을 다시 잡아야 합니다.
- 백엔드로 이벤트를 보내는 배선이 없습니다. 지금은 콘솔·JSON 까지입니다.
