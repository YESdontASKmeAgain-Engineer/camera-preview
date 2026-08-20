"""Rank one-person images against labeled ZH and QM reference images.

This utility uses pretrained visual features plus simple clothing-color features
to create sorted review folders. It deliberately does not create final ZH/QM
training labels: candidate matches must be checked before training.
"""

from __future__ import annotations

import argparse
import csv
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


IMAGE_EXTENSIONS = {".bmp", ".jpeg", ".jpg", ".png", ".webp"}


@dataclass(frozen=True)
class Crop:
    image_path: Path
    pixels: np.ndarray


@dataclass(frozen=True)
class Reference:
    class_id: int
    name: str
    crop: Crop


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-size", type=int, default=320)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--device", default="0")
    parser.add_argument("--feature-model", choices=("yolo", "resnet50"), default="yolo")
    parser.add_argument("--resnet-weights", type=Path)
    parser.add_argument("--color-weight", type=float, default=0.2)
    return parser.parse_args()


def read_names(path: Path) -> list[str]:
    names = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    names = [name for name in names if name]
    if len(names) < 2:
        raise ValueError(f"Expected at least two class names in: {path}")
    return names


def read_boxes(path: Path) -> list[tuple[int, float, float, float, float]]:
    boxes: list[tuple[int, float, float, float, float]] = []
    for line in path.read_text(encoding="ascii").splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue
        class_id, x_center, y_center, width, height = parts
        boxes.append(
            (int(class_id), float(x_center), float(y_center), float(width), float(height))
        )
    return boxes


def crop_box(image_path: Path, box: tuple[int, float, float, float, float]) -> Crop:
    image = cv2.imread(str(image_path))
    if image is None:
        raise ValueError(f"Could not read image: {image_path}")
    _, x_center, y_center, width, height = box
    image_height, image_width = image.shape[:2]
    left = max(0, round((x_center - width / 2) * image_width))
    top = max(0, round((y_center - height / 2) * image_height))
    right = min(image_width, round((x_center + width / 2) * image_width))
    bottom = min(image_height, round((y_center + height / 2) * image_height))
    pixels = image[top:bottom, left:right]
    if pixels.size == 0:
        raise ValueError(f"Invalid YOLO box in: {image_path}")
    return Crop(image_path=image_path, pixels=pixels)


def crop_for_image(image_path: Path, label_path: Path) -> Crop:
    boxes = read_boxes(label_path)
    if len(boxes) != 1:
        raise ValueError(f"Expected exactly one labeled person in: {label_path}")
    return crop_box(image_path, boxes[0])


def batches(items: list[Crop], size: int) -> Iterator[list[Crop]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def normalized(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector if norm == 0.0 else vector / norm


def color_features(image: np.ndarray) -> np.ndarray:
    height, width = image.shape[:2]
    left = round(width * 0.15)
    right = max(left + 1, round(width * 0.85))
    center = image[:, left:right]
    upper = center[round(height * 0.12) : max(round(height * 0.12) + 1, round(height * 0.6))]
    lower = center[round(height * 0.6) : max(round(height * 0.6) + 1, round(height * 0.95))]
    descriptors: list[np.ndarray] = []
    for region in (center, upper, lower):
        hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
        histogram = cv2.calcHist([hsv], [0, 1, 2], None, [12, 4, 4], [0, 180, 0, 256, 0, 256])
        descriptors.append(normalized(histogram.flatten()))
    return normalized(np.concatenate(descriptors))


def extract_embeddings(model, crops: list[Crop], args: argparse.Namespace) -> np.ndarray:
    vectors: list[np.ndarray] = []
    for group in batches(crops, args.batch_size):
        embeddings = model.embed(
            [crop.pixels for crop in group],
            stream=True,
            imgsz=args.image_size,
            batch=len(group),
            device=args.device,
            embed=[10, 16, 22],
            verbose=False,
        )
        vectors.extend(normalized(embedding.detach().float().cpu().numpy()) for embedding in embeddings)
    if len(vectors) != len(crops):
        raise RuntimeError("YOLO returned an unexpected number of embeddings.")
    return np.stack(vectors)


def resize_and_center_crop(image: np.ndarray, size: int = 224) -> np.ndarray:
    height, width = image.shape[:2]
    scale = 256 / min(height, width)
    resized = cv2.resize(
        image,
        (round(width * scale), round(height * scale)),
        interpolation=cv2.INTER_LINEAR,
    )
    resized_height, resized_width = resized.shape[:2]
    top = (resized_height - size) // 2
    left = (resized_width - size) // 2
    return resized[top : top + size, left : left + size]


def extract_resnet_embeddings(crops: list[Crop], args: argparse.Namespace) -> np.ndarray:
    import torch
    from torchvision.models import resnet50

    assert args.resnet_weights is not None
    state_dict = torch.load(args.resnet_weights, map_location="cpu", weights_only=True)
    model = resnet50(weights=None)
    model.load_state_dict(state_dict)
    model.fc = torch.nn.Identity()
    device = torch.device(
        "cpu"
        if str(args.device).lower() == "cpu" or not torch.cuda.is_available()
        else f"cuda:{args.device}"
    )
    model.to(device).eval()
    mean = torch.tensor((0.485, 0.456, 0.406), device=device).view(1, 3, 1, 1)
    std = torch.tensor((0.229, 0.224, 0.225), device=device).view(1, 3, 1, 1)
    vectors: list[np.ndarray] = []
    with torch.inference_mode():
        for group in batches(crops, args.batch_size):
            batch = []
            for crop in group:
                pixels = resize_and_center_crop(crop.pixels)
                rgb = cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB)
                batch.append(torch.from_numpy(rgb).permute(2, 0, 1).float() / 255.0)
            inputs = torch.stack(batch).to(device)
            features = model((inputs - mean) / std)
            vectors.extend(normalized(vector.cpu().numpy()) for vector in features)
    return np.stack(vectors)


def load_references(root: Path, names: list[str]) -> list[Reference]:
    images_dir = root / "images" / "raw"
    labels_dir = root / "labels" / "raw"
    references: list[Reference] = []
    for label_path in sorted(labels_dir.glob("*.txt")):
        boxes = read_boxes(label_path)
        if len(boxes) != 1:
            raise ValueError(f"Expected one reference box in: {label_path}")
        class_id = boxes[0][0]
        if class_id >= len(names):
            raise ValueError(f"Unknown class ID {class_id} in: {label_path}")
        image_path = images_dir / f"{label_path.stem}.jpg"
        references.append(
            Reference(class_id=class_id, name=names[class_id], crop=crop_box(image_path, boxes[0]))
        )
    if {reference.name for reference in references} != set(names):
        raise ValueError("Each class must have exactly one labeled reference image.")
    return references


def main() -> None:
    args = parse_args()
    if args.image_size <= 0 or args.batch_size <= 0:
        raise SystemExit("--image-size and --batch-size must be positive.")
    if not 0.0 <= args.color_weight <= 1.0:
        raise SystemExit("--color-weight must be between 0 and 1.")

    images_dir = args.images.resolve()
    labels_dir = args.labels.resolve()
    reference_root = args.references.resolve()
    output_root = args.output.resolve()
    if output_root.exists():
        raise SystemExit(f"Output directory already exists: {output_root}")
    if args.feature_model == "yolo":
        if args.model is None or not args.model.is_file():
            raise SystemExit("--model must point to a YOLO model when --feature-model=yolo.")
    elif args.resnet_weights is None or not args.resnet_weights.is_file():
        raise SystemExit(
            "--resnet-weights must point to ResNet50 weights when --feature-model=resnet50."
        )

    names = read_names(reference_root / "classes.txt")
    references = load_references(reference_root, names)
    crops: list[Crop] = []
    for image_path in sorted(images_dir.iterdir()):
        if image_path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        label_path = labels_dir / f"{image_path.stem}.txt"
        if not label_path.exists() or label_path.stat().st_size == 0:
            continue
        crops.append(crop_for_image(image_path, label_path))
    if not crops:
        raise SystemExit("No one-person images with non-empty labels were found.")

    all_crops = [reference.crop for reference in references] + crops
    if args.feature_model == "yolo":
        from ultralytics import YOLO

        assert args.model is not None
        embeddings = extract_embeddings(YOLO(str(args.model)), all_crops, args)
    else:
        embeddings = extract_resnet_embeddings(all_crops, args)
    reference_embeddings = embeddings[: len(references)]
    crop_embeddings = embeddings[len(references) :]
    reference_colors = np.stack([color_features(reference.crop.pixels) for reference in references])
    crop_colors = np.stack([color_features(crop.pixels) for crop in crops])

    output_root.mkdir(parents=True)
    records: list[dict[str, object]] = []
    for crop, embedding, color in zip(crops, crop_embeddings, crop_colors, strict=True):
        embedding_scores = reference_embeddings @ embedding
        color_scores = reference_colors @ color
        scores = embedding_scores * (1.0 - args.color_weight) + color_scores * args.color_weight
        ranked_indices = np.argsort(scores)[::-1]
        winner_index = int(ranked_indices[0])
        runner_up_score = float(scores[ranked_indices[1]])
        records.append(
            {
                "source": crop.image_path,
                "predicted_name": references[winner_index].name,
                "score": float(scores[winner_index]),
                "margin": float(scores[winner_index]) - runner_up_score,
                **{
                    f"{reference.name}_score": float(scores[index])
                    for index, reference in enumerate(references)
                },
            }
        )

    records.sort(key=lambda record: (str(record["predicted_name"]), -float(record["score"])))
    for name in names:
        (output_root / name / "images").mkdir(parents=True)
    for index, record in enumerate(records, start=1):
        name = str(record["predicted_name"])
        source = Path(record["source"])
        destination = output_root / name / "images" / f"{index:04d}__{source.name}"
        shutil.copy2(source, destination)

    report_path = output_root / "candidate_scores.csv"
    with report_path.open("w", encoding="utf-8", newline="") as report_file:
        fields = ["source", "predicted_name", "score", "margin"] + [
            f"{name}_score" for name in names
        ]
        writer = csv.DictWriter(report_file, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({key: record[key] for key in fields})

    for name in names:
        matched = [record for record in records if record["predicted_name"] == name]
        print(f"{name}: {len(matched)} candidates")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
