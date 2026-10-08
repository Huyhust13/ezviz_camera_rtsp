import os
import threading
import time
import logging

import cv2

logger = logging.getLogger("camera")

# TCP transport avoids the packet-loss artifacts UDP gives over wifi; low
# buffer size keeps the stream close to real-time instead of drifting behind.
# stimeout bounds how long a connect attempt can block — without it, opening
# a stream can hang far longer than reconnect_delay, stalling profile switches.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|buffer_size;1024000|stimeout;8000000",
)


class CameraStream:
    """Continuously pulls frames from an RTSP source on a background thread
    and keeps the latest one ready to serve, reconnecting on failure."""

    def __init__(self, profiles: dict, default_profile: str, reconnect_delay: float = 3.0):
        self.profiles = profiles
        self.reconnect_delay = reconnect_delay

        self._lock = threading.Lock()
        self._current_profile = default_profile
        self._switch_pending = None
        self.rtsp_url = profiles[default_profile]

        self._cap = None
        self._latest_jpeg = None
        self._frame_ready = threading.Condition()
        self._frame_version = 0
        self._stop = False
        self._connected = False

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def current_profile(self) -> str:
        with self._lock:
            return self._current_profile

    def switch_profile(self, name: str):
        if name not in self.profiles:
            raise ValueError(f"unknown profile: {name}")
        with self._lock:
            if name != self._current_profile:
                self._switch_pending = name

    def _open_capture(self):
        cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        return cap

    def _run(self):
        while not self._stop:
            switched = False
            with self._lock:
                if self._switch_pending is not None:
                    logger.info("switching stream profile to %s", self._switch_pending)
                    self._current_profile = self._switch_pending
                    self.rtsp_url = self.profiles[self._current_profile]
                    self._switch_pending = None
                    if self._cap is not None:
                        self._cap.release()
                    self._cap = None
                    self._connected = False
                    switched = True

            if switched:
                # The camera needs a moment to free its old RTSP session —
                # reconnecting immediately can make the new open() hang far
                # longer than stimeout (observed: 60s+ instead of failing fast).
                time.sleep(2.0)

            if self._cap is None:
                logger.info("connecting to %s", self.rtsp_url)
                self._cap = self._open_capture()
                if not self._cap.isOpened():
                    logger.warning("failed to open stream, retrying in %.0fs", self.reconnect_delay)
                    self._cap.release()
                    self._cap = None
                    self._connected = False
                    time.sleep(self.reconnect_delay)
                    continue
                self._connected = True
                logger.info("connected")

            ok, frame = self._cap.read()
            if not ok:
                logger.warning("lost stream, reconnecting")
                self._cap.release()
                self._cap = None
                self._connected = False
                time.sleep(self.reconnect_delay)
                continue

            frame = self.process_frame(frame)

            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                continue

            with self._frame_ready:
                self._latest_jpeg = buf.tobytes()
                self._frame_version += 1
                self._frame_ready.notify_all()

    def process_frame(self, frame):
        """Hook for future AI processing (detection, overlays, etc.)."""
        return frame

    def get_jpeg(self, timeout: float = 5.0):
        with self._frame_ready:
            if self._latest_jpeg is None:
                self._frame_ready.wait(timeout=timeout)
            return self._latest_jpeg

    def frame_generator(self):
        last_version = -1
        while True:
            with self._frame_ready:
                self._frame_ready.wait_for(
                    lambda: self._frame_version != last_version, timeout=5.0
                )
                last_version = self._frame_version
                jpeg = self._latest_jpeg
            if jpeg is None:
                continue
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
            )

    def stop(self):
        self._stop = True
        if self._cap is not None:
            self._cap.release()
