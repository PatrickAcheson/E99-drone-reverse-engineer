#!/usr/bin/env python3
"""Video decoding and session logs for the RC UFO client."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw
from .control import FlightLink


class SessionLog:
    def __init__(self, base, demo=False):
        self.directory = base / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        self.directory.mkdir(parents=True, exist_ok=False)
        self.lock = threading.Lock()
        self.file = (self.directory / "events.jsonl").open("w", encoding="utf-8")
        self.closed = False
        self.counts = {}
        self.emit("session_started", demo=demo)

    def emit(self, event, **values):
        with self.lock:
            if self.closed:
                return
            item = {"utc": dt.datetime.now(dt.timezone.utc).isoformat(), "event": event, **values}
            self.counts[event] = self.counts.get(event, 0) + 1
            try:
                self.file.write(json.dumps(item) + "\n")
                self.file.flush()
            except OSError:
                # Disk failure must not stop the control sender.
                pass

    def close(self):
        self.emit("session_closed")
        with self.lock:
            self.closed = True
            self.file.close()
            (self.directory / "session.json").write_text(json.dumps({
                "event_counts": self.counts, "files": ["events.jsonl", "ffmpeg.log", "first-frame.jpg"],
            }, indent=2), encoding="utf-8")


class JpegPipe:
    """FFmpeg emits ordinary JPEGs, each terminated by an EOI marker."""
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        frames = []
        while True:
            start = self.buffer.find(b"\xff\xd8")
            if start < 0:
                self.buffer[:] = self.buffer[-1:]
                break
            if start:
                del self.buffer[:start]
            end = self.buffer.find(b"\xff\xd9", 2)
            if end < 0:
                if len(self.buffer) > 4 * 1024 * 1024:
                    self.buffer.clear()
                    raise ValueError("FFmpeg image exceeds the 4 MB frame limit")
                break
            frames.append(bytes(self.buffer[:end + 2]))
            del self.buffer[:end + 2]
        return frames


def ffmpeg_command(executable, host):
    return [executable, "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-rtsp_transport", "udp", "-rtsp_flags", "filter_src", "-timeout", "5000000",
            "-fflags", "nobuffer", "-flags", "low_delay", "-analyzeduration", "1000000",
            "-probesize", "32768", "-i", f"rtsp://{host}:7070/webcam",
            "-map", "0:v:0", "-an", "-c:v", "mjpeg", "-q:v", "3", "-threads", "1",
            "-f", "image2pipe", "pipe:1"]


class VideoFeed:
    def __init__(self, host, executable, log, demo=False):
        self.host, self.executable, self.log, self.demo = host, executable, log, demo
        self.stopping = threading.Event()
        self.frames = queue.Queue(maxsize=1)
        self.thread = None
        self.process = None
        self.process_lock = threading.Lock()
        self.last_frame = 0.0
        self.frame_count = 0
        self.latest_jpeg = None
        self.dimensions = None
        self.status = "Connecting video..."

    def start(self):
        self.thread = threading.Thread(target=self.run, name="video-decoder", daemon=True)
        self.thread.start()

    def publish(self, image, jpeg=None):
        self.frame_count += 1
        self.last_frame = time.monotonic()
        if jpeg is None:
            encoded = io.BytesIO()
            image.save(encoded, format="JPEG", quality=85)
            jpeg = encoded.getvalue()
        self.latest_jpeg = jpeg
        self.dimensions = image.size
        if self.frame_count == 1:
            image.save(self.log.directory / "first-frame.jpg", quality=90)
            self.log.emit("video_first_frame", width=image.width, height=image.height)
        try:
            self.frames.get_nowait()
        except queue.Empty:
            pass
        self.frames.put_nowait(image)
        self.status = f"Video {image.width} x {image.height}"

    def run(self):
        if self.demo:
            while not self.stopping.wait(0.05):
                image = Image.new("RGB", (640, 360), "#172b40")
                draw = ImageDraw.Draw(image)
                draw.rectangle((24, 24, 616, 336), outline="#3c718e", width=2)
                draw.text((45, 55), "OFFLINE DEMO - NO NETWORK PACKETS", fill="white")
                draw.text((45, 90), "Use Enable controls to exercise the interface offline.", fill="#86b8d1")
                x = 60 + int(time.monotonic() * 80) % 500
                draw.ellipse((x, 170, x + 30, 200), fill="#60a5fa")
                self.publish(image)
            return
        command = ffmpeg_command(self.executable, self.host)
        while not self.stopping.is_set():
            self.status = "Connecting video..."
            self.log.emit("video_connect", command=command)
            process = None
            with (self.log.directory / "ffmpeg.log").open("ab", buffering=0) as errors:
                try:
                    with self.process_lock:
                        if self.stopping.is_set():
                            return
                        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                   stderr=errors, bufsize=0,
                                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                        self.process = process
                    parser = JpegPipe()
                    while not self.stopping.is_set():
                        chunk = process.stdout.read(4096)
                        if not chunk:
                            break
                        for frame in parser.feed(chunk):
                            try:
                                with Image.open(io.BytesIO(frame)) as source:
                                    self.publish(source.convert("RGB"), frame)
                            except (OSError, ValueError) as exc:
                                self.log.emit("frame_decode_error", message=str(exc))
                    self.status = "Video stopped; retrying (see ffmpeg.log)"
                    self.log.emit("video_ended", returncode=process.poll())
                except (OSError, ValueError) as exc:
                    self.status = f"Video error: {exc}"
                    self.log.emit("video_error", message=str(exc))
                finally:
                    if process:
                        self.terminate(process)
                        if process.stdout:
                            process.stdout.close()
                    with self.process_lock:
                        self.process = None
            self.stopping.wait(2.0)

    @staticmethod
    def terminate(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)

    def stop(self):
        self.stopping.set()
        with self.process_lock:
            if self.process:
                self.terminate(self.process)
        if self.thread:
            self.thread.join(timeout=2.0)


class DemoLink(FlightLink):
    def send(self, packet, kind):
        self.sent_packets += 1
        self.emit("demo_packet", kind=kind, hex=packet.hex(" "))
        return True

    def run(self):
        self.profile = 83
        while not self.stopping.wait(0.05):
            self.last_response = time.monotonic()
            self.tick(self.last_response)


