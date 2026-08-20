"""Build a reviewed one-class YOLO dataset from the user's folder named ``1``.

The prelabel files contain every detected person.  This script keeps only the
reviewed box belonging to the intended person and leaves all other people
unlabeled, which lets the detector learn that they are not class ``1``.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


# Values are one-based line numbers in the reviewed YOLO prelabel files.
TARGET_BOX_LINE_BY_STEM = {
    "20260819-092021": 1,
    "target_person_1787042968521166011_209436": 2,
    "target_person_1787042969525158629_209452": 1,
    "target_person_1787042970529375698_209468": 1,
    "target_person_1787042971533011805_209484": 1,
    "target_person_1787042972569127967_209500": 2,
    "target_person_1787042973573399888_209516": 1,
    "target_person_1787042974577303359_209532": 1,
    "target_person_1787042975616902701_209548": 2,
    "target_person_1787042976621110692_209564": 2,
    "target_person_1787042977625001014_209580": 1,
    "target_person_1787042978628850087_209596": 1,
    "target_person_1787042987765015991_209740": 1,
    "target_person_1787042988768692680_209756": 1,
    "target_person_1787042989808812661_209772": 1,
    "target_person_1787042990813142505_209788": 1,
    "target_person_1787042991816540386_209804": 1,
    "target_person_1787042992821072963_209820": 1,
    "target_person_1787042993856644576_209836": 1,
    "target_person_1787042994860597954_209852": 1,
    "target_person_1787042995864852543_209868": 1,
    "target_person_1787042996904708401_209884": 1,
    "target_person_1787042997908483790_209900": 1,
    "target_person_1787043197627687329_213052": 1,
    "下载": 1,
    "下载 (1)": 1,
}

# Keep a late, separate moment plus one varied reference image for validation.
VALIDATION_STEMS = {
    "target_person_1787042995864852543_209868",
    "target_person_1787042996904708401_209884",
    "target_person_1787042997908483790_209900",
    "target_person_1787043197627687329_213052",
    "下载 (1)",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--prelabels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def selected_yolo_line(label_path: Path, one_based_line_number: int) -> str:
    lines = label_path.read_text(encoding="ascii").splitlines()
    try:
        line = lines[one_based_line_number - 1]
    except IndexError as error:
        raise ValueError(
            f"Box line {one_based_line_number} does not exist in: {label_path}"
        ) from error
    parts = line.split()
    if len(parts) < 5:
        raise ValueError(f"Malformed YOLO label in: {label_path}")
    # This dataset has one class. Remove the original COCO person class and confidence.
    return "0 " + " ".join(parts[1:5]) + "\n"


def main() -> None:
    args = parse_args()
    source = args.source.resolve()
    prelabels = args.prelabels.resolve()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Output directory already exists: {output}")

    manifest: list[dict[str, str]] = []
    for stem, line_number in TARGET_BOX_LINE_BY_STEM.items():
        source_files = list(source.glob(f"{stem}.*"))
        if len(source_files) != 1:
            raise SystemExit(f"Expected one source image for '{stem}', found {len(source_files)}")
        source_image = source_files[0]
        source_label = prelabels / f"{stem}.txt"
        if not source_label.is_file():
            raise SystemExit(f"Prelabel does not exist: {source_label}")

        split = "val" if stem in VALIDATION_STEMS else "train"
        image_destination = output / "images" / split / source_image.name
        label_destination = output / "labels" / split / f"{stem}.txt"
        image_destination.parent.mkdir(parents=True, exist_ok=True)
        label_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_image, image_destination)
        label_destination.write_text(
            selected_yolo_line(source_label, line_number), encoding="ascii"
        )
        manifest.append(
            {
                "source": str(source_image),
                "split": split,
                "image": str(image_destination.relative_to(output)),
                "label": str(label_destination.relative_to(output)),
                "selected_prelabel_line": str(line_number),
            }
        )

    (output / "data.yaml").write_text(
        "path: " + str(output).replace("\\", "/") + "\n"
        "train: images/train\n"
        "val: images/val\n"
        "names:\n"
        "  0: '1'\n",
        encoding="utf-8",
    )
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    train_count = sum(item["split"] == "train" for item in manifest)
    val_count = sum(item["split"] == "val" for item in manifest)
    print(f"Created {output}")
    print(f"Training images: {train_count}")
    print(f"Validation images: {val_count}")


if __name__ == "__main__":
    main()
