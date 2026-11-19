"""Bounded local AI dispatch; no archive, database or camera I/O in this module."""
from __future__ import annotations

import json
import os
import select
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from recorder_core.ai_rules import RuleEngine

MAX_SOURCES = 16
MAX_QUEUE_BYTES = 8 * 1024 * 1024
MAX_PACKET_BYTES = 2 * 1024 * 1024
MAX_EXTRA_BYTES = 64 * 1024
AI_KINDS = frozenset({"person", "vehicle", "animal", "motion"})
STALE_SAMPLE = 6.0
FRAME_GRACE = 40.0
START_TIMEOUT = 40.0
INFERENCE_TIMEOUT = 10.0
RECOVERY_PROGRESS = 30.0
RETRY_DELAYS = (30, 120, 600)
MAX_RESIDENT_KIB = 1200 * 1024


def stamp(value):
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


@dataclass(frozen=True, slots=True)
class Frame:
    camera: int
    generation: str
    stream: str
    codec: str
    extra: bytes
    data: bytes
    arrival: float
    captured: float
    advanced: bool = False

    @property
    def size(self):
        return len(self.data) + len(self.extra)


@dataclass(slots=True)
class Source:
    generation: str
    kinds: frozenset
    since: float
    stream: str | None = None
    pending: Frame | None = None
    offered: float = -1e20
    completed: float | None = None
    first_after_fault: float | None = None
    activity: float | None = None
    previous_kinds: frozenset = field(default_factory=frozenset)
    active_kinds: frozenset = field(default_factory=frozenset)
    scene_active: bool = False
    policy_revision: int | None = None
    rules: RuleEngine | None = None
    rules_invalid: bool = False


@dataclass(frozen=True, slots=True)
class Observation:
    phase: str
    activity: float | None
    info: dict


class Worker:
    def __init__(self, prefer_gpu):
        env = {"PATH": os.defpath + ":/usr/local/bin", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
               "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "OV_TELEMETRY_DISABLED": "1", "DO_NOT_TRACK": "1"}
        prefix = Path("/opt/kmvms-intel")
        if prefer_gpu and prefix.is_dir():
            library = prefix / "usr/lib/x86_64-linux-gnu"
            env["LD_LIBRARY_PATH"] = os.pathsep.join(map(str, (library, library / "intel-opencl", prefix / "usr/local/lib")))
            env["OCL_ICD_VENDORS"] = str(prefix / "etc/OpenCL/vendors")
        # No service environment, DB DSN, device URLs, or inherited descriptors.
        opts = {}
        if os.geteuid() == 0:
            render_groups = []
            if prefer_gpu:
                for device in Path("/dev/dri").glob("renderD*"):
                    render_groups.append(device.stat().st_gid)
            opts = {"user": 65534, "group": 65534, "extra_groups": tuple(sorted(set(render_groups)))}
        self.process = subprocess.Popen(
            [sys.executable, "-m", "recorder_core.ai_worker", "auto" if prefer_gpu else "cpu"],
            cwd=Path(__file__).resolve().parents[1], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True, bufsize=0, **opts,
        )
        self.input = self.process.stdin.fileno()
        self.output = self.process.stdout.fileno()
        self.buffer = bytearray()
        self.last_rss_check = 0.0
        os.set_blocking(self.input, False)

    def ready(self):
        return self._reply(time.monotonic() + START_TIMEOUT)

    def analyze(self, frame):
        header = json.dumps({"stream": frame.stream, "camera": frame.camera, "codec": frame.codec, "rules": frame.advanced}).encode()
        raw = struct.pack("!III", len(header), len(frame.extra), len(frame.data))
        deadline = time.monotonic() + INFERENCE_TIMEOUT
        for block in (raw, header, frame.extra, frame.data):
            left = memoryview(block)
            while left:
                if not select.select([], [self.input], [], max(0, deadline - time.monotonic()))[1]:
                    raise TimeoutError
                count = os.write(self.input, left)
                if count <= 0:
                    raise BrokenPipeError
                left = left[count:]
        return self._reply(deadline)

    def _reply(self, deadline):
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                reply = bytes(self.buffer[:newline])
                del self.buffer[:newline+1]
                now = time.monotonic()
                if now - self.last_rss_check >= 2:
                    self.last_rss_check = now
                    if self._resident() > MAX_RESIDENT_KIB:
                        raise MemoryError
                return json.loads(reply)
            if len(self.buffer) >= 8192:
                raise ValueError("worker_reply_limit")
            if not select.select([self.output], [], [], max(0, deadline - time.monotonic()))[0]:
                raise TimeoutError
            data = os.read(self.output, min(4096, 8192 - len(self.buffer)))
            if not data:
                raise EOFError
            self.buffer.extend(data)

    def _resident(self):
        try:
            with open(f"/proc/{self.process.pid}/status", encoding="ascii", errors="ignore") as stream:
                for line in stream:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1])
        except FileNotFoundError:
            pass
        return 0

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()


class AiSupervisor:
    def __init__(self, worker_factory=Worker, *, event_sink=None):
        self.cond = threading.Condition()
        self.sources = {}
        self.limit_ids = frozenset()
        self.queue_bytes = 0
        self.cursor = -1
        self.closed = False
        self.thread = None
        self.worker_factory = worker_factory
        self.event_sink = event_sink
        self.gpu_allowed = False
        self.gpu_rejected = False
        self.change_device = False
        self.backend = {}
        self.failures = 0
        self.failed_at = self.next_at = None
        self.next_clock = 0.0
        self.success_since = None
        self.reason = None
        self.incident = 0
        self.last_manual = 0.0
        self.reported_incident = None
        self.reported_exhaustion = False
        self.transitions = deque(maxlen=12)

    def configure(self, configs, *, gpu_allowed, policy_revisions=None):
        now = time.monotonic()
        with self.cond:
            admitted = sorted(configs)[:MAX_SOURCES]
            self.limit_ids = frozenset(configs) - set(admitted)
            for i in set(self.sources) - set(admitted):
                self._discard(self.sources.pop(i))
            for i in admitted:
                generation, kinds, *advanced = configs[i]
                old = self.sources.get(i)
                if old is None or old.generation != generation:
                    if old:
                        self._discard(old)
                    source = Source(generation, kinds, now)
                    if advanced:
                        try:
                            source.rules = RuleEngine(*advanced[0])
                        except (KeyError, TypeError, ValueError):
                            source.rules_invalid = True
                    self.sources[i] = source
                self.sources[i].policy_revision = (policy_revisions or {}).get(i)
            if self.gpu_allowed != gpu_allowed:
                self.gpu_allowed = gpu_allowed
                if not gpu_allowed and self.backend and self.backend.get("backend") == "gpu":
                    self.change_device = True
                self.gpu_rejected = False
            if self.sources and self.thread is None:
                self.thread = threading.Thread(target=self._run, name="local-ai", daemon=True)
                self.thread.start()
            self.cond.notify()

    def _discard(self, source):
        if source.pending:
            self.queue_bytes -= source.pending.size
            source.pending = None

    def offer(self, camera, generation, stream, packet):
        size = packet.size
        if not 0 < size <= MAX_PACKET_BYTES:
            return False
        now = time.monotonic()
        # Capture never waits for the dispatch thread (which never holds this
        # lock during inference). Drop only the AI copy on contention.
        if not self.cond.acquire(blocking=False):
            return False
        try:
            source = self.sources.get(camera)
            if self.closed or source is None or source.generation != generation:
                return False
            if source.stream != stream:
                self._discard(source)
                source.stream = stream
                source.previous_kinds = frozenset()
                source.active_kinds = frozenset()
                source.scene_active = False
                if source.rules:
                    source.rules.reset()
            elif now - source.offered < .5:
                return False
            ctx = packet.stream.codec_context
            extra = ctx.extradata or b""
            if len(extra) > MAX_EXTRA_BYTES:
                return False
            old = source.pending.size if source.pending else 0
            if self.queue_bytes - old + size + len(extra) > MAX_QUEUE_BYTES:
                return False
            source.offered = now
            source.pending = Frame(camera, generation, stream, ctx.name, extra, bytes(packet), now, time.time(), source.rules is not None)
            self.queue_bytes += source.pending.size - old
            self.cond.notify()
            return True
        finally:
            self.cond.release()

    def select_stream(self, camera, generation, stream):
        # Stream changes are rare. Fence in-flight observations from the old
        # input immediately, before accepting the first sample of the new one.
        with self.cond:
            source = self.sources.get(camera)
            if source and source.generation == generation and source.stream != stream:
                self._discard(source)
                source.stream = stream
                source.previous_kinds = frozenset()
                source.active_kinds = frozenset()
                source.scene_active = False
                source.offered = -1e20
                if source.rules:
                    source.rules.reset()

    def observe(self, camera, generation):
        now = time.monotonic()
        with self.cond:
            source = self.sources.get(camera)
            if source is None or source.generation != generation:
                phase = "stalled" if camera in self.limit_ids else "connecting"
                return Observation(phase, None, {"phase": phase,
                    "reason": "capacity_limited" if camera in self.limit_ids else None})
            if self.failures and not self.backend:
                phase = "exhausted" if self.next_at is None else "failed"
            elif not self.backend:
                phase = "connecting"
            elif source.completed is None and now - source.since < FRAME_GRACE:
                phase = "connecting"
            elif now - (source.completed or source.since) >= FRAME_GRACE:
                phase = "stalled"
            elif self.failures and (source.first_after_fault is None or now - source.first_after_fault < RECOVERY_PROGRESS):
                phase = "recovering"
            else:
                phase = "healthy"
            incident = (None if not self.failures else {"reason": self.reason, "failed_attempts": self.failures,
                        "last_failure_at": stamp(self.failed_at), "next_attempt_at": stamp(self.next_at)})
            rules_view = ({"rules_view": "invalid_rules"} if source.rules_invalid
                          else {"rules_view": source.rules.view}
                          if source.rules is not None and phase == "healthy" else {})
            return Observation(phase, source.activity, {**self.backend, **rules_view, "phase": phase,
                "reason": "no_usable_frames" if phase == "stalled" else self.reason if incident else None,
                "incident": incident, "retry_token": str(self.incident) if phase == "exhausted" else None})

    def retry(self, token):
        with self.cond:
            now = time.time()
            if (not self.failures or self.next_at is not None or self.backend or str(self.incident) != token
                    or now - max(self.failed_at or 0, self.last_manual) < 30):
                return False
            self.last_manual = now
            self.incident += 1    # Ignore the same durable request after admission.
            self.next_at = now
            self.next_clock = time.monotonic()
            self.cond.notify()
            return True

    def _take(self, now):
        ids = sorted(self.sources)
        for i in [i for i in ids if i > self.cursor] + [i for i in ids if i <= self.cursor]:
            source = self.sources[i]
            frame = source.pending
            if frame:
                self._discard(source)
                if now - frame.arrival <= STALE_SAMPLE:
                    self.cursor = i
                    return frame
        return None

    def _accept(self, frame, result):
        now = time.monotonic()
        source = self.sources.get(frame.camera)
        if (source is None or source.generation != frame.generation or source.stream != frame.stream
                or result.get("phase") != "sample" or now - frame.arrival > STALE_SAMPLE):
            return
        if source.completed is None or now - source.completed > 8:
            source.previous_kinds = frozenset()
            source.active_kinds = frozenset()
            source.scene_active = False
        if source.completed is None or now - source.completed >= FRAME_GRACE:
            source.first_after_fault = now
        source.completed = now     # Empty detections are successful progress.
        if source.first_after_fault is None:
            source.first_after_fault = now
        starts = []
        if source.rules is not None:
            hits = source.rules.process(result, frame.captured)
            if any(hit.record for hit in hits):
                source.activity = frame.captured
            starts.extend((hit.rule_id, hit.kind, hit.object_kinds) for hit in hits if hit.started)
        elif not source.rules_invalid:
            kinds = frozenset(item["kind"] for item in result["objects"] if item.get("kind") in AI_KINDS)
            active = source.previous_kinds & kinds & source.kinds
            scene = bool(result.get("scene_change") and "motion" in source.kinds)
            if active or scene:
                source.activity = frame.captured
            starts.extend((None, "presence", (kind,)) for kind in sorted(active - source.active_kinds) if kind != "motion")
            if scene and not source.scene_active:
                starts.append((None, "scene", ()))
            source.active_kinds, source.scene_active = active, scene
            source.previous_kinds = kinds
        if self.failures:
            if self.success_since is None or now - self.success_since[1] > FRAME_GRACE:
                self.success_since = (now, now)
            else:
                self.success_since = (self.success_since[0], now)
            if now - self.success_since[0] >= RECOVERY_PROGRESS:
                if self.reported_incident is not None:
                    self._transition("recovered")
                self.reported_incident = None
                self.reported_exhaustion = False
                self.failures = 0
                self.failed_at = self.next_at = self.reason = None
        return source.policy_revision, starts

    def _failure(self, reason):
        self.backend = {}
        self.failures += 1
        self.incident += 1
        self.failed_at = time.time()
        delay = RETRY_DELAYS[self.failures-1] if self.failures <= len(RETRY_DELAYS) else None
        self.next_at = self.failed_at + delay if delay is not None else None
        self.next_clock = time.monotonic() + (delay or 0)
        self.reason = reason
        self.success_since = None
        if self.reported_incident is None:
            self.reported_incident = self.incident
            self.reported_exhaustion = False
            self._transition("failed")
        if delay is None and not self.reported_exhaustion:
            self.reported_exhaustion = True
            self._transition("exhausted")
        for source in self.sources.values():
            source.completed = source.first_after_fault = None
            source.previous_kinds = frozenset()
            if source.rules:
                source.rules.reset()

    def _transition(self, transition):
        self.transitions.append((transition, {
            "incident": self.reported_incident, "reason": self.reason,
            "failed_attempts": self.failures, "camera_ids": sorted(self.sources),
        }))

    def drain_transitions(self):
        with self.cond:
            result = tuple(self.transitions)
            self.transitions.clear()
            return result

    def _run(self):
        worker = None
        while True:
            action = None
            with self.cond:
                if self.closed:
                    break
                if not self.sources or self.change_device:
                    if worker:
                        action = "stop"
                    self.change_device = False
                    self.backend = {}
                    if not self.sources:
                        self.failures = 0
                        self.failed_at = self.next_at = self.reason = self.success_since = None
                        self.gpu_rejected = False
                        self.reported_incident = None
                        self.reported_exhaustion = False
                elif worker is None:
                    if not self.failures or (self.next_at is not None and self.next_clock <= time.monotonic()):
                        action = "start"
                        prefer_gpu = self.gpu_allowed and not self.gpu_rejected
                else:
                    frame = self._take(time.monotonic())
                    if frame:
                        action = "analyze"
                if action is None:
                    self.cond.wait(timeout=2 if self.sources and worker is None else None)
                    continue
            if action == "stop":
                self._close_worker(worker)
                worker = None
                continue
            try:
                if action == "start":
                    worker = self.worker_factory(prefer_gpu)
                    report = worker.ready()
                    if report.get("phase") != "ready":
                        raise RuntimeError
                    with self.cond:
                        self.backend = {key: report[key] for key in ("model", "version", "backend", "device", "runtime")}
                        self.next_at = None
                        if self.backend["backend"] == "gpu" and not self.gpu_allowed:
                            self.change_device = True
                else:
                    response = worker.analyze(frame)
                    if response.get("phase") not in {"sample", "frame_unusable"}:
                        raise RuntimeError
                    with self.cond:
                        evidence = self._accept(frame, response)
                    if self.event_sink and evidence:
                        try:
                            self.event_sink.offer(frame, *evidence)
                        except Exception:
                            pass  # Event history must not interrupt analysis or recording.
            except Exception as exc:
                self._close_worker(worker)
                worker = None
                with self.cond:
                    gpu_fault = (action == "start" and prefer_gpu) or self.backend.get("backend") == "gpu"
                    reason = "analysis_timeout" if isinstance(exc, TimeoutError) else "resource_limit" if isinstance(exc, MemoryError) else "engine_failed"
                    if action == "start" and prefer_gpu and not self.gpu_rejected and not self.failures:
                        # Lack of usable GPU on this installation is a normal
                        # CPU fallback. Do not open an incident or start emergency recording.
                        self.gpu_rejected = True
                        continue
                    self._failure(reason)
                    if gpu_fault and not self.gpu_rejected:
                        self.gpu_rejected = True
                        self.next_at = time.time()
                        self.next_clock = time.monotonic()
        self._close_worker(worker)

    @staticmethod
    def _close_worker(worker):
        if worker is not None:
            try:
                worker.close()
            except (OSError, subprocess.SubprocessError):
                pass

    def stop(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()
