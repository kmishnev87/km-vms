from fastapi import Request, HTTPException
from fastapi.responses import StreamingResponse
import os

CHUNK_SIZE = 1024 * 1024


def stream_video(request: Request, file_path: str):
    file_size = os.path.getsize(file_path)
    range_header = request.headers.get("range")

    start = 0
    end = file_size - 1

    if range_header:
        try:
            units, range_spec = range_header.split("=", 1)
            if units.strip().lower() != "bytes":
                raise ValueError("Only bytes range supported")
            start_s, end_s = range_spec.split("-", 1)
            if start_s.strip():
                start = int(start_s)
            if end_s.strip():
                end = int(end_s)
        except Exception:
            raise HTTPException(status_code=400, detail="Некорректный Range header")

    if start >= file_size or end >= file_size or start > end:
        raise HTTPException(status_code=416, detail="Range Not Satisfiable")

    def iterfile():
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk_size = min(CHUNK_SIZE, remaining)
                data = f.read(chunk_size)
                if not data:
                    break
                yield data
                remaining -= len(data)

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Type": "video/mp4",
    }

    status_code = 206 if range_header else 200
    return StreamingResponse(iterfile(), status_code=status_code, headers=headers)
