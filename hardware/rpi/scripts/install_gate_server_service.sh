#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="gate-server.service"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_SRC="${REPO_DIR}/systemd/${SERVICE_NAME}"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}"

if [[ ! -x "${REPO_DIR}/.venv/bin/python" ]]; then
  echo "missing venv python: ${REPO_DIR}/.venv/bin/python" >&2
  exit 1
fi

if [[ ! -f "${REPO_DIR}/.env" ]]; then
  echo "missing .env: ${REPO_DIR}/.env" >&2
  exit 1
fi

sudo install -m 0644 "${SERVICE_SRC}" "${SERVICE_DST}"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"
sudo systemctl --no-pager --full status "${SERVICE_NAME}"
