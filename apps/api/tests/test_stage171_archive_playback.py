"""Subsecond archive boundaries and streaming-only DB lifetime, synthetic data."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app.db.session import get_db
from app.routers import chronology, recordings
from app.utils.video_stream import stream_video
from test_records_playback_stage10 import db, add_camera, add_segment, add_user


def test_playback_exposes_exact_duration_and_fractional_offset(db):
    camera = add_camera(db)
    segment = add_segment(db, camera, name="stage171-synthetic.mkv", write_file=True)
    target = segment.started_at + timedelta(seconds=2, milliseconds=345)
    response = chronology.chronology_playback(camera.id, target.isoformat() + "Z", db, add_user(db))
    assert response["has_video"] is True
    assert response["offset_sec"] == pytest.approx(2.345)
    assert response["duration_sec"] == pytest.approx((segment.ended_at - segment.started_at).total_seconds())


@pytest.mark.parametrize("router,path", [(recordings.router, "/recordings/stream"), (chronology.router, "/chronology/file")])
def test_real_media_routes_release_db_before_streaming(router, path):
    route = next(value for value in router.routes if value.path == path)
    dependency = next(value for value in route.dependant.dependencies if value.call is get_db)
    assert dependency.scope == "function"


def test_range_stream_does_not_need_the_db_after_admission(tmp_path):
    source = tmp_path / "synthetic-video.mkv"
    source.write_bytes(b"0123456789")
    engine = create_engine("sqlite://", poolclass=QueuePool, pool_size=1, max_overflow=0,
                           connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=engine)
    closed = []

    def session_dependency():
        session = factory()
        session.execute(text("select 1"))
        try:
            yield session
        finally:
            session.close()
            closed.append(True)

    app = FastAPI()

    @app.get("/synthetic-stream")
    def route(session=Depends(session_dependency, scope="function")):
        # Only an admitted Path remains in the real product streaming iterator.
        return stream_video(SimpleNamespace(headers={"range": "bytes=2-5"}), source)

    async def run():
        messages = []

        async def send(message):
            if message["type"] == "http.response.body":
                assert closed
                assert engine.pool.checkedout() == 0
            messages.append(message)

        async def receive():
            await asyncio.Future()

        await app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "scheme": "http", "method": "GET", "path": "/synthetic-stream",
            "raw_path": b"/synthetic-stream", "query_string": b"", "headers": [],
            "client": ("127.0.0.1", 1), "server": ("synthetic", 80), "root_path": ""}, receive, send)
        start = next(message for message in messages if message["type"] == "http.response.start")
        assert start["status"] == 206
        assert b"".join(message.get("body", b"") for message in messages) == b"2345"

    try:
        asyncio.run(run())
    finally:
        engine.dispose()
