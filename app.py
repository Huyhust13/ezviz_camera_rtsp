import logging
import os

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template

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


@app.route("/health")
def health():
    return {"connected": stream.connected, "profile": stream.current_profile}


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
