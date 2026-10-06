#!/usr/bin/env python3
"""Read-only checks of the services identified in RC UFO 1.9.6.

Uses only Python's standard library. Never sends flight, camera selection,
password-change, upload, or delete commands. No credential guessing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import ftplib
import hashlib
import http.client
import ipaddress
import json
import platform
import select
import socket
import struct
import subprocess
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit


BODY_LIMIT = 8192
HEADER_LIMIT = 65536


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def error_text(exc):
    return f"{type(exc).__name__}: {exc}"


def private_ipv4(value):
    try:
        addr = ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use a private IPv4 address, e.g. 192.168.1.1") from exc
    if not addr.is_private or addr.is_unspecified or addr.is_multicast:
        raise argparse.ArgumentTypeError("This probe is restricted to private/local IPv4 targets")
    return str(addr)


class Output:
    def __init__(self, directory, host):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=False)
        self.log = (directory / "probe.txt").open("w", encoding="utf-8")
        self.report = {
            "started_utc": timestamp(), "target": host,
            "python": platform.python_version(), "platform": platform.system(),
            "scope": "Read-only service checks, optional video playback, observed UDP heartbeat only",
            "checks": {}, "findings": [],
        }
        self.save()

    def say(self, message):
        print(message, flush=True)
        self.log.write(message + "\n")
        self.log.flush()

    def save(self):
        temp = self.directory / "probe.json.tmp"
        temp.write_text(json.dumps(self.report, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.directory / "probe.json")

    def record(self, name, result):
        self.report["checks"][name] = result
        self.save()
        self.say(f"{name}: {result.get('summary', result.get('error', 'complete'))}")

    def close(self):
        self.report["finished_utc"] = timestamp()
        self.save()
        self.log.close()


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ("href", "src") and value and len(self.values) < 100:
                self.values.append(value)


def local_media_paths(body, base, host):
    """Take only same-host media links under the APK's documented directories."""
    parser = Links()
    parser.feed(body.decode("utf-8", "replace"))
    result = []
    for value in parser.values:
        url = urlsplit(urljoin(base, value))
        try:
            allowed = url.scheme == "http" and url.hostname == host and url.port in (None, 80)
        except ValueError:
            continue
        if not allowed or url.username or url.password:
            continue
        path = url.path
        if not path.startswith(("/PHOTO/", "/DCIM/")):
            continue
        if not path.lower().endswith((".jpg", ".jpeg", ".png", ".avi", ".mp4")):
            continue
        if "\r" in path or "\n" in path:
            continue
        request_path = path + ("?" + url.query if url.query else "")
        if request_path not in result:
            result.append(request_path)
    return result


def media_signature(body):
    if body.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if body.startswith(b"RIFF") and body[8:12] == b"AVI ":
        return "AVI"
    if len(body) >= 12 and body[4:8] == b"ftyp":
        return "MP4-family"
    return None


def http_get(host, path, timeout, port=80):
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers={
            "User-Agent": "RC-UFO-local-research-probe/1.0",
            "Range": f"bytes=0-{BODY_LIMIT - 1}", "Connection": "close",
        })
        response = connection.getresponse()
        # No redirect following, credentials, or cookie reuse.
        body = response.read(BODY_LIMIT)
        headers = dict(response.getheaders())
        kind = media_signature(body)
        result = {
            "path": path, "status": response.status, "reason": response.reason,
            "headers": headers, "bytes_sampled": len(body),
            "sample_may_be_truncated": len(body) == BODY_LIMIT,
            "sample_sha256": hashlib.sha256(body).hexdigest(),
            "media_signature": kind,
        }
        if not kind:
            result["body_preview"] = body[:2048].decode("utf-8", "replace")
        return result, body
    finally:
        connection.close()


def check_http(host, timeout, extra_paths):
    results, candidates = [], []
    for path in dict.fromkeys(["/", "/DCIM/", "/PHOTO/", "/PHOTO/T/", "/PHOTO/O/"] + extra_paths):
        try:
            result, body = http_get(host, path, timeout)
            results.append(result)
            if result["status"] in (200, 206):
                candidates.extend(local_media_paths(body, f"http://{host}{path}", host))
        except (OSError, ValueError, http.client.HTTPException) as exc:
            results.append({"path": path, "error": error_text(exc)})
    tested = {r["path"] for r in results}
    for path in list(dict.fromkeys(candidates))[:3]:
        if path in tested:
            continue
        try:
            result, _ = http_get(host, path, timeout)
            results.append(result)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            results.append({"path": path, "error": error_text(exc)})
    exposed = [r["path"] for r in results if r.get("status") in (200, 206) and r.get("media_signature")]
    reachable = any("status" in r for r in results)
    return {"summary": f"{'responding' if reachable else 'no HTTP response'}; {len(exposed)} media samples accessible without credentials",
            "requests": results, "unauthenticated_media_samples": exposed}


class RTSP:
    def __init__(self, host, port, timeout):
        self.host, self.port = host, port
        self.socket = socket.create_connection((host, port), timeout=timeout)
        self.buffer = bytearray()
        self.sequence = 0
        self.deadline = None

    def fill(self, size):
        while len(self.buffer) < size:
            if self.deadline is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("RTSP read deadline exceeded")
                self.socket.settimeout(remaining)
            chunk = self.socket.recv(4096)
            if not chunk:
                raise EOFError("RTSP connection closed")
            self.buffer.extend(chunk)

    def item(self):
        self.fill(1)
        if self.buffer[0] == 36:  # RTSP interleaved binary frame ('$').
            self.fill(4)
            channel = self.buffer[1]
            size = struct.unpack("!H", self.buffer[2:4])[0]
            self.fill(4 + size)
            payload = bytes(self.buffer[4:4 + size])
            del self.buffer[:4 + size]
            return {"channel": channel, "payload": payload}
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) > HEADER_LIMIT:
                raise ValueError("RTSP header exceeds limit")
            self.fill(len(self.buffer) + 1)
        end = self.buffer.index(b"\r\n\r\n")
        if end > HEADER_LIMIT:
            raise ValueError("RTSP header exceeds limit")
        lines = bytes(self.buffer[:end]).decode("utf-8", "replace").split("\r\n")
        parts = lines[0].split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("RTSP/"):
            raise ValueError("Unexpected RTSP response")
        headers = {}
        for line in lines[1:]:
            key, sep, value = line.partition(":")
            if sep:
                headers[key.lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        if not 0 <= length <= HEADER_LIMIT:
            raise ValueError("RTSP body exceeds limit")
        self.fill(end + 4 + length)
        body = bytes(self.buffer[end + 4:end + 4 + length])
        del self.buffer[:end + 4 + length]
        return {"status": int(parts[1]), "status_line": lines[0], "headers": headers,
                "body": body.decode("utf-8", "replace")}

    def request(self, method, uri, headers=None):
        self.sequence += 1
        values = {"CSeq": str(self.sequence), "User-Agent": "RC-UFO-local-research-probe/1.0"}
        values.update(headers or {})
        text = f"{method} {uri} RTSP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in values.items()) + "\r\n"
        self.socket.sendall(text.encode("ascii"))
        self.deadline = time.monotonic() + self.socket.gettimeout()
        while time.monotonic() < self.deadline:
            reply = self.item()
            if "status" in reply:
                return reply
        raise TimeoutError("RTSP response deadline exceeded")


def video_track(sdp):
    in_video = False
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("m="):
            in_video = line.startswith("m=video ")
        if in_video and line.startswith("a=control:"):
            return line[len("a=control:"):]
    return None


def same_rtsp_target(uri, host, port):
    parsed = urlsplit(uri)
    return (parsed.scheme == "rtsp" and parsed.hostname == host and (parsed.port or 554) == port
            and not parsed.username and not parsed.password and "\r" not in uri and "\n" not in uri)


def udp_pair(local_ip):
    """Reserve adjacent RTP/RTCP ports, with an even RTP port."""
    for _ in range(100):
        rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            rtp.bind((local_ip, 0))
            port = rtp.getsockname()[1]
            if port % 2 or port == 65535:
                rtp.close()
                rtcp.close()
                continue
            rtcp.bind((local_ip, port + 1))
            return rtp, rtcp
        except OSError:
            rtp.close()
            rtcp.close()
    raise OSError("Could not reserve adjacent UDP video ports")


def observe_rtp(result, packet):
    if len(packet) < 12 or packet[0] >> 6 != 2 or 192 <= packet[1] <= 223:
        return
    offset = 12 + 4 * (packet[0] & 15)
    if len(packet) < offset:
        return
    if packet[0] & 16:
        if len(packet) < offset + 4:
            return
        offset += 4 + 4 * struct.unpack("!H", packet[offset + 2:offset + 4])[0]
        if len(packet) < offset:
            return
    end = len(packet)
    if packet[0] & 32:
        padding = packet[-1]
        if padding == 0 or padding > end - offset:
            return
        end -= padding
    result["rtp_packets"] += 1
    result["rtp_bytes_received"] += len(packet)
    result["unauthenticated_media_observed"] = True
    result.setdefault("first_rtp_header_hex", packet[:12].hex(" "))
    payload_type = packet[1] & 127
    types = result.setdefault("rtp_payload_types", [])
    if payload_type not in types:
        types.append(payload_type)
    if payload_type == 26 and end >= offset + 8:
        header = packet[offset:offset + 8]
        info = {"type": header[4], "q": header[5], "width_blocks": header[6],
                "height_blocks": header[7], "fragment_offset": int.from_bytes(header[1:4], "big")}
        if header[6] and header[7]:
            info.update(width=header[6] * 8, height=header[7] * 8)
        result.setdefault("first_rtp_jpeg_header", info)


def check_rtsp(host, port, timeout, stream_seconds, transport="auto"):
    uri = f"rtsp://{host}:{port}/webcam"
    result = {"url": uri, "responses": {}, "interleaved_frames": 0, "rtp_packets": 0,
              "rtp_bytes_received": 0, "unauthenticated_media_observed": False}
    client, session, udp_sockets = None, None, None
    try:
        client = RTSP(host, port, timeout)
        result["local_address"] = list(client.socket.getsockname())
        for method, headers in [("OPTIONS", {}), ("DESCRIBE", {"Accept": "application/sdp"})]:
            response = client.request(method, uri, headers)
            result["responses"][method] = response
            if method == "DESCRIBE" and response["status"] in (401, 403):
                result["summary"] = f"{method} requires authentication or denies access ({response['status']})"
                return result
            if method == "DESCRIBE" and response["status"] != 200:
                result["summary"] = f"DESCRIBE returned {response['status']}; stream access not established"
                return result
        description = result["responses"]["DESCRIBE"]
        result["sdp_without_credentials"] = True
        result["summary"] = "SDP accessible without credentials; playback not tested"
        if stream_seconds <= 0:
            return result
        track = video_track(description["body"])
        if not track or track == "*":
            result["summary"] += "; no usable video track URI"
            return result
        base = description["headers"].get("content-base", description["headers"].get("content-location", uri + "/"))
        track_uri = urljoin(urljoin(uri, base), track)
        if not same_rtsp_target(track_uri, host, port):
            result["summary"] += "; refused a track URI outside the configured target"
            return result
        selected_transport = "udp" if transport == "udp" else "tcp"
        if selected_transport == "udp":
            udp_sockets = udp_pair(client.socket.getsockname()[0])
            rtp_port = udp_sockets[0].getsockname()[1]
            transport_header = f"RTP/AVP;unicast;client_port={rtp_port}-{rtp_port + 1}"
        else:
            transport_header = "RTP/AVP/TCP;unicast;interleaved=0-1"
        setup = client.request("SETUP", track_uri, {"Transport": transport_header})
        result["responses"]["SETUP"] = setup
        if setup["status"] == 461 and transport == "auto":
            selected_transport = "udp"
            udp_sockets = udp_pair(client.socket.getsockname()[0])
            rtp_port = udp_sockets[0].getsockname()[1]
            client.socket.settimeout(timeout)
            setup = client.request("SETUP", track_uri, {
                "Transport": f"RTP/AVP;unicast;client_port={rtp_port}-{rtp_port + 1}"})
            result["responses"]["SETUP_UDP"] = setup
        result["selected_transport"] = selected_transport
        if udp_sockets:
            result["udp_client_ports"] = [s.getsockname()[1] for s in udp_sockets]
        if setup["status"] != 200:
            result["summary"] = f"SDP accessible; {selected_transport.upper()} SETUP returned {setup['status']}; media access not established"
            return result
        session = setup["headers"].get("session", "").split(";", 1)[0]
        if not session or "\r" in session or "\n" in session:
            result["summary"] = "SETUP returned no usable session identifier"
            return result
        client.socket.settimeout(timeout)
        play = client.request("PLAY", uri, {"Session": session, "Range": "npt=0.000-"})
        result["responses"]["PLAY"] = play
        if play["status"] != 200:
            result["summary"] = f"PLAY returned {play['status']}; media access not established"
            return result
        deadline = time.monotonic() + stream_seconds
        client.deadline = deadline
        while time.monotonic() < deadline:
            if selected_transport == "udp":
                ready, _, _ = select.select(udp_sockets, [], [], max(0, deadline - time.monotonic()))
                for receiving_socket in ready:
                    payload, source = receiving_socket.recvfrom(65535)
                    if source[0] != host or receiving_socket is not udp_sockets[0]:
                        continue
                    result.setdefault("udp_rtp_source", list(source))
                    observe_rtp(result, payload)
                continue
            client.socket.settimeout(max(0.01, min(timeout, deadline - time.monotonic())))
            try:
                item = client.item()
            except socket.timeout:
                break
            if "payload" not in item:
                continue
            result["interleaved_frames"] += 1
            observe_rtp(result, item["payload"])
        result["unauthenticated_media_observed"] = result["rtp_packets"] > 0
        result["summary"] = (f"video-track RTP received over {selected_transport.upper()} without credentials ({result['rtp_packets']} packets); frame decoding not tested"
                             if result["rtp_packets"] else "PLAY accepted without credentials, but no RTP observed")
        if not result["rtp_packets"] and selected_transport == "udp":
            result["summary"] += "; Windows firewall or media delivery may need checking"
    except (OSError, EOFError, ValueError, UnicodeError) as exc:
        result["error"] = error_text(exc)
        result["summary"] = (f"RTP received without credentials ({result['rtp_packets']} packets); subsequent error: {result['error']}"
                             if result["unauthenticated_media_observed"] else f"probe incomplete: {result['error']}")
    finally:
        if client:
            if session:
                try:
                    client.socket.settimeout(timeout)
                    result["responses"]["TEARDOWN"] = client.request("TEARDOWN", uri, {"Session": session})
                except (OSError, EOFError, ValueError, UnicodeError) as exc:
                    result["teardown_error"] = error_text(exc)
            client.socket.close()
        if udp_sockets:
            for udp_socket in udp_sockets:
                udp_socket.close()
    return result


def check_network(host):
    result = {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route_socket:
            # Select a route without sending a datagram.
            route_socket.connect((host, 7099))
            local_ip = route_socket.getsockname()[0]
        result["selected_local_ip"] = local_ip
        subnet_matches = ipaddress.IPv4Address(host) in ipaddress.IPv4Network(local_ip + "/24", strict=False)
        result["same_assumed_24_subnet"] = subnet_matches
        result["summary"] = f"selected source address {local_ip} for {host}"
        if not subnet_matches:
            result["summary"] += "; different /24 subnet: confirm the PC is on the drone Wi-Fi (a routed connection could still work)"
    except OSError as exc:
        result["error"] = error_text(exc)
        result["summary"] = "no usable route to the target; connect the PC to the drone Wi-Fi and retry"
    if platform.system() == "Windows":
        for label, command in [("ipconfig", ["ipconfig"]), ("ipv4_routes", ["route", "print", "-4"])]:
            try:
                completed = subprocess.run(command, capture_output=True, timeout=5)
                result[label] = completed.stdout.decode("utf-8", "replace")[:32768]
            except (OSError, subprocess.SubprocessError) as exc:
                result[label + "_error"] = error_text(exc)
    return result


class TargetFTP(ftplib.FTP):
    def makepasv(self):
        # Never follow a server-provided PASV address to another host.
        _, port = super().makepasv()
        return self.host, port


def check_ftp(host, timeout):
    attempts = []
    for user, password, label in [("anonymous", "local-probe@example.invalid", "anonymous"),
                                  ("ftp", "ftp", "credentials from APK: ftp/ftp")]:
        attempt = {"login": label, "accepted": False}
        ftp = TargetFTP(timeout=timeout)
        try:
            attempt["banner"] = ftp.connect(host, 21)
            attempt["login_response"] = ftp.login(user, password)
            attempt["accepted"] = True
            # List names only, never RETR, STOR, DELE, or other file changes.
            with ftp.transfercmd("NLST /0/") as data_socket:
                data_socket.settimeout(timeout)
                chunks = bytearray()
                deadline = time.monotonic() + timeout
                while len(chunks) < BODY_LIMIT and time.monotonic() < deadline:
                    data_socket.settimeout(max(0.01, deadline - time.monotonic()))
                    chunk = data_socket.recv(min(4096, BODY_LIMIT - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                attempt["listing"] = chunks.decode("utf-8", "replace")
                attempt["listing_may_be_truncated"] = len(chunks) == BODY_LIMIT
        except (OSError, EOFError, ftplib.Error) as exc:
            attempt["error"] = error_text(exc)
        finally:
            ftp.close()
        attempts.append(attempt)
        if "banner" not in attempt:
            break
    accepted = [a["login"] for a in attempts if a["accepted"]]
    return {"summary": "accepted: " + ", ".join(accepted) if accepted else "no successful FTP login",
            "attempts": attempts}


def check_tcp(host, timeout):
    try:
        with socket.create_connection((host, 5000), timeout=timeout) as connection:
            return {"summary": "TCP 5000 accepts connections; no application data sent",
                    "connected": True, "local_address": list(connection.getsockname())}
    except OSError as exc:
        return {"summary": "TCP 5000 unavailable", "connected": False, "error": error_text(exc)}


def sanitize_udp(data):
    """Mask known password fields in reports, while retaining the profile byte."""
    hidden = set()
    if data and data[0] in (80, 81, 82, 85, 88):
        gl = data[0] in (82, 85, 88)
        if gl and len(data) >= 19:
            hidden.update(range(11, 19))
        elif len(data) >= 15:
            hidden.update(range(7, 15))
    return {"length": len(data), "hex": " ".join("??" if i in hidden else f"{b:02x}" for i, b in enumerate(data)),
            "password_fields_redacted": bool(hidden), "first_byte_decimal": data[0] if data else None}


def check_udp(host, timeout, port=7099):
    received = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("", 0))
        started = time.monotonic()
        for index in range(3):
            delay = started + index - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            sock.sendto(b"\x01\x01", (host, port))
            deadline = time.monotonic() + min(timeout, 1.0)
            while time.monotonic() < deadline and len(received) < 20:
                sock.settimeout(max(0.01, deadline - time.monotonic()))
                try:
                    data, source = sock.recvfrom(4096)
                except socket.timeout:
                    break
                if source[0] != host or source[1] != port:
                    continue
                packet = sanitize_udp(data)
                packet["source"] = list(source)
                received.append(packet)
        local_port = sock.getsockname()[1]
    profiles = sorted({p["first_byte_decimal"] for p in received if p["first_byte_decimal"] not in (None, 102)})
    return {"summary": f"{len(received)} replies to three observed heartbeats; candidate identifiers: {profiles}",
            "sent_payload_hex": "01 01", "local_port": local_port, "packets": received,
            "candidate_profile_identifiers": profiles,
            "note": "A reply is not proof that flight commands are accepted; no flight packets were sent."}


def findings(checks):
    result = []
    rtsp = checks.get("rtsp", {})
    if rtsp.get("unauthenticated_media_observed"):
        result.append("Video-track RTP received without RTSP credentials; confirms media delivery to this client, not decoded frames or access from every LAN peer.")
    elif rtsp.get("sdp_without_credentials"):
        result.append("RTSP stream description is accessible without credentials; media access is not confirmed.")
    for path in checks.get("http", {}).get("unauthenticated_media_samples", []):
        result.append(f"HTTP media sample readable without credentials: {path}. Only the initial bytes were read.")
    for attempt in checks.get("ftp", {}).get("attempts", []):
        if attempt["accepted"]:
            result.append(f"FTP login accepted ({attempt['login']}); file permissions depend on the separate listing result.")
    if not result:
        result.append("No unauthenticated media access or successful FTP login demonstrated. Closed ports, timeouts, 404s, and unsupported RTSP transport are inconclusive about security.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=private_ipv4, default="192.168.1.1")
    parser.add_argument("--timeout", type=float, default=3.0, help="Per-operation timeout, seconds (default: 3)")
    parser.add_argument("--stream-seconds", type=float, default=3.0, help="Brief RTSP playback; 0 tests only OPTIONS/DESCRIBE")
    parser.add_argument("--transport", choices=("auto", "tcp", "udp"), default="auto", help="Video transport; auto retries UDP when TCP SETUP returns 461")
    parser.add_argument("--skip-udp", action="store_true", help="Do not send the app's 01 01 heartbeat")
    parser.add_argument("--skip-ftp", action="store_true")
    parser.add_argument("--http-path", action="append", default=[], help="Known media path to check, e.g. /PHOTO/O/example.jpg; repeatable")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "results" / "probes")
    args = parser.parse_args()
    if not 0 < args.timeout <= 15 or not 0 <= args.stream_seconds <= 15:
        parser.error("Timeout must be >0 and <=15; stream duration must be 0-15")
    for path in args.http_path:
        if not path.startswith("/") or path.startswith("//") or "\r" in path or "\n" in path:
            parser.error("--http-path must be a local absolute URL path")
    directory = args.output_dir / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = Output(directory, args.host)
    try:
        output.say(f"Target: {args.host}; output: {directory}")
        output.say("No flight, camera-selection, password-change, upload, or delete commands will be sent.")
        phases = [("network", lambda: check_network(args.host)),
                  ("rtsp", lambda: check_rtsp(args.host, 7070, args.timeout, args.stream_seconds, args.transport)),
                  ("http", lambda: check_http(args.host, args.timeout, args.http_path)),
                  ("tcp_5000", lambda: check_tcp(args.host, args.timeout))]
        if not args.skip_ftp:
            phases.append(("ftp", lambda: check_ftp(args.host, args.timeout)))
        if not args.skip_udp:
            phases.append(("udp_heartbeat", lambda: check_udp(args.host, args.timeout)))
        for name, check in phases:
            output.say(f"Checking {name}...")
            try:
                output.record(name, check())
            except Exception as exc:
                output.record(name, {"error": error_text(exc), "summary": "check failed; see JSON error"})
        output.report["findings"] = findings(output.report["checks"])
        output.say("\nFindings:")
        for finding in output.report["findings"]:
            output.say("- " + finding)
        output.say("\nConnect a second client and repeat to evaluate access from another LAN device.")
    except KeyboardInterrupt:
        output.report["interrupted"] = True
        output.say("Interrupted; completed checks have been saved.")
    finally:
        output.close()
    print(f"\nSaved {directory / 'probe.json'}\nSaved {directory / 'probe.txt'}", flush=True)


if __name__ == "__main__":
    main()
