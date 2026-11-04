from datetime import datetime, timedelta
from app.models.recording import RecordingJob
from app.models.archive_integrity import ArchiveIntegrityScan
from test_stage13_5_4_10_3_archive_integrity import (
    stage4103, add_camera, add_segment, run_scan, active_findings, integrity,
)


def test_active_job_does_not_hide_its_finalized_missing_or_corrupt_files(stage4103, monkeypatch):
    ctx=stage4103
    camera=add_camera(ctx)
    job=RecordingJob(id="stage14-active-job", camera_id=camera.id, state="recording", started_at=datetime.utcnow()-timedelta(hours=3))
    ctx.db.add(job);ctx.db.commit()
    for name,exists,status,age in [
        ("good.mkv",True,"finalized",45),("missing.mkv",False,"finalized",45),
        ("bad.mkv",True,"finalized",45),("writing.mkv",True,"writing",45),
        ("recent.mkv",True,"finalized",0),
    ]:
        segment,_=add_segment(ctx,camera,name=name,create_file=exists,status=status,age_minutes=age)
        segment.job_id=job.id
        segment.updated_at=datetime.utcnow()-timedelta(minutes=age)
        ctx.db.commit()
    monkeypatch.setattr(integrity,"_safe_probe",lambda path: (False,"bad") if path.name=="bad.mkv" else (True,"ok"))
    result=run_scan(ctx,key="stage14-active-history")
    findings=active_findings(ctx,result["scan_id"])
    assert {finding.category for finding in findings} == {"missing_file","corrupted_file"}
    scan=ctx.db.get(ArchiveIntegrityScan,result["scan_id"])
    assert scan.skipped_count == 2
    assert scan.checked_count >= 3
