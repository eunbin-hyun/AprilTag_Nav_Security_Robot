#!/usr/bin/env bash
# person_guard GPU 스모크 — SSAFY GPU 서버(jupyter02) 터미널에서 그대로 붙여 돌립니다.
#
# 전제
#   - ~/c207_robot/ai/person_guard/ 에 이 모듈이 올라가 있을 것
#   - ~/c207_robot/weights/yolov8n.pt, ~/c207_robot/datasets/coco8/ 이 있을 것
#   - conda activate 는 비대화형 셸에서 안 먹으므로 env 인터프리터를 절대경로로 부릅니다
#   - CUDA_VISIBLE_DEVICES 는 --gpu-index 로 넘겨서 6번 카드만 씁니다 (남의 카드 안 건드림)

set -u
ROOT="$HOME/c207_robot"
PY="$HOME/.conda/envs/python39/bin/python"
GPU="${GPU_INDEX:-6}"

cd "$ROOT" || exit 1
mkdir -p out/person_guard

echo "===== 0. 착수 전 GPU 점유 ====="
nvidia-smi --query-gpu=index,name,memory.used,utilization.gpu --format=csv,noheader | sed -n "$((GPU + 1))p"
echo "compute apps: $(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader | wc -l)"

echo
echo "===== 1. 단위시험 (ultralytics 없이 도는 판정 로직) ====="
"$PY" -m unittest discover -s ai/person_guard/tests -t . 2>&1 | tail -4

echo
echo "===== 2. 이미지 스모크 — 기본 정지 존 ====="
for IMG in datasets/coco8/images/val/*.jpg; do
  echo "--- $IMG"
  "$PY" -m ai.person_guard \
    --source "$IMG" --weights weights/yolov8n.pt --gpu-index "$GPU" \
    --json "out/person_guard/$(basename "$IMG" .jpg)_default.json"
done

echo
echo "===== 3. 존을 화면 전체로 넓히면 판정이 바뀌는지 ====="
"$PY" -m ai.person_guard \
  --source datasets/coco8/images/val/000000000036.jpg \
  --weights weights/yolov8n.pt --gpu-index "$GPU" \
  --zone 0.0 1.0 0.0 1.0 --json out/person_guard/036_fullzone.json

echo
echo "===== 4. 뒷정리 · GPU 잔재 확인 ====="
find ai -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
echo "compute apps: $(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader | wc -l)"
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader
