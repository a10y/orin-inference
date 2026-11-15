# Orin Inference

A private object-detection service for the AGX Orin. Upload images or videos, pick a
model and output format, and track job progress in the browser.

- **URL:** `http://<orin-hostname>:8765` (or the Tailscale IP). It is reachable **only over Tailscale**.
- **API docs:** `/api/docs`

## Install

Requires a Jetson running JetPack 6 (CUDA 12.6), with [Tailscale](https://tailscale.com) installed and logged in.

```sh
git clone <this repo> ~/orin-inference && cd ~/orin-inference
uv venv -p 3.10 .venv && . .venv/bin/activate
uv pip install --index-url https://pypi.jetson-ai-lab.io/jp6/cu126 "torch==2.8.0" "torchvision==0.23.0"
uv pip install -r requirements.txt

mkdir -p ~/.config/systemd/user
cp orin-inference.service ~/.config/systemd/user/
loginctl enable-linger "$USER"        # start at boot without a login session
systemctl --user daemon-reload
systemctl --user enable --now orin-inference
```

Then open `http://<orin-hostname>:8765` from any device on your tailnet.

## Layout

```
~/orin-inference/
  run.sh                 launcher: waits for Tailscale and binds to its IP only
  orin-inference.service systemd user unit (copied to ~/.config/systemd/user/)
  requirements.txt       Python dependencies (install torch from the Jetson index first)
  infer.env              optional overrides (INFER_PORT, INFER_MAX_UPLOAD_GB, INFER_RETENTION_DAYS, INFER_BATCH)
  server/                FastAPI app, worker, model backends, static frontend
  data/jobs.db           job database (SQLite)
  data/jobs/<id>/        upload and outputs for each job
  data/weights/          downloaded model weights
  data/server.log        service log
```

## Operations

```sh
systemctl --user status orin-inference
systemctl --user restart orin-inference
tail -f ~/orin-inference/data/server.log
```

The service runs under systemd with user lingering enabled, so it starts at boot without
anyone logged in and restarts after crashes. If the service restarts, jobs that were
running go back into the queue and start over. Finished jobs are deleted after
`INFER_RETENTION_DAYS` (default 14).

## Security model

- Binds only to the Tailscale IPv4 address, so it isn't on the LAN or the public internet.
- Rejects requests whose `Host` header isn't the Tailscale IP or MagicDNS name, which
  blocks DNS-rebinding attacks.
- Requests that change anything must include the `X-Infer-Client: 1` header, and
  cross-origin requests are rejected. This stops other websites open in a tailnet
  user's browser from submitting jobs.
- There is no login: anyone on your tailnet can use it. To restrict access to particular
  users or devices, use Tailscale ACLs.

## API

```sh
# Submit (raw file body; options go in the query string)
curl -H 'X-Infer-Client: 1' --data-binary @clip.mp4 \
  'http://<orin-hostname>:8765/api/jobs?filename=clip.mp4&model=yolo11m&output=media&conf=0.35'
curl http://<orin-hostname>:8765/api/jobs/<id>                    # status and progress
curl -OJ http://<orin-hostname>:8765/api/jobs/<id>/files/<name>   # download
```

`output=json` produces COCO-format results (`info`, `categories`, `images`, and
`annotations` with `bbox` as [x, y, w, h] in pixels plus `score`). For video, each frame is
an `images` entry with `frame_index` and `timestamp_sec`.

## Models & licenses

The Ultralytics models (YOLO26, YOLO11, YOLO12, RT-DETR) are AGPL-3.0. The torchvision models
(Faster R-CNN, RetinaNet, FCOS, SSDlite) are BSD-3. All are pretrained on COCO's 80 classes.
Weights are downloaded the first time each model is used.

## License

AGPL-3.0, matching the Ultralytics dependency. See [LICENSE](LICENSE).
