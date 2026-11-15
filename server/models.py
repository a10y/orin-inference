"""Model registry and detector backends.

Every detector exposes:
    names: list[str]                      class index -> label
    predict(frames, conf) -> list[np.ndarray]   one (N, 6) array per frame:
                                          x1, y1, x2, y2, score, class_index
"""
import collections
import gc
import threading
from dataclasses import asdict, dataclass

import numpy as np

from . import config

AGPL = "AGPL-3.0 (Ultralytics)"
BSD = "BSD-3-Clause (torchvision)"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    family: str
    backend: str  # "ultralytics" | "torchvision"
    license: str
    notes: str = ""


def _ultra(prefix, label, sizes, notes=""):
    size_names = {"n": "nano", "s": "small", "m": "medium", "l": "large", "x": "xlarge"}
    return [
        ModelSpec(f"{prefix}{s}", f"{label}-{s.upper()} ({size_names[s]})", label, "ultralytics", AGPL, notes)
        for s in sizes
    ]


SPECS: list[ModelSpec] = [
    *_ultra("yolo26", "YOLO26", "nsmlx", "Latest Ultralytics YOLO, NMS-free. COCO 80 classes."),
    *_ultra("yolo11", "YOLO11", "nsmlx", "Ultralytics YOLO11. COCO 80 classes."),
    *_ultra("yolo12", "YOLO12", "nsmlx", "Attention-centric YOLO. COCO 80 classes."),
    ModelSpec("rtdetr-l", "RT-DETR-L", "RT-DETR", "ultralytics", AGPL, "Real-time DETR transformer (Ultralytics port). COCO 80 classes."),
    ModelSpec("rtdetr-x", "RT-DETR-X", "RT-DETR", "ultralytics", AGPL, "Real-time DETR transformer (Ultralytics port). COCO 80 classes."),
    ModelSpec("fasterrcnn_resnet50_fpn_v2", "Faster R-CNN ResNet50-FPN v2", "torchvision", "torchvision", BSD, "Two-stage, accurate, slower. COCO 80 classes."),
    ModelSpec("fasterrcnn_mobilenet_v3_large_fpn", "Faster R-CNN MobileNetV3-L FPN", "torchvision", "torchvision", BSD, "Lightweight two-stage. COCO 80 classes."),
    ModelSpec("retinanet_resnet50_fpn_v2", "RetinaNet ResNet50-FPN v2", "torchvision", "torchvision", BSD, "One-stage focal-loss detector. COCO 80 classes."),
    ModelSpec("fcos_resnet50_fpn", "FCOS ResNet50-FPN", "torchvision", "torchvision", BSD, "Anchor-free one-stage. COCO 80 classes."),
    ModelSpec("ssdlite320_mobilenet_v3_large", "SSDlite320 MobileNetV3-L", "torchvision", "torchvision", BSD, "Very fast, low accuracy. COCO 80 classes."),
]
REGISTRY = {s.id: s for s in SPECS}
DEFAULT_MODEL = "yolo11m"


def describe():
    return [asdict(s) for s in SPECS]


class UltralyticsDetector:
    def __init__(self, spec: ModelSpec):
        from ultralytics import RTDETR, YOLO, settings

        settings.update({"sync": False})  # no telemetry: this is private inference
        weights = config.WEIGHTS_DIR / f"{spec.id}.pt"
        cls = RTDETR if spec.id.startswith("rtdetr") else YOLO
        self.model = cls(str(weights))  # downloads into WEIGHTS_DIR on first use
        names = self.model.names
        self.names = [names[i] for i in range(len(names))]

    def predict(self, frames, conf):
        results = self.model.predict(frames, conf=conf, device=0, verbose=False)
        out = []
        for r in results:
            b = r.boxes
            if b is None or len(b) == 0:
                out.append(np.zeros((0, 6), np.float32))
                continue
            out.append(
                np.concatenate(
                    [b.xyxy.cpu().numpy(), b.conf.cpu().numpy()[:, None], b.cls.cpu().numpy()[:, None]], axis=1
                ).astype(np.float32)
            )
        return out


class TorchvisionDetector:
    def __init__(self, spec: ModelSpec):
        import torch
        import torchvision

        self.torch = torch
        weights = torchvision.models.get_model_weights(spec.id).DEFAULT
        self.model = torchvision.models.get_model(spec.id, weights=weights).eval().cuda()
        self.transform = weights.transforms()
        self.names = list(weights.meta["categories"])

    def predict(self, frames, conf):
        torch = self.torch
        with torch.inference_mode():
            batch = [
                self.transform(torch.from_numpy(np.ascontiguousarray(f[..., ::-1])).permute(2, 0, 1).cuda())
                for f in frames
            ]
            preds = self.model(batch)
        out = []
        for p in preds:
            keep = p["scores"] >= conf
            out.append(
                torch.cat(
                    [p["boxes"][keep], p["scores"][keep, None], p["labels"][keep, None].float()], dim=1
                ).cpu().numpy().astype(np.float32)
            )
        return out


_BACKENDS = {"ultralytics": UltralyticsDetector, "torchvision": TorchvisionDetector}
_loaded: "collections.OrderedDict[str, object]" = collections.OrderedDict()
_load_lock = threading.Lock()


def get(model_id):
    """Return a loaded detector, keeping the most recently used few in GPU memory."""
    with _load_lock:
        if model_id in _loaded:
            _loaded.move_to_end(model_id)
            return _loaded[model_id]
        spec = REGISTRY[model_id]
        while len(_loaded) >= config.MAX_LOADED_MODELS:
            _loaded.popitem(last=False)
            gc.collect()
            import torch

            torch.cuda.empty_cache()
        det = _BACKENDS[spec.backend](spec)
        _loaded[model_id] = det
        return det


def loaded_ids():
    return list(_loaded)
