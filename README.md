# human_detector

Motion-gated person detection for Raspberry Pi 4 (YOLO11n on NCNN, CPU only).

Per frame:
1. Cheap motion check (160x120 gray diff against a running background, ~1 ms).
2. Motion, or a person seen in the last `--hold` s (10 s) → run the detector on 1 of every `--skip` (5) frames.
3. Person seen in the last 10 s → draw boxes and record to `recordings/YYYY-MM-DD/person_HHMMSS.mp4`
   (2 s pre-roll included, clips split at 10 min).
4. Every 10 min delete recordings older than `--retention` hours (24), and the oldest ones if free disk < 1 GB.

Measured on a Pi 4 (8 GB): detector ~104 ms at 320 px; ~10% CPU on a quiet scene, ~26% with people constantly in view.

## Setup

```bash
sudo apt install python3-opencv python3-numpy   # already present on this Pi
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
# model: models/yolo11n.ncnn.{param,bin} from https://github.com/nihui/ncnn-assets/tree/master/models
```

Pi CSI camera: `sudo apt install python3-picamera2` and use `--source picam`.

## Run

```bash
.venv/bin/python app.py --source 0                       # USB camera
.venv/bin/python app.py --source picam                   # Pi camera
.venv/bin/python app.py --source rtsp://user:pass@ip/... # IP camera
.venv/bin/python app.py --source clip.mp4 --out /tmp/rec # test on a file
.venv/bin/python app.py --help                           # all options
```

Useful knobs: `--imgsz 416` (better for small/far people, ~1.5x slower), `--skip`, `--hold`,
`--motion-area` (raise if lighting/trees cause false triggers), `--conf`.

## Run as a service

```bash
# edit --source in human-detector.service first
sudo cp human-detector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now human-detector
journalctl -u human-detector -f
```

Recordings use the `mp4v` codec (plays in VLC; not in most browsers).
