# camera

Livestreams an RTSP camera to a web page, reachable remotely over Tailscale.

## How it works

- `camera.py` — background thread pulls frames from the RTSP camera via OpenCV/ffmpeg, reconnects automatically if the stream drops. Supports switching between RTSP profiles (`main`/`sub`) at runtime. `process_frame()` is the hook to add AI (detection, overlays, etc.) later — it receives each raw frame before it's JPEG-encoded.
- `app.py` — Flask app serving:
  - `/` — web page with the live view and a profile toggle
  - `/video_feed` — MJPEG stream
  - `/snapshot.jpg` — single current frame as JPEG
  - `/detections` — latest person boxes from `../human_detector` (read from `/dev/shm/human_detections.json`, dropped after 3 s); the web page polls it every 300 ms and draws the boxes on a canvas over the video, so `/video_feed` itself stays clean
  - `/recordings` — page listing `../human_detector/recordings` clips by day (newest first) with an in-page player; `/recordings/<day>/<file>.mp4` serves a clip (Range requests, so seeking works). Override the folder with `RECORDINGS_DIR`
  - `/health` — `{"connected", "profile", "fps", "width", "height"}`; the live page shows the capture fps and resolution from it
  - `/profile` — `GET` current profile + available profiles; `POST /profile/<name>` to switch
- Config lives in `.env` (not committed — holds the camera's RTSP credentials).

## Getting the RTSP URL (EZVIZ C1C)

EZVIZ cameras run Hikvision firmware under the hood, so they can be configured locally with Hikvision's **iVMS-4200** client instead of the cloud-only EZVIZ app — that's what exposes the plain RTSP stream this project needs.

1. **Find the camera's IP** — check your router's DHCP client list / connected devices page for the camera (EZVIZ/Hikvision MAC vendor), or check the EZVIZ app under the device's network info. A LAN scan (`nmap -sn 192.168.x.0/24` or `arp-scan --localnet`) from another machine on the same network works too.
2. **Install iVMS-4200** (Windows/macOS, from Hikvision's site) on a machine on the same LAN as the camera.
3. **Add the camera** in iVMS-4200 via Device Management → add by IP address, logging in with the camera's admin credentials (set during EZVIZ app activation, or on the camera's label).
4. **Turn off image/video encryption** — in the camera's configuration (Configuration → Network → Advanced, or Configuration → System → Security, varies by firmware) find the "Image Encryption" / "Video Encryption" toggle and disable it. Leaving it on requires appending the camera's verification code to the RTSP credentials, which breaks a plain `user:password` RTSP URL.
5. **Confirm RTSP is enabled** and note the port (default `554`).
6. The RTSP URL is then `rtsp://<user>:<password>@<ip>:554/H.264` for the main stream, or `/H.264/ch1/sub` for the lower-res substream this project defaults to (see [Notes](#notes)) — set it in `.env` as `RTSP_URL_MAIN` / `RTSP_URL_SUB`.

Exact menu names shift between firmware versions — if a setting isn't where expected, search iVMS-4200's full configuration tree for "encryption".

## Run it

```
source venv/bin/activate
python app.py
```

Or as a systemd service (auto-starts on boot, restarts on crash):

```
sudo cp ../systemd/camera-stream.service /etc/systemd/system/
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
