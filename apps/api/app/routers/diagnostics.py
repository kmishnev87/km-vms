from datetime import datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.models.user import User
from app.routers.deps import require_permission

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


def safe_log_files() -> list[Path]:
    candidates = [
        Path("/var/log/km-vms"),
        Path("logs"),
    ]
    files: list[Path] = []
    for root in candidates:
        if not root.exists() or not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".log", ".txt"}:
                files.append(path)
    return files[:50]


@router.post("/archive")
def create_diagnostic_archive(
    current_user: User = Depends(require_permission("manage_settings")),
):
    created_at = datetime.utcnow()
    filename = f"km-vms-diagnostics-{created_at.strftime('%Y%m%d-%H%M%S')}.zip"
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "metadata.txt",
            "\n".join(
                [
                    "KM VMS diagnostics",
                    f"created_utc={created_at.isoformat()}Z",
                    f"user={current_user.username}",
                    f"app_env={settings.app_env}",
                    "version=1.0.0",
                ]
            )
            + "\n",
        )
        for path in safe_log_files():
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if len(data) > 2 * 1024 * 1024:
                data = data[-2 * 1024 * 1024 :]
            archive.writestr(f"logs/{path.name}", data)

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
