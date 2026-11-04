"""Bounded FFmpeg diagnostics, rotated by the existing stream maintenance loop.

copy/truncate keeps FFmpeg's open O_APPEND descriptor valid. Logs are best-effort
diagnostics: a concurrent write at the truncation boundary may be omitted, unlike
media files, which are never touched here. No new polling thread or process.
"""
import os
from pathlib import Path
import threading

LOG_PART_BYTES = 1024 * 1024
LOG_TAIL_BYTES = 64 * 1024


class BoundedProcessLog:
    def __init__(self, limit=LOG_PART_BYTES, retained=LOG_TAIL_BYTES):
        self.limit = int(limit)
        self.retained = min(int(retained), self.limit)
        self.lock = threading.Lock()

    def read_tail(self, path: Path, max_bytes: int) -> tuple[str, bool]:
        with self.lock:
            rotated = False
            with path.open('rb') as reader:
                size = os.fstat(reader.fileno()).st_size
                if size > self.limit:
                    reader.seek(max(0, size - self.limit))
                    history = reader.read(self.limit)
                    reader.seek(max(0, size - self.retained))
                    tail = reader.read(self.retained)
                    path.with_name(path.name + '.1').write_bytes(history)
                    with path.open('r+b') as writer:
                        writer.write(tail)
                        writer.truncate()
                    rotated = True
                reader.seek(0, os.SEEK_END)
                reader.seek(max(0, reader.tell() - max_bytes))
                return reader.read(max_bytes).decode('utf-8', errors='replace'), rotated
