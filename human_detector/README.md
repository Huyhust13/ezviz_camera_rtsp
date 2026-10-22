# human_detector

Motion-gated person detection for Raspberry Pi 4 (YOLO11n on NCNN, CPU only).

Per frame:
1. Cheap motion check (160x120 gray diff against a running background, ~1 ms).
2. Motion, or a person seen in the last `--hold` s (10 s) → run the detector on 1 of every `--skip` (8) frames.
3. Person seen in the last 10 s → draw boxes and record to `recordings/YYYY-MM-DD/person_HHMMSS.mp4`
   (2 s pre-roll included, clips split at 10 min).
4. Every 10 min delete recordings older than `--retention` hours (24), and the oldest ones if free disk < 1 GB.

Measured on a Pi 4 (8 GB): detector ~86 ms at 320 px, ~340 ms at 640 px (the default). At 320 px the
EZVIZ room view missed a bending person almost entirely (1/58 frames) and boxed a kick scooter as a person;
at 640 px the person was found in 39/58 frames and the scooter in none. With 640 px and `--skip 8` the loop
still runs ~11 fps with people constantly in view.

## Setup

```bash
sudo apt install python3-opencv python3-numpy   # already present on this Pi
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
# model: models/yolo11n.ncnn.{param,bin} from https://github.com/nihui/ncnn-assets/tree/master/models
```

Pi CSI camera: `sudo apt install python3-picamera2` and use `--source picam`.

## Run

By default it reads the MJPEG feed of the camera server (`~/camera`, `camera-stream.service`)
at `http://127.0.0.1:8000/video_feed`, so the camera's RTSP stream is only opened and decoded once.

```bash
.venv/bin/python app.py                                  # camera server on port 8000
.venv/bin/python app.py --source 0                       # USB camera
.venv/bin/python app.py --source picam                   # Pi camera
.venv/bin/python app.py --source rtsp://user:pass@ip/... # IP camera
.venv/bin/python app.py --source clip.mp4 --out /tmp/rec # test on a file
.venv/bin/python app.py --help                           # all options
```

Each detector run also writes the boxes to `/dev/shm/human_detections.json` (`--publish`, `''` to disable);
the camera server serves them at `/detections` and the live page draws them over the video.

Useful knobs: `--imgsz 320` (~4x faster, but misses small people), `--skip`, `--hold`,
`--motion-area` (raise if lighting/trees cause false triggers), `--conf`.

## Run as a service

```bash
sudo cp human-detector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now human-detector
journalctl -u human-detector -f
```

Recordings are H.264 (`--fourcc avc1`, ~19 ms/frame on the Pi), so they play in the browser on the
camera server's `/recordings` page. A clip only becomes playable once it is closed.
