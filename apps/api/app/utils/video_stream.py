import os
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

CHUNK_SIZE = 1024 * 1024


def _single_byte_range(range_header: str, file_size: int) -> tuple[int, int]:
    try:
        units, range_spec = range_header.split("=", 1)
        if units.strip().lower() != "bytes" or "," in range_spec:
            raise ValueError("Only one bytes range is supported")
        start_value, end_value = (part.strip() for part in range_spec.split("-", 1))
        if not start_value:
            suffix_length = int(end_value)
            if suffix_length <= 0:
                raise ValueError("Invalid suffix range")
            start = max(0, file_size - suffix_length)
            end = file_size - 1
        else:
            start = int(start_value)
            end = int(end_value) if end_value else file_size - 1
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid Range header") from exc

    if start < 0 or start >= file_size or end < start:
        raise HTTPException(
            status_code=416,
            detail="Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    return start, min(end, file_size - 1)


def stream_video(
    request: Request | None,
    file_path: str | Path,
    media_type: str = "application/octet-stream",
    *,
    filename: str | None = None,
) -> StreamingResponse:
    target = Path(file_path)
    # Open before sending headers. A verified move may unlink the source after
    # the metadata switch, but this request keeps its original inode open.
    source = target.open("rb")
    try:
        file_size = os.fstat(source.fileno()).st_size
        range_header = request.headers.get("range") if request is not None else None

        start = 0
        end = file_size - 1
        if range_header:
            start, end = _single_byte_range(range_header, file_size)
    except BaseException:
        source.close()
        raise

    def iterfile():
        with source:
            source.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk_size = min(CHUNK_SIZE, remaining)
                data = source.read(chunk_size)
                if not data:
                    break
                yield data
                remaining -= len(data)

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    }
    status_code = 200
    if range_header:
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    if filename is not None:
        escaped = quote(filename, safe="")
        headers["Content-Disposition"] = (
            f"attachment; filename*=utf-8''{escaped}" if escaped != filename else f'attachment; filename="{filename}"'
        )
    return StreamingResponse(
        iterfile(),
        status_code=status_code,
        headers=headers,
        media_type=media_type or "application/octet-stream",
        background=BackgroundTask(source.close),
    )


def stream_archive_file(db, request, segment, *, resolve_path, media_type, get_filename=None):
    for attempt in range(2):
        previous_root = segment.archive_root_id
        try:
            path = resolve_path(segment)
            return stream_video(request, path, media_type(segment, path) or "application/octet-stream",
                                filename=get_filename(segment, path) if get_filename else None)
        except (HTTPException, FileNotFoundError) as exc:
            if isinstance(exc, HTTPException) and exc.status_code not in (404, 409):
                raise
            if not attempt:
                db.refresh(segment)
                if segment.archive_root_id != previous_root and segment.status == "finalized" and segment.deleted_at is None:
                    continue
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(404, "Recording file not found") from exc
