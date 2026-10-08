# camera

Livestreams an RTSP camera to a web page, reachable remotely over Tailscale.

## How it works

- `camera.py` — background thread pulls frames from the RTSP camera via OpenCV/ffmpeg, reconnects automatically if the stream drops. Supports switching between RTSP profiles (`main`/`sub`) at runtime. `process_frame()` is the hook to add AI (detection, overlays, etc.) later — it receives each raw frame before it's JPEG-encoded.
- `app.py` — Flask app serving:
  - `/` — web page with the live view and a profile toggle
  - `/video_feed` — MJPEG stream
  - `/snapshot.jpg` — single current frame as JPEG
  - `/health` — `{"connected": true/false, "profile": "main"|"sub"}`
  - `/profile` — `GET` current profile + available profiles; `POST /profile/<name>` to switch
- Config lives in `.env` (not committed — holds the camera's RTSP credentials).

## Run it

```
source venv/bin/activate
python app.py
```

Or as a systemd service (auto-starts on boot, restarts on crash):

```
sudo cp camera-stream.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now camera-stream
```

Logs: `journalctl -u camera-stream -f`

## Accessing it remotely

This Pi is already on your Tailscale network (hostname `pi-server`, 100.75.232.43). From any device on your tailnet (phone, laptop, PC) — including away from home:

```
http://pi-server:8000
```

or by IP:

```
http://100.75.232.43:8000
```

No port forwarding or public exposure needed — Tailscale's WireGuard mesh handles the connection.

## Notes

- The camera reports itself as H.264 in the URL path but actually streams **HEVC/H.265**. The app defaults to the camera's substream (`sub` = `/H.264/ch1/sub`, 704x396@10fps) rather than the main stream (`main` = `/H.264`, 1920x1080@15fps) — same codec, but ~85% less CPU to software-decode (~22% of one core vs ~115%). Switch profiles live from the web page, or change `DEFAULT_PROFILE` in `.env`.
- Switching profiles takes ~4-5s: the camera needs a moment to free its old RTSP session before it'll accept a new connection — reconnecting immediately can make the open() call hang 60s+ instead of failing fast, so `camera.py` pauses briefly after release before reopening.
- **Hardware HEVC decode was investigated and is a dead end on this stack**: the Pi 4 does have a dedicated HEVC decoder block (`rpi-hevc-dec`, `/dev/video19`), but it only exposes a vendor-specific tiled pixel format (`NC12`) over the V4L2 Request API. ffmpeg's `hevc_v4l2m2m` wrapper doesn't support the Request API at all (wrong device model), and GStreamer's `v4l2codecs` plugin — which does support Request API decoders — explicitly refuses to register this one ("H265 decoder... produces no supported format"), since nothing in the stack can de-tile `NC12` back to a usable format. Making this work would mean writing a custom GStreamer/libcamera de-tiling stage; not worth it given the substream already solves the CPU problem.
- MJPEG was chosen over HLS for simplicity and lower latency, and because it gives direct frame access for the planned AI step. It's less efficient over slow links — revisit if that matters later.
- `gstreamer1.0-*` packages got installed while investigating hardware decode above; they're not used by the app and can be removed (`sudo apt-get remove gstreamer1.0-plugins-bad gstreamer1.0-plugins-good gstreamer1.0-tools gstreamer1.0-rtsp gstreamer1.0-libav python3-gst-1.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0`) if you'd rather not keep them around.
