from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import uuid
from sqlalchemy import text

from .contracts import METADATA_SOURCE_RECORDER, OWNERSHIP_KM_VMS, RecorderState, UNSET


@dataclass(frozen=True)
class RecordingJobStore:
    """One transaction boundary for job authority; publish ids only after commit."""

    engine: Any
    instance_id: str
    selected_source_stream: Callable
    input_fingerprint: Callable
    redact_text: Callable
    truncate_error: Callable
    log_event: Callable

    def create_recording_job(self, row, job: Any) -> str:
        if job.db_job_id:
            return job.db_job_id

        job_id = job.pending_db_job_id or str(uuid.uuid4())
        job.pending_db_job_id = job_id
        source_stream = self.selected_source_stream(row)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO recording_jobs (
                        id,
                        camera_id,
                        camera_name_snapshot,
                        camera_folder_snapshot,
                        state,
                        source_stream,
                        input_fingerprint,
                        recorder_instance_id,
                        started_at,
                        created_by,
                        ownership,
                        source,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        :id,
                        :camera_id,
                        :camera_name,
                        :camera_folder,
                        :state,
                        :source_stream,
                        :input_fingerprint,
                        :recorder_instance_id,
                        timezone('UTC', NOW()),
                        :created_by,
                        :ownership,
                        :source,
                        NOW(),
                        NOW()
                    )
                    ON CONFLICT (id) DO NOTHING
                    """
                ),
                {
                    "id": job_id,
                    "camera_id": job.camera_id,
                    "camera_name": self.redact_text(job.camera_name),
                    "camera_folder": job.folder_name,
                    "state": RecorderState.STARTING,
                    "source_stream": source_stream,
                    "input_fingerprint": self.input_fingerprint(row),
                    "recorder_instance_id": self.instance_id,
                    "created_by": OWNERSHIP_KM_VMS,
                    "ownership": OWNERSHIP_KM_VMS,
                    "source": METADATA_SOURCE_RECORDER,
                },
            )
        # Publish authority only after commit; reuse the pending id if its reply was
        # lost so that retry cannot create a second owner or launch an orphan writer.
        job.db_job_id = job_id
        job.pending_db_job_id = None
        job.source_stream = source_stream
        self.log_event("info", "recording_job_created", camera_id=job.camera_id, camera_name=job.camera_name, db_job_id=job_id)
        return job_id

    def update_recording_job(self,
        job: Any,
        *,
        state: str | None = None,
        stop_reason: Any = UNSET,
        stopped: bool = False,
        last_error: Any = UNSET,
        last_error_type: Any = UNSET,
        ffmpeg_pid: Any = UNSET,
        last_exit_code: Any = UNSET,
    ) -> None:
        job_id = job.db_job_id or job.pending_db_job_id
        if not job_id:
            return
        sanitized_last_error = (
            self.truncate_error(self.redact_text(last_error))
            if last_error is not UNSET and last_error
            else None
        )

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE recording_jobs
                    SET state = COALESCE(:state, state),
                        stopped_at = CASE WHEN :stopped THEN timezone('UTC', NOW()) ELSE stopped_at END,
                        stop_reason = CASE WHEN :stop_reason_set THEN :stop_reason ELSE stop_reason END,
                        last_error = CASE WHEN :last_error_set THEN :last_error ELSE last_error END,
                        last_error_type = CASE WHEN :last_error_type_set THEN :last_error_type ELSE last_error_type END,
                        ffmpeg_pid = CASE WHEN :ffmpeg_pid_set THEN :ffmpeg_pid ELSE ffmpeg_pid END,
                        last_exit_code = CASE WHEN :last_exit_code_set THEN :last_exit_code ELSE last_exit_code END,
                        updated_at = timezone('UTC', NOW())
                    WHERE id = :id
                    """
                ),
                {
                    "id": job_id,
                    "state": state,
                    "stopped": stopped,
                    "stop_reason_set": stop_reason is not UNSET,
                    "stop_reason": None if stop_reason is UNSET else stop_reason,
                    "last_error_set": last_error is not UNSET,
                    "last_error": sanitized_last_error,
                    "last_error_type_set": last_error_type is not UNSET,
                    "last_error_type": None if last_error_type is UNSET else last_error_type,
                    "ffmpeg_pid_set": ffmpeg_pid is not UNSET,
                    "ffmpeg_pid": None if ffmpeg_pid is UNSET else ffmpeg_pid,
                    "last_exit_code_set": last_exit_code is not UNSET,
                    "last_exit_code": None if last_exit_code is UNSET else last_exit_code,
                },
            )
        self.log_event("info", "recording_job_updated", camera_id=job.camera_id, camera_name=job.camera_name, db_job_id=job.db_job_id, state=state)

    def clear_recording_job_error(self, job: Any) -> None:
        if not job.db_job_id:
            return
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE recording_jobs
                    SET last_error = NULL,
                        last_error_type = NULL,
                        last_exit_code = NULL,
                        updated_at = timezone('UTC', NOW())
                    WHERE id = :id
                    """
                ),
                {"id": job.db_job_id},
            )
