#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

python_bin="${CAMERA_PREVIEW_PYTHON:-/home/yundrone/yolo/.venv/bin/python}"
model_path="${CAMERA_PREVIEW_YOLO_MODEL:-/home/yundrone/yolo/models/yolo11n.pt}"

if [[ $# -eq 0 ]]; then
    set -- \
        --camera 0 \
        --width 640 \
        --height 480 \
        --fps 30 \
        --yolo \
        --yolo-model "$model_path" \
        --start-lan-stream
fi

exec "$python_bin" camera_preview.py "$@"
