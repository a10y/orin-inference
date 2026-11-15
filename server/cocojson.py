"""Streaming writer for COCO-format detection results.

Output follows the COCO object-detection dataset layout (info / categories /
images / annotations) with an extra `score` per annotation, so it loads in
pycocotools, FiftyOne, CVAT, etc. For videos, every frame is an `images` entry
with `frame_index` and `timestamp_sec`. Boxes are [x, y, width, height] in pixels.
"""
import json
import shutil

SKIP_NAMES = {"N/A", "__background__"}


class CocoWriter:
    def __init__(self, path, info, names):
        self.path = path
        self.info = info
        self.names = names
        self._img_path = path.with_suffix(".images.part")
        self._ann_path = path.with_suffix(".annotations.part")
        self._img = open(self._img_path, "w")
        self._ann = open(self._ann_path, "w")
        self._n_img = 0
        self._n_ann = 0

    def add(self, image, dets):
        image = {"id": self._n_img, **image}
        self._img.write(("," if self._n_img else "") + json.dumps(image, separators=(",", ":")))
        for x1, y1, x2, y2, score, cls in dets:
            w, h = float(x2 - x1), float(y2 - y1)
            ann = {
                "id": self._n_ann,
                "image_id": self._n_img,
                "category_id": int(cls),
                "category_name": self.names[int(cls)],
                "bbox": [round(float(x1), 2), round(float(y1), 2), round(w, 2), round(h, 2)],
                "area": round(w * h, 2),
                "score": round(float(score), 4),
                "iscrowd": 0,
            }
            self._ann.write(("," if self._n_ann else "") + json.dumps(ann, separators=(",", ":")))
            self._n_ann += 1
        self._n_img += 1

    def close(self):
        self._img.close()
        self._ann.close()
        cats = [{"id": i, "name": n} for i, n in enumerate(self.names) if n not in SKIP_NAMES]
        with open(self.path, "w") as f:
            f.write('{"info":' + json.dumps(self.info) + ',"categories":' + json.dumps(cats) + ',"images":[')
            with open(self._img_path) as part:
                shutil.copyfileobj(part, f)
            f.write('],"annotations":[')
            with open(self._ann_path) as part:
                shutil.copyfileobj(part, f)
            f.write("]}")
        self.discard_parts()

    def discard_parts(self):
        for p in (self._img_path, self._ann_path):
            p.unlink(missing_ok=True)
