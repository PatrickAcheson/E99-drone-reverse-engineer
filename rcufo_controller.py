#!/usr/bin/env python3
"""Local browser controls and live video for the RC UFO profile-83 drone."""
from __future__ import annotations

import argparse
import ipaddress
import json
import math
import secrets
import shutil
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from rcufo.control import FlightLink, axes_for_keys
from rcufo.video import DemoLink, SessionLog, VideoFeed


def validate_host(value):
    address = ipaddress.IPv4Address(value)
    if not address.is_private or address.is_unspecified or address.is_multicast:
        raise ValueError("Use the drone's private IPv4 address")
    return str(address)


class Controller:
    def __init__(self, host, executable, log, demo=False):
        self.host, self.executable, self.log, self.demo = host, executable, log, demo
        self.lock = threading.RLock()
        self.link, self.video = None, None
        self.last_input_sequence = -1
        self.fps_time, self.fps_count, self.fps = time.monotonic(), 0, 0.0
        self.trims = {"roll": 0, "pitch": 0, "yaw": 0}
        self.last_keys, self.last_speed, self.headless = set(), 30, False

    def update_sticks(self):
        if self.link:
            self.link.update_input(axes_for_keys(self.last_keys, self.last_speed, self.trims), self.headless)

    def connect(self, host):
        with self.lock:
            if self.link:
                return "Already connected"
            host = validate_host(host)
            if not self.demo and not self.executable:
                raise ValueError("FFmpeg not found. Supply its executable with --ffmpeg")
            self.host = host
            self.link = (DemoLink if self.demo else FlightLink)(host, self.log.emit)
            self.video = VideoFeed(host, self.executable, self.log, self.demo)
            self.last_input_sequence = -1
            self.last_keys = set()
            self.fps_time, self.fps_count, self.fps = time.monotonic(), 0, 0.0
            self.link.start()
            self.video.start()
            self.log.emit("connected", host=host, demo=self.demo)
            return "Connecting; flight controls remain disabled"

    def disconnect(self):
        with self.lock:
            if self.link:
                self.link.stop()
                self.link = None
            if self.video:
                self.video.stop()
                self.video = None
            self.log.emit("disconnected")
            return "Disconnected; no automatic landing was requested"

    def state(self):
        with self.lock:
            state = {"connected": self.link is not None, "host": self.host, "demo": self.demo,
                     "enabled": False, "profile": None, "fresh": False, "emergency": False,
                     "reason": "Disconnected", "axes": [128] * 4, "video": "Video waiting",
                     "trims": dict(self.trims), "calibrating": False, "calibration_remaining": 0,
                     "video_stale": False, "dimensions": None, "fps": 0, "log_directory": str(self.log.directory)}
            if self.link:
                state.update(self.link.status())
            if self.video:
                now = time.monotonic()
                if now - self.fps_time >= 1:
                    self.fps = (self.video.frame_count - self.fps_count) / (now - self.fps_time)
                    self.fps_count, self.fps_time = self.video.frame_count, now
                state.update(video=self.video.status, dimensions=self.video.dimensions,
                             fps=round(self.fps, 1), video_stale=bool(self.video.last_frame and now - self.video.last_frame > 2))
            return state

    def input(self, values):
        keys = values.get("keys", [])
        if not isinstance(keys, list) or len(keys) > 16 or any(k not in ("w", "s", "a", "d", "space", "shift", "q", "e") for k in keys):
            raise ValueError("Invalid movement keys")
        sequence = values.get("sequence")
        if not isinstance(sequence, (int, float)) or not math.isfinite(sequence):
            raise ValueError("Input sequence required")
        speed = values.get("speed", 30)
        if not isinstance(speed, (int, float)) or not math.isfinite(speed):
            raise ValueError("Invalid stick speed")
        with self.lock:
            if sequence <= self.last_input_sequence:
                return "Old input ignored"
            self.last_input_sequence = sequence
            self.last_keys, self.last_speed = set(keys), speed
            self.headless = values.get("headless") is True
            self.update_sticks()
        return "Input updated"

    def adjust_trim(self, values):
        axis = values.get("axis")
        delta = values.get("delta")
        with self.lock:
            if self.link and self.link.status()["calibrating"]:
                return "Wait for the calibration command to finish before changing trim"
            if axis == "reset":
                self.trims = {"roll": 0, "pitch": 0, "yaw": 0}
            elif axis in self.trims and isinstance(delta, int) and delta in (-2, 2):
                self.trims[axis] = max(-48, min(48, self.trims[axis] + delta))
            else:
                raise ValueError("Choose roll, pitch, or yaw with a trim step of -2 or +2")
            self.last_keys.clear()
            self.update_sticks()
            self.log.emit("trim", values=dict(self.trims))
            return "Trim: " + ", ".join(f"{name} {value:+d}" for name, value in self.trims.items())

    def action(self, name):
        with self.lock:
            if not self.link:
                return "Connect first"
            if name == "enable":
                accepted, message = self.link.enable()
                if accepted:
                    self.last_keys.clear()
                    self.update_sticks()
                return message
            if name == "disable":
                self.link.disable()
                return "Controls disabled; this does not land the drone"
            if name in ("takeoff", "land", "calibrate", "emergency"):
                return self.link.command(name)[1]
            if name == "camera":
                return self.link.camera()[1]
            if name == "snapshot":
                if not self.video or not self.video.latest_jpeg:
                    return "No decoded frame yet"
                filename = "snapshot-" + str(time.time_ns()) + ".jpg"
                (self.log.directory / filename).write_bytes(self.video.latest_jpeg)
                self.log.emit("snapshot", file=filename)
                return "Saved " + filename
            raise ValueError("Unknown action")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, controller, port=0):
        super().__init__(("127.0.0.1", port), Handler)
        self.controller = controller
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_address[1]}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not copy access tokens into logs.

    def authorized(self):
        supplied = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
        return secrets.compare_digest(supplied, self.server.token)

    def respond(self, status, body, content_type="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.authorized():
            self.respond(403, {"error": "Open the link printed by the controller"})
            return
        path = urlsplit(self.path).path
        if path == "/":
            page = (Path(__file__).parent / "rcufo" / "assets" / "controller.html").read_text(encoding="utf-8")
            self.respond(200, page.replace("__TOKEN__", self.server.token).encode(), "text/html; charset=utf-8")
        elif path in ("/assets/controller.css", "/assets/controller.js"):
            asset = Path(__file__).parent / "rcufo" / "assets" / path.rsplit("/", 1)[1]
            content_type = "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8"
            self.respond(200, asset.read_bytes(), content_type)
        elif path == "/api/state":
            self.respond(200, self.server.controller.state())
        elif path == "/stream":
            self.stream()
        else:
            self.respond(404, {"error": "Not found"})

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = None
        last_sent = 0.0
        try:
            self.wfile.write(b"--frame\r\n")
            while True:
                video = self.server.controller.video
                if video is None:
                    return
                image = video.latest_jpeg
                # Repeat a still frame occasionally so new browser clients can
                # finish parsing a multipart image even when decoding is paused.
                if image is not None and (image is not last or time.monotonic() - last_sent >= 0.5):
                    self.wfile.write(b"Content-Type: image/jpeg\r\nContent-Length: " + str(len(image)).encode() + b"\r\n\r\n" + image + b"\r\n--frame\r\n")
                    self.wfile.flush()
                    last = image
                    last_sent = time.monotonic()
                time.sleep(0.02)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        if not self.authorized() or self.headers.get("Origin", self.server.origin) != self.server.origin:
            self.respond(403, {"error": "Local access token and same-origin request required"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Invalid request size")
            values = json.loads(self.rfile.read(length))
            if not isinstance(values, dict):
                raise ValueError("JSON object required")
            path = urlsplit(self.path).path
            controller = self.server.controller
            if path == "/api/connect":
                message = controller.connect(values.get("host", controller.host))
            elif path == "/api/disconnect":
                message = controller.disconnect()
            elif path == "/api/input":
                message = controller.input(values)
            elif path == "/api/action":
                message = controller.action(values.get("name"))
            elif path == "/api/trim":
                message = controller.adjust_trim(values)
            elif path == "/api/quit":
                message = controller.disconnect()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.respond(404, {"error": "Not found"})
                return
            self.respond(200, {"message": message})
        except (ValueError, TypeError, OSError) as exc:
            self.respond(400, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.1.1")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    parser.add_argument("--connect", action="store_true", help="Connect on launch; controls remain disabled")
    parser.add_argument("--demo", action="store_true", help="Offline interface; no network packets")
    parser.add_argument("--no-browser", action="store_true", help="Print the local URL without opening it")
    args = parser.parse_args()
    try:
        validate_host(args.host)
    except ValueError as exc:
        parser.error(str(exc))
    log = SessionLog(Path(__file__).resolve().parent / "results" / "flights", args.demo)
    controller = Controller(args.host, args.ffmpeg, log, args.demo)
    server = Server(controller)
    url = server.origin + "/?token=" + server.token
    print(f"Open: {url}\nSession output: {log.directory}\nPress Ctrl+C or Quit in the page to close.", flush=True)
    try:
        if args.connect or args.demo:
            controller.connect(args.host)
        if not args.no_browser:
            webbrowser.open(url)
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        controller.disconnect()
        server.server_close()
        log.close()


if __name__ == "__main__":
    main()
