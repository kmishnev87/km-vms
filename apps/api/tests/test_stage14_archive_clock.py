from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import archive_exports
from app.services.system_settings import get_system_settings
from app.services.timezone_contract import TimezoneContext
from test_archive_export_stage11 import api_db, add_user, add_camera, add_segment


@pytest.mark.parametrize("zone", ["UTC", "Asia/Yekaterinburg", "America/New_York", "Europe/Berlin"])
def test_aware_and_local_export_range_refer_to_same_calendar(zone):
    ctx = TimezoneContext(zone, ZoneInfo(zone))
    local = datetime(2026, 3, 8, 12, 30)
    instant = local.replace(tzinfo=ctx.zone).astimezone(timezone.utc)
    assert archive_exports.normalize_export_datetime(instant, "start", ctx) == local
    assert archive_exports.normalize_export_datetime(local, "start", ctx) == local


def test_equivalent_utc_and_product_local_requests_select_same_file_without_rewriting_dates(api_db):
    db, _ = api_db
    actor, camera = add_user(db), add_camera(db)
    system = get_system_settings(db)
    system.timezone = "Asia/Yekaterinburg"
    db.commit()
    local = datetime(2026, 1, 1, 15, 0)
    segment = add_segment(db, camera, started=local)
    common = dict(actor=actor, camera_id=camera.id)
    naive = archive_exports.create_archive_export_job(db, start_ts=local, end_ts=local + timedelta(seconds=30), **common)
    utc = archive_exports.create_archive_export_job(db, start_ts=local.replace(tzinfo=ZoneInfo(system.timezone)).astimezone(timezone.utc),
        end_ts=(local + timedelta(seconds=30)).replace(tzinfo=ZoneInfo(system.timezone)).astimezone(timezone.utc), **common)
    assert naive.source_segment_ids == utc.source_segment_ids == [segment.id]
    assert naive.start_ts == utc.start_ts == local
    db.refresh(segment)
    assert segment.started_at == local
