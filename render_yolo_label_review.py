"""Render indexed YOLO boxes for quick annotation review."""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2


IMAGE_EXTENSIONS = {".bmp", ".jpeg", ".jpg", ".png", ".webp"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for image_path in sorted(args.images.iterdir()):
        if image_path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        image = cv2.imread(str(image_path))
        if image is None:
            print(f"Skipped unreadable image: {image_path.name}")
            continue
        height, width = image.shape[:2]
        label_path = args.labels / f"{image_path.stem}.txt"
        if label_path.exists():
            for index, line in enumerate(label_path.read_text(encoding="ascii").splitlines(), start=1):
                parts = line.split()
                if len(parts) < 5:
                    continue
                _, center_x, center_y, box_width, box_height = map(float, parts[:5])
                left = round((center_x - box_width / 2) * width)
                top = round((center_y - box_height / 2) * height)
                right = round((center_x + box_width / 2) * width)
                bottom = round((center_y + box_height / 2) * height)
                cv2.rectangle(image, (left, top), (right, bottom), (0, 255, 255), 2)
                cv2.putText(
                    image,
                    str(index),
                    (max(left, 0) + 4, max(top, 0) + 24),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 0, 255),
                    2,
                    cv2.LINE_AA,
                )
        cv2.imwrite(str(args.output / f"{image_path.stem}.jpg"), image)


if __name__ == "__main__":
    main()
