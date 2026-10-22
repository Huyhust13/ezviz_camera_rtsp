"""Motion-gated person detection with event recording and 1-day rotation.

Pipeline per frame:
  1. cheap motion check (downscaled, blurred, diff vs running background)
  2. if motion, or a person was seen in the last HOLD seconds -> run the
     detector on every SKIP-th frame
  3. while a person was seen in the last HOLD seconds -> draw boxes and record
  4. periodically delete recordings older than RETENTION hours
"""
import argparse
import collections
import json
import logging
import os
import signal
import shutil
import time
from datetime import datetime

import cv2
import numpy as np

from detector import PersonDetector

log = logging.getLogger("human_detector")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", default=os.environ.get("HD_SOURCE", "http://127.0.0.1:8000/video_feed"),
                   help="camera server MJPEG feed (default), camera index, 'picam', RTSP URL or video file")
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--height", type=int, default=480)
    p.add_argument("--fps", type=float, default=10, help="capture / recording fps")
    p.add_argument("--out", default=os.environ.get("HD_OUT", os.path.expanduser("~/human_detector/recordings")))
    p.add_argument("--skip", type=int, default=8, help="run detector on 1 of every N frames")
    p.add_argument("--hold", type=float, default=10, help="seconds to keep detecting/recording after last person")
    p.add_argument("--motion-hold", type=float, default=1.0, help="seconds to keep detecting after last motion")
    p.add_argument("--motion-area", type=float, default=0.005, help="fraction of pixels that must change")
    p.add_argument("--preroll", type=float, default=2, help="seconds of video kept before a detection")
    p.add_argument("--max-clip", type=float, default=600, help="split clips longer than this (seconds)")
    p.add_argument("--retention", type=float, default=24, help="delete recordings older than this (hours)")
    p.add_argument("--min-free-gb", type=float, default=1.0, help="delete oldest recordings below this free space")
    p.add_argument("--imgsz", type=int, default=640,
                   help="detector input size (320/416/640); 640 needed for small/far people")
    p.add_argument("--conf", type=float, default=0.4)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--fourcc", default="avc1", help="video codec for cv2.VideoWriter (avc1 = H.264, plays in browsers)")
    p.add_argument("--publish", default=os.environ.get("HD_PUBLISH", "/dev/shm/human_detections.json"),
                   help="write latest boxes here as JSON for the camera server overlay ('' to disable)")
    p.add_argument("--realtime", action="store_true",
                   help="for files: use wall clock instead of frame timestamps")
    return p.parse_args()


# ---------------------------------------------------------------- capture

class PiCamera:
    """Raspberry Pi CSI camera via picamera2 (apt install python3-picamera2)."""

    def __init__(self, w, h, fps):
        from picamera2 import Picamera2
        self.cam = Picamera2()
        cfg = self.cam.create_video_configuration(main={"size": (w, h), "format": "RGB888"},
                                                  controls={"FrameRate": fps})
        self.cam.configure(cfg)
        self.cam.start()

    def read(self):
        return True, self.cam.capture_array()  # RGB888 in picamera2 is BGR byte order

    def release(self):
        self.cam.stop()


def open_source(args):
    """Return (capture, is_live)."""
    if args.source == "picam":
        return PiCamera(args.width, args.height, args.fps), True
    if args.source.isdigit():
        cap = cv2.VideoCapture(int(args.source), cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
        cap.set(cv2.CAP_PROP_FPS, args.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        live = True
    else:
        cap = cv2.VideoCapture(args.source)
        live = "://" in args.source
        if not live:
            args.fps = cap.get(cv2.CAP_PROP_FPS) or args.fps
    if not cap.isOpened():
        raise SystemExit(f"cannot open source {args.source!r}")
    return cap, live


# ---------------------------------------------------------------- motion

class MotionDetector:
    def __init__(self, area_frac, size=(160, 120)):
        self.area_frac = area_frac
        self.size = size
        self.bg = None

    def __call__(self, frame):
        small = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (7, 7), 0).astype(np.float32)
        if self.bg is None:
            self.bg = gray
            return False
        diff = cv2.absdiff(gray, self.bg)
        cv2.accumulateWeighted(gray, self.bg, 0.05)  # slowly adapt to lighting changes
        changed = np.count_nonzero(diff > 25)
        return changed > self.area_frac * diff.size


# ---------------------------------------------------------------- recording

class Recorder:
    def __init__(self, out_dir, fps, fourcc, max_clip):
        self.out_dir = out_dir
        self.fps = fps
        self.fourcc = cv2.VideoWriter_fourcc(*fourcc)
        self.max_clip = max_clip
        self.writer = None
        self.path = None
        self.started = 0.0
        self.frames = 0
        self.shape = None

    def write(self, frame, now, wall, preroll=()):
        # a new clip also starts if the frame size changes (camera server profile switch)
        if self.writer is not None and (now - self.started > self.max_clip
                                        or frame.shape[:2] != self.shape):
            self.close()
        if self.writer is None:
            day_dir = os.path.join(self.out_dir, wall.strftime("%Y-%m-%d"))
            os.makedirs(day_dir, exist_ok=True)
            self.path = os.path.join(day_dir, wall.strftime("person_%H%M%S.mp4"))
            h, w = self.shape = frame.shape[:2]
            self.writer = cv2.VideoWriter(self.path, self.fourcc, self.fps, (w, h))
            self.started = now
            self.frames = 0
            log.info("recording -> %s", self.path)
            for f in preroll:
                if f.shape[:2] != self.shape:
                    continue
                self.writer.write(f)
                self.frames += 1
        self.writer.write(frame)
        self.frames += 1

    def close(self):
        if self.writer is not None:
            self.writer.release()
            log.info("saved %s (%d frames, %.1fs)", self.path, self.frames, self.frames / self.fps)
            self.writer = None


def cleanup(out_dir, retention_h, min_free_gb):
    """Delete clips older than retention, then oldest clips while disk is too full."""
    cutoff = time.time() - retention_h * 3600
    files = []
    for root, _, names in os.walk(out_dir):
        for n in names:
            p = os.path.join(root, n)
            try:
                files.append((os.path.getmtime(p), p))
            except FileNotFoundError:
                pass
    files.sort()
    removed = 0
    for mtime, p in files:
        free_gb = shutil.disk_usage(out_dir).free / 1e9
        if mtime >= cutoff and free_gb >= min_free_gb:
            break
        os.remove(p)
        removed += 1
    for root, dirs, names in os.walk(out_dir, topdown=False):  # drop empty day folders
        if root != out_dir and not dirs and not names:
            os.rmdir(root)
    if removed:
        log.info("cleanup: removed %d old recording(s)", removed)


# ---------------------------------------------------------------- overlay

def publish(path, boxes, frame):
    """Atomically write the latest boxes so the camera server can overlay them."""
    if not path:
        return
    h, w = frame.shape[:2]
    data = {"ts": time.time(), "width": w, "height": h,
            "boxes": [{"x1": x1, "y1": y1, "x2": x2, "y2": y2, "score": round(s, 3)}
                      for x1, y1, x2, y2, s in boxes]}
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("publish failed: %s", e)


# ---------------------------------------------------------------- main loop

def draw(frame, boxes, wall):
    for x1, y1, x2, y2, score in boxes:
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(frame, f"person {score:.2f}", (x1, max(y1 - 6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
    cv2.putText(frame, wall.strftime("%Y-%m-%d %H:%M:%S"), (8, frame.shape[0] - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.makedirs(args.out, exist_ok=True)

    cap, live = open_source(args)
    detector = PersonDetector(target_size=args.imgsz, conf=args.conf, threads=args.threads)
    motion = MotionDetector(args.motion_area)
    recorder = Recorder(args.out, args.fps, args.fourcc, args.max_clip)
    preroll = collections.deque(maxlen=max(1, int(args.preroll * args.fps)))
    use_wall = live or args.realtime
    log.info("source=%s live=%s fps=%.1f skip=%d hold=%.0fs out=%s",
             args.source, live, args.fps, args.skip, args.hold, args.out)

    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))

    frame_i = 0
    motion_until = person_until = 0.0
    boxes = []
    next_cleanup = 0.0
    stats = collections.Counter()
    stats_t = time.monotonic()
    start_wall = datetime.now()
    fails = 0

    cleanup(args.out, args.retention, args.min_free_gb)
    try:
        while not stop:
            ok, frame = cap.read()
            if not ok:
                if not live:
                    break
                fails += 1
                if fails > 50:
                    log.warning("camera read failing, reopening")
                    cap.release()
                    time.sleep(2)
                    cap, _ = open_source(args)
                    fails = 0
                time.sleep(0.1)
                continue
            fails = 0
            frame_i += 1
            # file sources run faster than real time, so derive time from the frame index
            now = time.monotonic() if use_wall else frame_i / args.fps
            wall = datetime.now() if use_wall else datetime.fromtimestamp(start_wall.timestamp() + now)

            if motion(frame):
                motion_until = now + args.motion_hold
                stats["motion"] += 1

            active = now < motion_until or now < person_until
            if active and frame_i % args.skip == 0:
                t0 = time.perf_counter()
                boxes = detector(frame)
                publish(args.publish, boxes, frame)
                stats["det"] += 1
                stats["det_ms"] += (time.perf_counter() - t0) * 1000
                if boxes:
                    if now >= person_until:
                        log.info("person detected (%d)", len(boxes))
                    person_until = now + args.hold
            elif not active and boxes:
                boxes = []
                publish(args.publish, boxes, frame)

            if now < person_until:
                draw(frame, boxes, wall)
                recorder.write(frame, now, wall, preroll)
                preroll.clear()
            else:
                if recorder.writer is not None:
                    recorder.close()
                preroll.append(frame)

            stats["frames"] += 1
            mono = time.monotonic()
            if mono >= next_cleanup:
                cleanup(args.out, args.retention, args.min_free_gb)
                next_cleanup = mono + 600
            if mono - stats_t >= 60:
                el = mono - stats_t
                log.info("stats: %.1f fps, motion %d, detector %d runs avg %.0f ms",
                         stats["frames"] / el, stats["motion"], stats["det"],
                         stats["det_ms"] / max(stats["det"], 1))
                stats.clear()
                stats_t = mono
    finally:
        recorder.close()
        cap.release()
        log.info("stopped")


if __name__ == "__main__":
    main()
