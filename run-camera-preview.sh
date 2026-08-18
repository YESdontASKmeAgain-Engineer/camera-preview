#!/usr/bin/env bash
set -euo pipefail

cd /home/orangepi/CameraPreview

# Probing the UVC metadata node (/dev/video1) can destabilize this board's
# vendor kernel. The desktop shortcut should use the known capture node only.
if [[ $# -eq 0 ]]; then
    if [[ ${XRDP_SESSION:-0} == 1 ]]; then
        set -- --camera 0 --width 320 --height 240 --fps 1 --start-lan-stream
    else
        set -- --camera 0 --width 320 --height 240 --fps 15 --start-lan-stream
    fi
fi

{
    printf '\n[%s] Starting Camera Preview:' "$(date --iso-8601=seconds)"
    printf ' %q' /usr/bin/python3 camera_preview.py "$@"
    printf '\n'
    exec /usr/bin/python3 camera_preview.py "$@"
} >> camera-preview.log 2>&1
