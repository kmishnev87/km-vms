"""Host-network discovery worker controlled ONLY through a private Unix socket."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import socketserver
import threading
import time
import uuid

from app.services.camera_discovery_network import (
    MAX_CANDIDATES, MAX_HOSTS, DEFAULT_ONVIF_PORTS, DEFAULT_RTSP_PORTS,
    address_discovery, host_networks, merge_candidate, multicast_discovery,
    parse_ports, parse_ranges, unicast_discovery,
)

SOCKET = os.environ.get("CAMERA_DISCOVERY_SOCKET", "/run/km-vms-discovery/control.sock")
TERMINAL = {"completed", "cancelled", "partial", "failed"}


class DiscoveryJobs:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {}

    def _purge(self):
        now = time.monotonic()
        for key, job in list(self.jobs.items()):
            if job["status"] in TERMINAL and now - job["started"] > 900:
                self.jobs.pop(key, None)
        terminal = sorted((job for job in self.jobs.values() if job["status"] in TERMINAL), key=lambda job: job["started"])
        for job in terminal[:max(0, len(self.jobs) - 48)]:
            self.jobs.pop(job["id"], None)

    def start(self, owner, payload):
        if not isinstance(owner, str) or not owner.isdecimal() or len(owner) > 20:
            raise ValueError("invalid_owner")
        networks, hosts = parse_ranges(payload.get("networks", []))
        protocols = payload.get("protocols", ["onvif", "rtsp"])
        if not isinstance(protocols, list) or not protocols or len(protocols) > 2 or any(value not in {"onvif", "rtsp"} for value in protocols):
            raise ValueError("invalid_protocols")
        local = payload.get("local", True)
        if type(local) is not bool or not local and not hosts:
            raise ValueError("empty_search")
        onvif_ports = parse_ports(payload.get("onvif_ports"), DEFAULT_ONVIF_PORTS)
        rtsp_ports = parse_ports(payload.get("rtsp_ports"), DEFAULT_RTSP_PORTS)
        with self.lock:
            self._purge()
            active = [job for job in self.jobs.values() if job["status"] not in TERMINAL]
            if any(job["owner"] == owner for job in active) or len(active) >= 2:
                raise RuntimeError("discovery_busy")
            job = {"id": str(uuid.uuid4()), "owner": owner, "status": "running", "phase": "local" if local else "addresses",
                   "started": time.monotonic(), "finished": None, "cancel": threading.Event(), "candidates": {},
                   "checked_hosts": 0, "total_hosts": len(hosts), "warnings": [], "code": "searching"}
            self.jobs[job["id"]] = job
        thread = threading.Thread(target=self._run, args=(job, local, networks, hosts, protocols, onvif_ports, rtsp_ports), daemon=True)
        thread.start()
        return self.snapshot(owner, job["id"])

    def _run(self, job, local, networks, hosts, protocols, onvif_ports, rtsp_ports):
        deadline = job["started"] + 150

        def cancelled():
            return job["cancel"].is_set() or time.monotonic() >= deadline

        def publish(item):
            if cancelled():
                return
            if not set(item.get("protocols") or []) & set(protocols):
                return
            with self.lock:
                if len(job["candidates"]) >= MAX_CANDIDATES and item["host"] not in job["candidates"]:
                    if "result_limit_reached" not in job["warnings"]:
                        job["warnings"].append("result_limit_reached")
                    return
                job["candidates"][item["host"]] = merge_candidate(job["candidates"].get(item["host"]), item)

        def progress(count):
            with self.lock:
                job["checked_hosts"] = count

        try:
            if local and "onvif" in protocols and not cancelled():
                try:
                    multicast_discovery(4, cancelled, publish)
                except ImportError:
                    job["warnings"].append("discovery_dependency_unavailable")
                except Exception:
                    job["warnings"].append("local_discovery_unavailable")
            if hosts and not cancelled():
                with self.lock:
                    job["phase"] = "addresses"
                if "onvif" in protocols:
                    unicast_discovery(hosts, networks, cancelled, publish)
                address_discovery(hosts, onvif_ports, rtsp_ports, protocols, cancelled, publish, progress)
            with self.lock:
                if job["cancel"].is_set():
                    job.update(status="cancelled", code="cancelled")
                elif time.monotonic() >= deadline:
                    job.update(status="partial", code="time_budget_reached")
                elif job["warnings"]:
                    job.update(status="partial", code="partial_results")
                else:
                    job.update(status="completed", code="ok" if job["candidates"] else "no_devices_found")
        except Exception:
            # No packet contents, URLs, credentials or raw exceptions cross the boundary.
            with self.lock:
                job.update(status="partial" if job["candidates"] else "failed", code="discovery_failed")
        finally:
            with self.lock:
                job["finished"] = time.monotonic()
                job["phase"] = "finished"

    def snapshot(self, owner, job_id, cancel=False):
        with self.lock:
            self._purge()
            job = self.jobs.get(job_id)
            if not job or job["owner"] != owner:
                raise KeyError("discovery_not_found")
            if cancel and job["status"] not in TERMINAL:
                job["cancel"].set()
            ended = job["finished"] or time.monotonic()
            rows = sorted(job["candidates"].values(), key=lambda item: tuple(map(int, item["host"].split("."))))
            return {"ok": job["status"] != "failed", "discovery_supported": True, "job_id": job["id"],
                    "status": job["status"], "phase": job["phase"], "code": job["code"],
                    "cancel_requested": job["cancel"].is_set(), "elapsed_seconds": round(ended - job["started"], 1),
                    "checked_hosts": job["checked_hosts"], "total_hosts": job["total_hosts"],
                    "candidates": rows, "warnings": list(job["warnings"]), "limits": {"max_hosts": MAX_HOSTS, "max_candidates": MAX_CANDIDATES}}


JOBS = DiscoveryJobs()


class UnixHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    address_family = __import__("socket").AF_UNIX
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _respond(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16384:
                raise ValueError("invalid_request_size")
            self.connection.settimeout(5)
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("invalid_request")
            owner = str(payload.get("owner", ""))
            if self.path == "/networks":
                result = {"ok": True, "networks": host_networks(), "limits": {"max_hosts": MAX_HOSTS}, "ipv4_only": True}
            elif self.path == "/jobs":
                result = JOBS.start(owner, payload)
            elif self.path in {"/status", "/cancel"}:
                result = JOBS.snapshot(owner, str(payload.get("job_id", "")), cancel=self.path == "/cancel")
            else:
                self._respond(404, {"code": "not_found"})
                return
            self._respond(200, result)
        except ValueError as exc:
            code = str(exc)
            allowed = {"invalid_networks", "invalid_network", "private_ipv4_required", "network_too_large", "too_many_hosts",
                       "invalid_ports", "invalid_protocols", "empty_search", "invalid_owner", "invalid_request_size"}
            self._respond(400, {"code": code if code in allowed else "invalid_request"})
        except KeyError:
            self._respond(404, {"code": "discovery_not_found"})
        except RuntimeError:
            self._respond(409, {"code": "discovery_busy"})
        except Exception:
            self._respond(503, {"code": "discovery_unavailable"})


def main():
    root = Path(SOCKET).parent
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    path = Path(SOCKET)
    if path.is_symlink() or path.exists() and not __import__("stat").S_ISSOCK(path.stat().st_mode):
        raise RuntimeError("unsafe_discovery_socket")
    path.unlink(missing_ok=True)
    server = UnixHTTPServer(SOCKET, Handler)
    path.chmod(0o600)
    try:
        server.serve_forever(poll_interval=1)
    finally:
        server.server_close()
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
