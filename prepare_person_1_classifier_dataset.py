"""Create a balanced target-vs-other crop dataset for YOLO classification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2

from prepare_person_1_dataset import TARGET_BOX_LINE_BY_STEM, VALIDATION_STEMS


# Full-body boxes for another person visible beside the target in the early scene.
OTHER_BOX_LINES_BY_STEM = {
    "target_person_1787042968521166011_209436": 1,
    "target_person_1787042969525158629_209452": 2,
    "target_person_1787042970529375698_209468": 2,
    "target_person_1787042971533011805_209484": 2,
    "target_person_1787042972569127967_209500": 1,
    "target_person_1787042973573399888_209516": 2,
    "target_person_1787042974577303359_209532": 2,
    "target_person_1787042975616902701_209548": 1,
    "target_person_1787042976621110692_209564": 1,
    "target_person_1787042977625001014_209580": 2,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--prelabels", type=Path, required=True)
    parser.add_argument("--other-images", type=Path, required=True)
    parser.add_argument("--other-labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--other-limit", type=int, default=150)
    parser.add_argument("--target-repeat", type=int, default=6)
    return parser.parse_args()


def read_box(label_path: Path, one_based_line_number: int) -> tuple[float, float, float, float]:
    lines = label_path.read_text(encoding="ascii").splitlines()
    try:
        parts = lines[one_based_line_number - 1].split()
    except IndexError as error:
        raise ValueError(f"Missing box line in: {label_path}") from error
    if len(parts) < 5:
        raise ValueError(f"Malformed YOLO label in: {label_path}")
    return tuple(float(value) for value in parts[1:5])  # type: ignore[return-value]


def crop_person(image_path: Path, box: tuple[float, float, float, float]):
    image = cv2.imread(str(image_path))
    if image is None:
        raise ValueError(f"Could not read: {image_path}")
    center_x, center_y, width, height = box
    image_height, image_width = image.shape[:2]
    pad_x = width * 0.08
    pad_y = height * 0.05
    left = max(0, round((center_x - width / 2 - pad_x) * image_width))
    top = max(0, round((center_y - height / 2 - pad_y) * image_height))
    right = min(image_width, round((center_x + width / 2 + pad_x) * image_width))
    bottom = min(image_height, round((center_y + height / 2 + pad_y) * image_height))
    crop = image[top:bottom, left:right]
    if crop.size == 0:
        raise ValueError(f"Invalid box in: {image_path}")
    return crop


def save_crop(crop, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(destination), crop):
        raise OSError(f"Could not write: {destination}")


def main() -> None:
    args = parse_args()
    if args.other_limit < 10 or args.target_repeat < 1:
        raise SystemExit("--other-limit must be at least 10 and --target-repeat must be positive.")

    source = args.source.resolve()
    prelabels = args.prelabels.resolve()
    other_images = args.other_images.resolve()
    other_labels = args.other_labels.resolve()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Output directory already exists: {output}")

    manifest: list[dict[str, str]] = []
    for stem, line_number in TARGET_BOX_LINE_BY_STEM.items():
        source_files = list(source.glob(f"{stem}.*"))
        if len(source_files) != 1:
            raise SystemExit(f"Expected exactly one source image for: {stem}")
        crop = crop_person(source_files[0], read_box(prelabels / f"{stem}.txt", line_number))
        split = "val" if stem in VALIDATION_STEMS else "train"
        repeats = 1 if split == "val" else args.target_repeat
        for repeat in range(repeats):
            destination = output / split / "1" / f"{stem}__{repeat + 1:02d}.jpg"
            save_crop(crop, destination)
            manifest.append(
                {
                    "class": "1",
                    "split": split,
                    "source": str(source_files[0]),
                    "image": str(destination.relative_to(output)),
                }
            )

    other_pairs = []
    for image_path in sorted(other_images.glob("*.jpg")):
        label_path = other_labels / f"{image_path.stem}.txt"
        if label_path.is_file() and label_path.stat().st_size > 0:
            other_pairs.append((image_path, label_path))
    if len(other_pairs) < args.other_limit:
        raise SystemExit(f"Need {args.other_limit} other-person images, found {len(other_pairs)}")

    # Spread samples through the original capture instead of taking one adjacent burst.
    indices = [round(index * (len(other_pairs) - 1) / (args.other_limit - 1)) for index in range(args.other_limit)]
    selected_others = [other_pairs[index] for index in indices]
    validation_count = max(20, round(args.other_limit * 0.2))
    for index, (image_path, label_path) in enumerate(selected_others):
        crop = crop_person(image_path, read_box(label_path, 1))
        split = "val" if index >= len(selected_others) - validation_count else "train"
        destination = output / split / "other" / f"{image_path.stem}.jpg"
        save_crop(crop, destination)
        manifest.append(
            {
                "class": "other",
                "split": split,
                "source": str(image_path),
                "image": str(destination.relative_to(output)),
            }
        )

    # Add difficult negatives from the exact same camera scene.
    for index, (stem, line_number) in enumerate(OTHER_BOX_LINES_BY_STEM.items()):
        source_files = list(source.glob(f"{stem}.*"))
        if len(source_files) != 1:
            raise SystemExit(f"Expected exactly one source image for: {stem}")
        crop = crop_person(source_files[0], read_box(prelabels / f"{stem}.txt", line_number))
        split = "val" if index >= len(OTHER_BOX_LINES_BY_STEM) - 2 else "train"
        destination = output / split / "other" / f"same_scene__{stem}.jpg"
        save_crop(crop, destination)
        manifest.append(
            {
                "class": "other",
                "split": split,
                "source": str(source_files[0]),
                "image": str(destination.relative_to(output)),
            }
        )

    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for split in ("train", "val"):
        for class_name in ("1", "other"):
            count = len(list((output / split / class_name).glob("*.jpg")))
            print(f"{split}/{class_name}: {count}")


if __name__ == "__main__":
    main()
