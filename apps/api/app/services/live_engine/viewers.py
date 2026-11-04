"""Viewer leases. All access is serialized by StreamManager.lock."""
from dataclasses import dataclass


def stream_key(camera_id, stream):
    return int(camera_id), str(stream or "main").lower()


@dataclass
class ViewerSession:
    id: str
    camera_id: int
    stream: str
    created_at: float
    last_seen: float


class ViewerRegistry:
    def __init__(self):
        self.sessions = {}

    def add(self, viewer):
        self.sessions[viewer.id] = viewer

    def pop(self, viewer_id, default=None):
        return self.sessions.pop(viewer_id, default)

    def get(self, viewer_id):
        return self.sessions.get(viewer_id)

    def clear(self):
        self.sessions.clear()

    def count(self, camera_id, stream):
        key = stream_key(camera_id, stream)
        return sum(1 for viewer in self.sessions.values()
                   if stream_key(viewer.camera_id, viewer.stream) == key)

    def details(self, camera_id, stream, now):
        key = stream_key(camera_id, stream)
        return [
            {"id": viewer.id, "created_at": viewer.created_at,
             "last_heartbeat": viewer.last_seen,
             "age_seconds": round(now - viewer.created_at, 2),
             "idle_seconds": round(now - viewer.last_seen, 2)}
            for viewer in self.sessions.values()
            if stream_key(viewer.camera_id, viewer.stream) == key
        ]

    def expire(self, now, ttl):
        expired = []
        for viewer_id, viewer in list(self.sessions.items()):
            if now - viewer.last_seen > ttl:
                expired.append(viewer)
                self.sessions.pop(viewer_id, None)
        return expired

    def remove_stream(self, camera_id, stream):
        key = stream_key(camera_id, stream)
        for viewer_id, viewer in list(self.sessions.items()):
            if stream_key(viewer.camera_id, viewer.stream) == key:
                self.sessions.pop(viewer_id, None)
