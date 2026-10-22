# home-camera

Home security camera on a Raspberry Pi 4: an EZVIZ C1C RTSP camera streamed to a web page over
Tailscale, with motion-gated person detection that overlays boxes on the live view and records clips.

| Folder | What | Service |
|---|---|---|
| [`camera/`](camera/README.md) | Flask server on port 8000: live view, detection overlay, fps, recordings page | `camera-stream` |
| [`human_detector/`](human_detector/README.md) | Motion check → YOLO11n (NCNN) person detection → H.264 clips, 24 h retention | `human-detector` |
| [`systemd/`](systemd/) | unit files for both services | |

They run as two separate processes, so either one can crash or be restarted without taking down the other.

## Data flow

```
EZVIZ C1C ──RTSP/H.265──▶ camera server (camera/, :8000)
                            capture thread: decode → JPEG → latest frame
                            ├── /video_feed  (MJPEG) ──▶ browser
                            │                       └──▶ human_detector
                            ├── /detections  ◀── /dev/shm/human_detections.json ◀── human_detector
                            ├── /recordings  ◀── human_detector/recordings/YYYY-MM-DD/*.mp4
                            └── /health      (connected, profile, fps, size)

human_detector: motion check → YOLO on 1 of 8 frames at 640 px while active
                → publish boxes → record H.264 while a person was seen in the last 10 s
```

The camera RTSP stream is opened and decoded only once, by the camera server. The live page draws the
boxes on a canvas over `/video_feed`, so the feed the detector reads never contains its own boxes.

Open `http://pi-server:8000` (live) or `http://pi-server:8000/recordings` from any device on the tailnet.

## Setup

```bash
sudo apt install python3-flask python3-dotenv python3-opencv python3-numpy

cd ~/home-camera/camera
python3 -m venv --system-site-packages venv   # everything comes from apt, see requirements.txt
cp .env.example .env   # fill in the RTSP URLs

cd ~/home-camera/human_detector
python3 -m venv --system-site-packages .venv && .venv/bin/pip install -r requirements.txt

sudo cp ~/home-camera/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now camera-stream human-detector
```
