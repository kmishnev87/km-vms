from datetime import datetime, timezone

from app.services.camera_event_frames import forward_segment


def test_hls_snapshot_candidates_never_take_an_earlier_frame(tmp_path):
    playlist = tmp_path / "index.m3u8"
    for name in ("before", "after", "later", "far"):
        (tmp_path / (name + ".ts")).touch()
    playlist.write_text("""#EXTM3U
#EXT-X-PROGRAM-DATE-TIME:2026-11-19T15:04:58.010Z
before.ts
#EXT-X-PROGRAM-DATE-TIME:2026-11-19T15:05:04.010Z
later.ts
#EXT-X-PROGRAM-DATE-TIME:2026-11-19T20:05:00.010+05:00
after.ts
#EXT-X-PROGRAM-DATE-TIME:2026-11-19T15:05:06.010Z
far.ts
""", encoding="utf-8")
    stamp = datetime(2026, 11, 19, 15, 5, tzinfo=timezone.utc)
    assert forward_segment(playlist, stamp) == tmp_path / "after.ts"
    (tmp_path / "after.ts").unlink()
    (tmp_path / "later.ts").unlink()
    assert forward_segment(playlist, stamp) is None
