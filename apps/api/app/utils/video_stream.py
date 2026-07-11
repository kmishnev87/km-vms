import os
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

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
) -> StreamingResponse:
    target = Path(file_path)
    file_size = os.path.getsize(target)
    range_header = request.headers.get("range") if request is not None else None

    start = 0
    end = file_size - 1
    if range_header:
        start, end = _single_byte_range(range_header, file_size)

    def iterfile():
        with target.open("rb") as source:
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
    return StreamingResponse(
        iterfile(),
        status_code=status_code,
        headers=headers,
        media_type=media_type or "application/octet-stream",
    )
