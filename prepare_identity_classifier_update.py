"""Add reviewed target screenshots to an existing identity classifier dataset."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import cv2
import numpy as np


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validation-count", type=int, default=2)
    return parser.parse_args()


def image_files(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def green_overlay_mask(image: np.ndarray) -> np.ndarray:
    """Find the green target box drawn by ``PersonIdentityPipeline``."""
    blue, green, red = cv2.split(image)
    return (
        (green >= 155)
        & (blue <= 120)
        & (red <= 150)
        & (green.astype(np.int16) - blue.astype(np.int16) >= 75)
        & (green.astype(np.int16) - red.astype(np.int16) >= 55)
    ).astype(np.uint8)


def target_box_from_overlay(image: np.ndarray) -> tuple[int, int, int, int, int]:
    """Recover the detection box from the saved green target overlay."""
    mask = green_overlay_mask(image)
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    candidates: list[tuple[int, int, int, int, int, int]] = []
    for component_id in range(1, component_count):
        left, top, width, height, area = stats[component_id]
        if width >= 25 and height >= 60 and area >= 100:
            candidates.append((component_id, left, top, width, height, area))
    if not candidates:
        raise ValueError("Could not find a green target overlay in the screenshot.")

    component_id, left, overlay_top, width, height, _ = max(
        candidates, key=lambda item: item[3] * item[4]
    )
    component = labels == component_id
    row_counts = component.sum(axis=1)
    wide_rows = np.flatnonzero(row_counts >= max(20, round(width * 0.45)))
    row_runs = np.split(wide_rows, np.where(np.diff(wide_rows) > 1)[0] + 1)
    if len(row_runs) < 2 or len(row_runs[0]) == 0 or len(row_runs[-1]) == 0:
        raise ValueError("Could not separate the target box from its label overlay.")
    top = int(row_runs[0][-1])
    bottom = int(row_runs[-1][0])

    column_counts = component[top : bottom + 1].sum(axis=0)
    vertical_columns = np.flatnonzero(
        column_counts >= max(20, round((bottom - top) * 0.35))
    )
    if len(vertical_columns) >= 2:
        left = int(vertical_columns.min())
        right = int(vertical_columns.max())
        left_presence = component[top : bottom + 1, max(0, left - 2) : left + 3].any(
            axis=1
        )
        right_presence = component[top : bottom + 1, max(0, right - 2) : right + 3].any(
            axis=1
        )
        shared_rows = np.flatnonzero(left_presence & right_presence)
        if len(shared_rows) >= 2:
            top += int(shared_rows.min())
            bottom = top + int(shared_rows.max() - shared_rows.min())
    else:
        right = left + width - 1

    if right - left < 20 or bottom - top < 50:
        raise ValueError("The green overlay does not contain a usable person box.")
    return left, top, right, bottom, overlay_top


def crop_target(image: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Remove the annotation and return a slightly padded target-person crop."""
    left, top, right, bottom, overlay_top = target_box_from_overlay(image)
    cleanup_mask = green_overlay_mask(image) * 255
    cv2.rectangle(
        cleanup_mask,
        (max(0, left - 3), max(0, overlay_top - 3)),
        (min(image.shape[1] - 1, right + 3), min(image.shape[0] - 1, top + 3)),
        255,
        thickness=-1,
    )
    cv2.rectangle(
        cleanup_mask,
        (max(0, left - 3), max(0, top - 3)),
        (min(image.shape[1] - 1, right + 3), min(image.shape[0] - 1, bottom + 3)),
        255,
        thickness=6,
    )
    clean = cv2.inpaint(image, cleanup_mask, 4, cv2.INPAINT_TELEA)
    box_width = right - left
    box_height = bottom - top
    inset_x = max(4, round(box_width * 0.06))
    inset_y = max(4, round(box_height * 0.025))
    crop_left = left + inset_x
    crop_top = top + inset_y
    crop_right = right - inset_x
    crop_bottom = bottom - inset_y
    crop = clean[crop_top:crop_bottom, crop_left:crop_right]
    if crop.size == 0:
        raise ValueError("The recovered target crop is empty.")
    return crop, (left, top, right, bottom)


def copy_base_dataset(base: Path, output: Path) -> None:
    for split in ("train", "val"):
        for class_name in ("1", "other"):
            source_directory = base / split / class_name
            if not source_directory.is_dir():
                raise ValueError(f"Missing base dataset directory: {source_directory}")
            destination_directory = output / split / class_name
            destination_directory.mkdir(parents=True, exist_ok=True)
            for image_path in image_files(source_directory):
                shutil.copy2(image_path, destination_directory / image_path.name)


def validation_indices(count: int, validation_count: int) -> set[int]:
    if count < 2:
        raise ValueError("At least two target screenshots are required.")
    validation_count = min(max(1, validation_count), count - 1)
    if validation_count == 1:
        return {count - 1}
    return {
        round(index * (count - 1) / (validation_count - 1))
        for index in range(validation_count)
    }


def main() -> None:
    args = parse_args()
    base = args.base.resolve()
    source = args.source.resolve()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Output directory already exists: {output}")
    screenshots = image_files(source)
    if len(screenshots) < 2:
        raise SystemExit("Need at least two target screenshots.")

    copy_base_dataset(base, output)
    validation = validation_indices(len(screenshots), args.validation_count)
    manifest: list[dict[str, object]] = []
    for index, screenshot_path in enumerate(screenshots):
        image = cv2.imread(str(screenshot_path))
        if image is None:
            raise SystemExit(f"Could not read: {screenshot_path}")
        crop, box = crop_target(image)
        split = "val" if index in validation else "train"
        destination = output / split / "1" / f"works_{screenshot_path.stem}.jpg"
        if not cv2.imwrite(str(destination), crop):
            raise SystemExit(f"Could not write: {destination}")
        manifest.append(
            {
                "source": str(screenshot_path),
                "split": split,
                "image": str(destination.relative_to(output)),
                "overlay_box": list(box),
            }
        )

    (output / "works_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="ascii"
    )
    for split in ("train", "val"):
        for class_name in ("1", "other"):
            count = len(image_files(output / split / class_name))
            print(f"{split}/{class_name}: {count}")


if __name__ == "__main__":
    main()
