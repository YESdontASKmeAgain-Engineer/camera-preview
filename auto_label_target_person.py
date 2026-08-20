"""Create conservative YOLO pre-labels for one-person camera samples.

Images with exactly one detected person receive class 0 (target_person).
Images with no detected person receive an empty label as a negative sample.
Images containing multiple people are left unlabeled and written to a review
report, because a person detector cannot determine which person is the target.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator


IMAGE_EXTENSIONS = {".bmp", ".jpeg", ".jpg", ".png", ".webp"}
TARGET_CLASS_ID = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--confidence", type=float, default=0.35)
    parser.add_argument("--image-size", type=int, default=640)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--device", default="0")
    return parser.parse_args()


def chunks(items: list[Path], size: int) -> Iterator[list[Path]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def label_path_for(image_path: Path, images_dir: Path, labels_dir: Path) -> Path:
    return labels_dir / image_path.relative_to(images_dir).with_suffix(".txt")


def write_box_label(label_path: Path, box: list[float]) -> None:
    x_center, y_center, width, height = box
    label_path.parent.mkdir(parents=True, exist_ok=True)
    label_path.write_text(
        f"{TARGET_CLASS_ID} {x_center:.6f} {y_center:.6f} {width:.6f} {height:.6f}\n",
        encoding="ascii",
    )


def write_empty_label(label_path: Path) -> None:
    label_path.parent.mkdir(parents=True, exist_ok=True)
    label_path.write_text("", encoding="ascii")


def main() -> None:
    args = parse_args()
    if not 0.0 <= args.confidence <= 1.0:
        raise SystemExit("--confidence must be between 0 and 1.")
    if args.image_size <= 0 or args.batch_size <= 0:
        raise SystemExit("--image-size and --batch-size must be positive.")

    images_dir = args.images.resolve()
    labels_dir = args.labels.resolve()
    report_path = args.report.resolve()
    if not images_dir.is_dir():
        raise SystemExit(f"Image directory does not exist: {images_dir}")
    if not args.model.is_file():
        raise SystemExit(f"YOLO model does not exist: {args.model}")

    images = sorted(
        path for path in images_dir.rglob("*") if path.suffix.lower() in IMAGE_EXTENSIONS
    )
    if not images:
        raise SystemExit(f"No supported images found in: {images_dir}")

    pending = [
        image
        for image in images
        if not label_path_for(image, images_dir, labels_dir).exists()
    ]
    counts: Counter[str] = Counter(
        total_images=len(images),
        kept_existing=len(images) - len(pending),
    )
    ambiguous: list[dict[str, object]] = []

    from ultralytics import YOLO

    model = YOLO(str(args.model))
    for batch_number, batch in enumerate(chunks(pending, args.batch_size), start=1):
        results = model.predict(
            source=[str(path) for path in batch],
            conf=args.confidence,
            imgsz=args.image_size,
            device=args.device,
            classes=[0],
            batch=len(batch),
            stream=True,
            verbose=False,
        )
        for result in results:
            image_path = Path(result.path).resolve()
            label_path = label_path_for(image_path, images_dir, labels_dir)
            boxes = result.boxes
            people_count = 0 if boxes is None else len(boxes)
            if people_count == 1:
                write_box_label(label_path, boxes.xywhn[0].cpu().tolist())
                counts["labeled"] += 1
            elif people_count == 0:
                write_empty_label(label_path)
                counts["negative"] += 1
            else:
                counts["needs_review"] += 1
                ambiguous.append(
                    {
                        "image": str(image_path.relative_to(images_dir)),
                        "detected_people": people_count,
                    }
                )

        completed = min(batch_number * args.batch_size, len(pending))
        print(
            f"Processed {completed}/{len(pending)}: "
            f"labeled={counts['labeled']}, negative={counts['negative']}, "
            f"needs_review={counts['needs_review']}"
        )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "images_directory": str(images_dir),
                "labels_directory": str(labels_dir),
                "model": str(args.model.resolve()),
                "confidence": args.confidence,
                "image_size": args.image_size,
                "counts": dict(counts),
                "needs_review": ambiguous,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
