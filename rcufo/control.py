"""TC/profile-83 flight protocol and its independent 20 Hz UDP sender."""
from __future__ import annotations

import socket
import threading
import time


TAKEOFF, LAND, KILL, HEADLESS, CALIBRATE = 0x01, 0x02, 0x04, 0x10, 0x80


def control_packet(roll=128, pitch=128, throttle=128, yaw=128, flags=0):
    values = (roll, pitch, throttle, yaw, flags)
    if any(not isinstance(v, int) or not 0 <= v <= 255 for v in values):
        raise ValueError("Control fields must be integers from 0 through 255")
    checksum = roll ^ pitch ^ throttle ^ yaw ^ flags
    return bytes((3, 0x66, *values, checksum, 0x99))


def axes_for_keys(keys, speed, trims=None, yaw_speed=64):
    speed = max(1, min(64, int(speed)))
    yaw_speed = max(1, min(127, int(yaw_speed)))
    trims = trims or {}
    for name in ("roll", "pitch", "yaw"):
        value = trims.get(name, 0)
        if not isinstance(value, int) or not -48 <= value <= 48:
            raise ValueError("Trim values must be integers in range -48..48")
    def axis(positive, negative, trim=0, strength=None):
        strength = speed if strength is None else strength
        value = 128 + trim + strength * (int(positive in keys) - int(negative in keys))
        return max(1, min(255, value))
    return (axis("d", "a", trims.get("roll", 0)), axis("w", "s", trims.get("pitch", 0)),
            axis("space", "shift"), axis("e", "q", trims.get("yaw", 0), yaw_speed))


class FlightLink:
    """Starts in heartbeat-only mode. Never enables itself after a reconnect."""
    def __init__(self, host, emit, port=7099):
        self.host, self.port, self.emit = host, port, emit
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("", 0))
        self.socket.setblocking(False)
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.thread = None
        self.enabled = False
        self.axes = (128, 128, 128, 128)
        self.headless = False
        self.profile = None
        self.last_response = 0.0
        self.last_ui = 0.0
        self.command_flag = 0
        self.command_until = 0.0
        self.emergency_until = 0.0
        self.camera_under = False
        self.reason = "Waiting for profile 83"
        self.sent_packets = 0
        self.last_error = ""

    def start(self):
        self.thread = threading.Thread(target=self.run, name="flight-udp", daemon=True)
        self.thread.start()

    def send(self, packet, kind):
        # Caller holds lock, so disabling cannot race a previously built movement packet.
        try:
            self.socket.sendto(packet, (self.host, self.port))
            self.sent_packets += 1
            self.emit("udp_tx", kind=kind, hex=packet.hex(" "))
            return True
        except OSError as exc:
            text = str(exc)
            if text != self.last_error:
                self.emit("udp_error", message=text)
                self.last_error = text
            return False

    def fresh(self, now=None):
        now = time.monotonic() if now is None else now
        return self.last_response > 0 and now - self.last_response < 3.0

    def status(self):
        with self.lock:
            remaining = max(0.0, self.command_until - time.monotonic()) if self.command_flag == CALIBRATE else 0.0
            return {"enabled": self.enabled, "profile": self.profile, "fresh": self.fresh(),
                    "reason": self.reason, "axes": (128,) * 4 if remaining else self.axes, "packets_sent": self.sent_packets,
                    "calibrating": remaining > 0, "calibration_remaining": round(remaining, 1),
                    "emergency": self.emergency_until > time.monotonic()}

    def enable(self):
        with self.lock:
            if self.profile != 83 or not self.fresh():
                return False, "Need a recent profile-83 reply before enabling controls"
            if self.emergency_until > time.monotonic():
                return False, "Emergency stop is still being transmitted"
            self.axes = (128, 128, 128, 128)
            self.command_flag = 0
            self.last_ui = time.monotonic()
            self.enabled = True
            self.reason = "Controls enabled"
            self.emit("controls_enabled", profile=self.profile)
            return True, self.reason

    def disable(self, reason="Controls disabled"):
        with self.lock:
            was_enabled = self.enabled
            self.enabled = False
            self.axes = (128, 128, 128, 128)
            self.command_flag = 0
            self.command_until = 0
            self.reason = reason
            if was_enabled and not self.emergency_until:
                self.send(control_packet(), "neutral_before_disable")
                self.send(bytes((8, 1)), "disable_controls")
                self.emit("controls_disabled", reason=reason)

    def update_input(self, axes, headless=False):
        if len(axes) != 4 or any(not isinstance(v, int) or not 1 <= v <= 255 for v in axes):
            raise ValueError("Four stick values in range 1..255 are required")
        with self.lock:
            self.axes = tuple(axes)
            self.headless = headless
            self.last_ui = time.monotonic()

    def command(self, name):
        with self.lock:
            now = time.monotonic()
            if name == "emergency":
                if self.profile != 83:
                    return False, "No confirmed profile-83 connection"
                self.enabled = False
                self.axes = (128, 128, 128, 128)
                self.command_flag = 0
                self.emergency_until = now + 1.0
                self.reason = "Emergency stop; controls disabled"
                self.send(control_packet(flags=KILL), "emergency")
            else:
                if not self.enabled or not self.fresh():
                    return False, "Enable controls with a live connection first"
                if name in ("takeoff", "calibrate") and self.command_flag == CALIBRATE and now < self.command_until:
                    self.emit("command_rejected", name=name, reason="calibration_in_progress")
                    return False, f"Calibration command still running: {self.command_until - now:.1f}s remaining"
                commands = {"takeoff": (TAKEOFF, 1.0), "land": (LAND, 1.0), "calibrate": (CALIBRATE, 2.0)}
                flag, duration = commands[name]
                self.command_flag = flag
                self.command_until = now + duration
                self.axes = (128, 128, 128, 128)
                self.send(control_packet(flags=flag | (HEADLESS if self.headless and name != "calibrate" else 0)), name)
            self.emit("command", name=name)
            return True, name.replace("_", " ").title() + " sent"

    def camera(self):
        with self.lock:
            if self.profile != 83 or not self.fresh():
                return False, "Need a live profile-83 connection"
            self.camera_under = not self.camera_under
            value = 2 if self.camera_under else 1
            sent = self.send(bytes((6, value)), "camera")
            return sent, "Camera selection sent"

    def accept_response(self, data, source, now=None):
        if source != (self.host, self.port) or len(data) < 5:
            return
        # Status/event packets beginning 66 are not profile identifiers.
        profile = data[0]
        if profile == 0x66:
            return
        with self.lock:
            changed = self.profile != profile
            self.profile = profile
            self.last_response = time.monotonic() if now is None else now
            if changed:
                self.emit("profile", id=profile)
            if profile != 83:
                self.disable(f"Profile {profile}: this client supports profile 83 only")
            elif not self.enabled and not self.emergency_until:
                self.reason = "Connected; controls disabled"

    def tick(self, now):
        with self.lock:
            if self.emergency_until:
                if now < self.emergency_until:
                    self.send(control_packet(flags=KILL), "emergency_repeat")
                else:
                    self.emergency_until = 0
                    self.send(bytes((8, 1)), "disable_after_emergency")
                return
            if not self.enabled:
                return
            if not self.fresh(now):
                self.disable("Status replies lost; controls disabled")
                return
            if now - self.last_ui > 0.5:
                self.disable("UI stopped updating; controls disabled")
                return
            flags = HEADLESS if self.headless else 0
            if now < self.command_until:
                flags |= self.command_flag
                if self.command_flag == CALIBRATE:
                    self.send(control_packet(flags=CALIBRATE), "flight")
                    return
            self.send(control_packet(*self.axes, flags), "flight")

    def run(self):
        next_control, next_heartbeat = time.monotonic(), 0.0
        self.emit("udp_started", local_port=self.socket.getsockname()[1], target=f"{self.host}:{self.port}")
        while not self.stopping.is_set():
            now = time.monotonic()
            if now >= next_heartbeat:
                with self.lock:
                    self.send(b"\x01\x01", "heartbeat")
                next_heartbeat = now + 1.0
            for _ in range(64):
                try:
                    data, source = self.socket.recvfrom(2048)
                except BlockingIOError:
                    break
                except OSError:
                    break
                self.accept_response(data, source)
            if now >= next_control:
                self.tick(now)
                # Retain the 50 ms schedule through small Windows timer delays.
                next_control += 0.05
                if next_control <= now:
                    next_control = now + 0.05  # Skip missed periods rather than bursting.
            self.stopping.wait(max(0.001, min(0.01, next_control - time.monotonic())))

    def stop(self):
        self.disable("Disconnected")
        # Let an explicitly requested emergency pulse finish before closing its socket.
        remaining = max(0.0, self.emergency_until - time.monotonic())
        if remaining and self.thread and self.thread.is_alive():
            time.sleep(min(remaining + 0.06, 1.1))
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=0.5)
        self.socket.close()
