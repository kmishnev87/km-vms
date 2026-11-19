"""Bounded compressed-packet history. No archive I/O while waiting for an event."""
from __future__ import annotations

import math
import re
import subprocess
import threading
import time
import traceback
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PACKET_OVERHEAD = 512  # Account for Python/AVPacket objects as well as payload.
MAX_CAMERA_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 192 * 1024 * 1024
SWITCH_GUARD_SECONDS = 1.0
MAX_MEDIA_SILENCE = 20.0
RETRY_SECONDS = (5, 15, 30, 60, 120)
PART_STAMP = re.compile(r"-ev4-(main|sub)-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{6}Z)\.(?:mkv|mp4)$")


@dataclass(frozen=True, slots=True)
class Sample:
    number: int
    packet: object
    at: float
    span: float
    video: bool
    key: bool

    @property
    def size(self):
        return self.packet.size + PACKET_OVERHEAD


class Gop:
    def __init__(self, feed, sample):
        self.feed = feed
        self.samples = [sample]
        self.size = sample.size


class PacketBudget:
    """One shared ledger; time/byte pressure removes whole, oldest GOPs."""

    def __init__(self, per_camera=MAX_CAMERA_BYTES, total=MAX_TOTAL_BYTES):
        self.lock = threading.RLock()
        self.per_camera = per_camera
        self.limit = total
        self.total = 0
        self.cameras = {}
        self.groups = OrderedDict()

    def _remove(self, group, pressure=False):
        del self.groups[group]
        feed = group.feed
        assert feed.groups.popleft() is group
        if group is feed.current:
            feed.current = None  # Never continue a GOP missing its keyframe.
        self.total -= group.size
        remaining = self.cameras[feed.camera_id] - group.size
        if remaining:
            self.cameras[feed.camera_id] = remaining
        else:
            del self.cameras[feed.camera_id]
        if pressure:
            feed.limited_until = time.monotonic() + feed.keep

    def clear(self, feed):
        with self.lock:
            while feed.groups:
                self._remove(feed.groups[0])

    def append(self, feed, sample):
        with self.lock:
            if sample.video:
                # Retain complete GOPs; an older GOP that straddles the cutoff
                # may still be useful to an already active writer.
                while (len(feed.groups) > 1
                       and feed.groups[1].samples[0].at <= sample.at - feed.keep):
                    self._remove(feed.groups[0])
                if feed.groups and sample.at - feed.groups[0].samples[0].at > feed.keep + 8:
                    self._remove(feed.groups[0], pressure=True)
            elif feed.current is None:
                return
            if sample.size > min(self.limit, self.per_camera):
                if feed.current and not sample.key:
                    while feed.groups:
                        self._remove(feed.groups[0], pressure=True)
                feed.current = None
                feed.limited_until = time.monotonic() + feed.keep
                return
            while self.cameras.get(feed.camera_id, 0) + sample.size > self.per_camera:
                old = next(group for group in self.groups if group.feed.camera_id == feed.camera_id)
                self._remove(old, pressure=True)
            while self.total + sample.size > self.limit:
                self._remove(next(iter(self.groups)), pressure=True)
            if sample.key:
                group = feed.current = Gop(feed, sample)
                feed.groups.append(group)
                self.groups[group] = None
            elif feed.current is not None:
                feed.current.samples.append(sample)
                feed.current.size += sample.size
            else:
                return
            self.total += sample.size
            self.cameras[feed.camera_id] = self.cameras.get(feed.camera_id, 0) + sample.size


def capture_command(url, transport):
    return ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-max_alloc", "16777216",
            "-rtsp_transport", transport, "-timeout", "10000000", "-analyzeduration", "800000",
            "-probesize", "262144", "-i", url, "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy", "-c:a", "aac", "-f", "nut", "-write_index", "0", "-flush_packets", "1", "pipe:1"]


class Feed:
    """One subprocess receives a logical RTSP profile; its pipe stays private."""

    def __init__(self, pool, camera_id, url, transport, role, seconds, command=capture_command, *, tap=None):
        self.pool, self.camera_id, self.url, self.transport, self.role = pool, camera_id, url, transport, role
        self.command = command
        self.tap = tap
        self.keep = max(8, seconds + 6)  # Also allow the poll-to-writer handoff and live consumption.
        self.groups = deque()
        self.current = None
        self.streams = {}
        self.serial = 0
        self.sequence = 0
        self.latest_at = None
        self.limited_until = 0.0
        self.last_progress = self.started = self.retry_at = 0.0
        self.retry = 0
        self.proc = self.thread = None

    def ensure(self):
        now = time.monotonic()
        if self.proc is not None:
            if (self.proc.poll() is None and self.thread.is_alive()
                    and now - max(self.started, self.last_progress) < MAX_MEDIA_SILENCE):
                return
            self.close()
            self.retry_at = now + RETRY_SECONDS[min(self.retry, len(RETRY_SECONDS) - 1)]
            self.retry += 1
        if not self.url or now < self.retry_at:
            return
        self.serial += 1
        self.started, self.last_progress = now, 0.0
        try:
            self.proc = subprocess.Popen(self.command(self.url, self.transport), stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, bufsize=0)
        except OSError:
            self.retry_at = now + RETRY_SECONDS[min(self.retry, len(RETRY_SECONDS) - 1)]
            self.retry += 1
            return
        self.thread = threading.Thread(target=self._read, args=(self.proc, self.serial), daemon=True)
        self.thread.start()

    def _read(self, proc, serial):
        try:
            import av

            with av.open(proc.stdout, format="nut", buffer_size=4096,
                         options={"probesize": "32768", "analyzeduration": "100000"}) as source:
                streams = {s.index: s for s in source.streams if s.type in {"video", "audio"}}
                media_origin = wall_origin = None
                prior_video_dts = None
                for packet in source.demux(tuple(streams.values())):
                    if packet.size <= 0 or packet.pts is None or not packet.time_base:
                        continue
                    video = packet.stream.type == "video"
                    key = video and packet.is_keyframe
                    pts = float(packet.pts * packet.time_base)
                    dts = float((packet.dts if packet.dts is not None else packet.pts) * packet.time_base)
                    now = time.monotonic()
                    if video and prior_video_dts is not None and (dts < prior_video_dts or dts - prior_video_dts > 4):
                        # Timestamp reset or a hole: finish the old part and re-anchor the next GOP.
                        with self.pool.lock:
                            if self.serial != serial:
                                return
                            self.pool.clear(self)
                            self.serial += 1
                            serial = self.serial
                            self.latest_at = None
                        media_origin = wall_origin = None
                    if video:
                        prior_video_dts = dts
                    if media_origin is None:
                        if not key:
                            continue
                        media_origin = pts
                        wall_origin = time.time()
                    at = wall_origin + pts - media_origin
                    span = float((packet.duration or 0) * packet.time_base)
                    if video and not span:
                        rate = streams[packet.stream.index].average_rate
                        span = float(1 / rate) if rate and 0 < rate <= 240 else 0.04
                    with self.pool.lock:
                        if self.serial != serial:
                            return
                        if video:
                            self.last_progress = now
                            if now - self.started > 30:
                                self.retry = 0
                            self.latest_at = max(self.latest_at or at, at + span)
                        self.streams = streams
                        self.sequence += 1
                        self.pool.append(self, Sample(self.sequence, packet, at, span, video, key))
                    if key and self.tap is not None:
                        try:
                            # Outside the archive packet-ledger lock. Analysis
                            # must never break capture or exert backpressure.
                            self.tap(self, serial, packet)
                        except Exception:
                            pass
        except Exception:
            # RTSP input details and credentials must not escape this thread.
            return

    def close(self):
        with self.pool.lock:
            self.serial += 1
            self.pool.clear(self)
            self.latest_at = None
            self.streams = {}
            self.limited_until = 0.0
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
            self.thread.join(timeout=1)
            if not self.thread.is_alive():
                self.proc.stdout.close()
            self.proc = self.thread = None

    def next_key(self, at, after=0):
        with self.pool.lock:
            for group in self.groups:
                sample = group.samples[0]
                if sample.number > after and sample.at >= at:
                    return self.serial, sample, dict(self.streams)
        return None

    def next_samples(self, serial, cursor, limit=64):
        with self.pool.lock:
            if serial != self.serial:
                return ()
            result = []
            for group in self.groups:
                if group.samples[-1].number <= cursor:
                    continue
                index = max(0, cursor + 1 - group.samples[0].number)
                result.extend(group.samples[index:index + limit - len(result)])
                if len(result) == limit:
                    break
            return result

    def status(self, seconds):
        now = time.monotonic()
        with self.pool.lock:
            if not self.url:
                phase = "not_configured"
            elif not self.proc or self.proc.poll() is not None or not self.thread.is_alive() or self.retry_at > now:
                phase = "unavailable"
            elif not self.groups or now - self.last_progress > 4:
                phase = "waiting_keyframe"
            else:
                phase = "ready"
            available = 0
            if phase == "ready" and self.latest_at is not None:
                start = next((group.samples[0].at for group in self.groups
                              if group.samples[0].at >= self.latest_at - seconds), self.latest_at)
                available = max(0, math.floor(self.latest_at - start))
            return phase, available, now < self.limited_until


def part_name(pattern, role, start):
    prefix = Path(pattern).name.split("%", 1)[0]
    stamp = datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    return Path(pattern).with_name(f"{prefix}ev4-{role}-{stamp}{Path(pattern).suffix}")


class Part:
    def __init__(self, pattern, role, first, templates):
        import av

        self.start = first.at
        self.end = first.at
        source = first.packet
        self.origin = source.pts * source.time_base
        self.role = role
        target = part_name(pattern, role, self.start)
        # Fragmented MP4 and short MKV clusters keep persisted packets recoverable
        # using the existing interrupted-segment path after power loss.
        options = ({"movflags": "frag_keyframe+empty_moov+default_base_moof+global_sidx", "flush_packets": "1"}
                   if target.suffix == ".mp4" else {"cluster_time_limit": "1000", "flush_packets": "1"})
        options["max_interleave_delta"] = "500000"
        self.container = av.open(str(target), "w", options=options)
        try:
            self.streams = {index: self.container.add_stream_from_template(stream) for index, stream in templates.items()}
        except Exception:
            self.container.close()
            raise
        self.av = av

    def write(self, sample):
        raw = sample.packet
        if not sample.video and sample.at < self.start:
            return
        packet = self.av.Packet(bytes(raw))
        packet.stream = self.streams[raw.stream.index]
        packet.time_base = raw.time_base
        offset = round(self.origin / raw.time_base)
        packet.pts = raw.pts - offset
        packet.dts = (raw.dts if raw.dts is not None else raw.pts) - offset
        packet.duration = raw.duration or 0
        packet.is_keyframe = raw.is_keyframe
        self.container.mux(packet)
        if sample.video:
            self.end = max(self.end, sample.at + sample.span)

    def close(self):
        self.container.close()


class BufferedWriter:
    """Process-shaped adapter for the existing start/stop/recovery state machine.

    Archive I/O is confined to this episode thread. Capture and its RAM history
    are independent and remain available for the next episode.
    """

    stderr = None

    def __init__(self, inputs, pattern, segment_seconds, signal_at):
        self.inputs, self.pattern, self.segment_seconds = inputs, pattern, segment_seconds
        self.signal_at = signal_at
        self.pid = None  # No independent archive-writing FFmpeg process.
        self.exit_code = None
        self.failure = None
        self.stop = threading.Event()
        self.aborted = threading.Event()
        self.part = None
        self.feed = None
        self.serial = self.cursor = 0
        self.written_end = inputs.written_end
        self.stream_role = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.stop.set()

    def kill(self):
        self.aborted.set()
        self.stop.set()

    def wait(self, timeout=None):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise subprocess.TimeoutExpired("buffered archive writer", timeout)
        return self.exit_code

    def _close_part(self):
        if self.part:
            part, self.part = self.part, None
            part.close()

    def _bind(self, feed, key):
        self._close_part()
        self.feed, self.serial, self.cursor, self.templates = feed, key[0], key[1].number - 1, key[2]
        self.stream_role = feed.role
        self.inputs.select_analysis(feed)

    def _pump(self, before, max_batches=8):
        # Re-open only on a valid keyframe after an evicted/missing packet.
        for _ in range(max_batches):
            samples = self.feed.next_samples(self.serial, self.cursor)
            if not samples:
                return
            for sample in samples:
                if self.aborted.is_set() or sample.at >= before:
                    return
                if sample.number != self.cursor + 1:
                    self._close_part()
                self.cursor = sample.number
                if sample.key and (self.part is None or sample.at - self.part.start >= self.segment_seconds):
                    self._close_part()
                    self.part = Part(self.pattern, self.feed.role, sample, self.templates)
                if self.part is not None:
                    self.part.write(sample)
                    self.written_end = max(self.written_end, self.part.end)

    def _run(self):
        inp = self.inputs
        try:
            first_at = max(self.written_end - .001, self.signal_at - inp.seconds)
            while not self.stop.is_set():
                if not inp.warm:
                    inp.primary.ensure()
                if inp.second:
                    inp.second.ensure()
                if self.feed and self.feed.serial != self.serial:
                    self._close_part()
                    self.feed = None
                    self.stream_role = None
                    inp.select_analysis(inp.primary)
                if not self.feed:
                    # MAIN is allowed even if the optional prehistory input is absent.
                    # This isn't a silent quality substitution; missing Sub is reported.
                    primary_key = inp.primary.next_key(max(first_at, self.written_end - .001))
                    if primary_key:
                        self._bind(inp.primary, primary_key)
                    elif inp.second:
                        main_key = inp.second.next_key(max(self.signal_at, self.written_end - .001))
                        if main_key:
                            self._bind(inp.second, main_key)
                if self.feed:
                    pending = inp.second and self.feed is inp.primary
                    limit = time.time() - SWITCH_GUARD_SECONDS if pending else math.inf
                    main_key = (inp.second.next_key(max(self.written_end - .001, self.signal_at)) if pending else None)
                    if main_key and (inp.primary.latest_at is None or inp.primary.latest_at >= main_key[1].at - .08
                                     or time.monotonic() - inp.primary.last_progress > 2):
                        self._pump(main_key[1].at)
                        self._bind(inp.second, main_key)
                        limit = math.inf
                    self._pump(limit)
                self.stop.wait(.1)
            if self.feed and not self.aborted.is_set():
                self._pump(time.time())
            self._close_part()
            self.exit_code = 0
        except Exception as exc:
            # Code location is useful for diagnosing mux errors without
            # publishing source credentials or archive paths from the exception.
            leaf = traceback.extract_tb(exc.__traceback__)[-1]
            self.failure = f"buffered_writer_failed:{type(exc).__name__}:{leaf.name}:{leaf.lineno}"
            try:
                self._close_part()
            finally:
                self.exit_code = 1
        finally:
            inp.written_end = max(inp.written_end, self.written_end)


class BufferedCamera:
    def __init__(self, pool, camera_id, recording_url, recording_role, preview_url, preview_role, transport, profile, seconds,
                 command=capture_command, *, analysis=None, analysis_switch=None):
        self.seconds = seconds
        self.warm = profile != "economical"
        self.written_end = 0.0
        self.analysis = analysis
        self.analysis_switch = analysis_switch
        self.analysis_id = uuid.uuid4().hex[:12] if analysis else ""
        self.analysis_epoch = 0
        self.analysis_lock = threading.Lock() if analysis else None
        two_inputs = profile == "balanced" and recording_url != preview_url
        self.primary = Feed(pool, camera_id, preview_url if two_inputs else recording_url, transport,
                            preview_role if two_inputs else recording_role, seconds, command, tap=self._tap if analysis else None)
        self.second = Feed(pool, camera_id, recording_url, transport, recording_role, 0, command,
                           tap=self._tap if analysis else None) if two_inputs else None
        self.analysis_input = self.primary

    def _stream_token(self, serial):
        return f"{self.analysis_id}:{self.analysis_epoch}:{serial}"

    def select_analysis(self, feed):
        if self.analysis_lock is None:
            return
        with self.analysis_lock:
            if self.analysis_input is feed:
                return
            self.analysis_input = feed
            self.analysis_epoch += 1
            if self.analysis_switch is not None:
                try:
                    self.analysis_switch(self._stream_token(feed.serial))
                except Exception:
                    pass

    def _tap(self, feed, serial, packet):
        if self.analysis_lock.acquire(blocking=False):
            try:
                if feed is self.analysis_input and feed.serial == serial:
                    self.analysis(self._stream_token(serial), packet)
            finally:
                self.analysis_lock.release()

    def prime(self, writer=None):
        # For AI balanced, Sub is only the waiting/prehistory source and the
        # hands-off input while Main warms. Once real Main media is being
        # written, analysis follows it and the unneeded Sub socket is closed.
        if (self.analysis and self.second and writer and writer.feed is self.second and writer.part
                and writer.written_end > writer.part.start and time.monotonic() - self.second.last_progress < 4):
            if self.primary.proc:
                self.primary.close()
                self.primary.retry_at = 0
            return
        if self.warm:
            self.primary.ensure()

    def idle(self):
        self.select_analysis(self.primary)
        if not self.warm and self.primary.proc:
            self.primary.close()
            self.primary.retry = 0
            self.primary.retry_at = 0
        if self.second:
            if self.second.proc:
                self.second.close()
            self.second.retry = 0
            self.second.retry_at = 0

    def close(self):
        self.idle()
        self.primary.close()

    def status(self, writer=None):
        phase, available, limited = self.primary.status(self.seconds)
        if not self.warm and not writer:
            phase = "disabled_by_profile"
        main = self.second.status(0)[0] if self.second and writer else None
        return {"prebuffer_status": phase, "prebuffer_seconds_available": available,
                "prebuffer_capacity_limited": limited, "main_stream_status": main,
                "active_stream": writer.stream_role if writer else None}
