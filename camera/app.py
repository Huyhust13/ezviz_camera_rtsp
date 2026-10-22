import json
import logging
import os
import time
from functools import lru_cache

import cv2

from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, render_template, send_from_directory

from camera import CameraStream

load_dotenv()

logging.basicConfig(level=logging.INFO)

PROFILES = {
    "main": os.environ["RTSP_URL_MAIN"],
    "sub": os.environ["RTSP_URL_SUB"],
}
DEFAULT_PROFILE = os.environ.get("DEFAULT_PROFILE", "sub")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
# written by ~/human_detector; boxes older than DETECTIONS_MAX_AGE seconds are dropped
DETECTIONS_FILE = os.environ.get("DETECTIONS_FILE", "/dev/shm/human_detections.json")
DETECTIONS_MAX_AGE = float(os.environ.get("DETECTIONS_MAX_AGE", "3"))
# clips recorded by ~/human_detector, laid out as YYYY-MM-DD/person_HHMMSS.mp4
RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/home/huy/human_detector/recordings")
# a clip modified this recently is still being written (not playable yet)
RECORDING_ACTIVE_SECS = 5

app = Flask(__name__)
stream = CameraStream(PROFILES, DEFAULT_PROFILE)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/video_feed")
def video_feed():
    return Response(
        stream.frame_generator(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/snapshot.jpg")
def snapshot():
    jpeg = stream.get_jpeg()
    if jpeg is None:
        return "no frame available", 503
    return Response(jpeg, mimetype="image/jpeg")


@app.route("/detections")
def detections():
    try:
        with open(DETECTIONS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"boxes": [], "detector": False}
    if time.time() - data.get("ts", 0) > DETECTIONS_MAX_AGE:
        data["boxes"] = []
    data["detector"] = True
    return data


@app.route("/health")
def health():
    size = stream.frame_size
    return {
        "connected": stream.connected,
        "profile": stream.current_profile,
        "fps": round(stream.fps, 1),
        "width": size[0] if size else None,
        "height": size[1] if size else None,
    }


@lru_cache(maxsize=512)
def clip_duration(path, mtime, size):
    """Clip length in seconds; mtime/size are only part of the cache key."""
    cap = cv2.VideoCapture(path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        return frames / fps if fps > 0 and frames > 0 else None
    finally:
        cap.release()


def clip_start(name):
    """person_134441.mp4 -> 13:44:41"""
    t = name.removeprefix("person_").removesuffix(".mp4")
    return f"{t[:2]}:{t[2:4]}:{t[4:6]}" if len(t) == 6 and t.isdigit() else t


def list_recordings():
    """[(day, [clip, ...]), ...], newest day and clip first."""
    days = []
    try:
        day_names = sorted(os.listdir(RECORDINGS_DIR), reverse=True)
    except FileNotFoundError:
        return days
    now = time.time()
    for day in day_names:
        day_dir = os.path.join(RECORDINGS_DIR, day)
        if not os.path.isdir(day_dir):
            continue
        clips = []
        for name in sorted(os.listdir(day_dir), reverse=True):
            if not name.endswith(".mp4"):
                continue
            path = os.path.join(day_dir, name)
            try:
                st = os.stat(path)
            except FileNotFoundError:
                continue
            active = now - st.st_mtime < RECORDING_ACTIVE_SECS
            clips.append({
                "day": day,
                "name": name,
                "start": clip_start(name),
                "size_mb": st.st_size / 1e6,
                "duration": None if active else clip_duration(path, st.st_mtime, st.st_size),
                "active": active,
            })
        if clips:
            days.append((day, clips))
    return days


@app.route("/recordings")
def recordings():
    days = list_recordings()
    total = sum(len(c) for _, c in days)
    return render_template("recordings.html", days=days, total=total)


@app.route("/recordings/<day>/<name>")
def recording_file(day, name):
    if not name.endswith(".mp4"):
        abort(404)
    # send_from_directory rejects paths escaping RECORDINGS_DIR and supports Range requests
    return send_from_directory(RECORDINGS_DIR, f"{day}/{name}", mimetype="video/mp4")


@app.route("/profile")
def get_profile():
    return {"profile": stream.current_profile, "profiles": list(stream.profiles)}


@app.route("/profile/<name>", methods=["POST"])
def set_profile(name):
    try:
        stream.switch_profile(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return {"profile": stream.current_profile}


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=True)
