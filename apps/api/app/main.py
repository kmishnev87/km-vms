import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.sanitization import redact_text
from app.core.version import APP_VERSION
from app.db.session import SessionLocal
from app.routers.audit import router as audit_router
from app.routers.auth import router as auth_router
from app.routers.archive_exports import router as archive_exports_router
from app.routers.cameras import router as cameras_router, viewer_router as viewer_cameras_router
from app.routers.chronology import router as chronology_router
from app.routers.hardware import router as hardware_router
from app.routers.live import router as live_router
from app.routers.maintenance import migrations_router, overview_router as maintenance_overview_router, restore_router, router as maintenance_router
from app.routers.previews import router as previews_router
from app.routers.recordings import router as recordings_router
from app.routers.deps import require_permission
from app.routers.settings import router as settings_router
from app.routers.storage import router as storage_router
from app.routers.users import router as users_router
from app.services.automatic_retention import start_automatic_retention_worker, stop_automatic_retention_worker
from app.services.archive_integrity import start_archive_integrity_worker, stop_archive_integrity_worker
from app.services.archive_migration import start_archive_migration_worker, stop_archive_migration_worker
from app.services.archive_root_activation import start_archive_root_activation_closeout_worker
from app.services.bootstrap import init_db, ensure_admin, ensure_owner_migration, ensure_system_settings
from app.services.recording_storage import ensure_archive_roots, migrate_archive_root_identities, write_archive_roots_runtime_files
from app.services.hardware import refresh_hardware_capabilities
from app.services.live_engine import start_cleanup_worker, stop_all_streams, stop_cleanup_worker
from app.services.update_check import run_startup_due_check


class AccessLogRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_text(arg) if isinstance(arg, str) else arg for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: redact_text(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        return True


logging.getLogger("uvicorn.access").addFilter(AccessLogRedactionFilter())

MAX_VALIDATION_ERRORS = 32
MAX_VALIDATION_LOC_DEPTH = 8
MAX_VALIDATION_LOC_INDEX = 1_000_000
MAX_VALIDATION_TYPE_LENGTH = 80
MAX_VALIDATION_MESSAGE_LENGTH = 300
MAX_VALIDATION_RESPONSE_BYTES = 16 * 1024
VALIDATION_SOURCES = {"body", "query", "path", "header", "cookie"}
VALIDATION_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.]{0,79}$")
VALIDATION_MESSAGES = {
    "extra_forbidden": "Extra input is not permitted.",
    "json_invalid": "Request body is not valid JSON.",
    "missing": "Required input is missing.",
    "string_too_long": "Input text exceeds the allowed length.",
    "string_too_short": "Input text is shorter than the allowed length.",
}
VALIDATION_FALLBACK_DETAIL = {
    "loc": ["body"],
    "type": "validation_error",
    "msg": "Request validation failed.",
}


def _normalized_validation_type(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_VALIDATION_TYPE_LENGTH:
        return "validation_error"
    return value if VALIDATION_TYPE_RE.fullmatch(value) else "validation_error"


def _safe_validation_loc(value: Any, *, error_type: str) -> list[str | int]:
    raw = value if isinstance(value, (list, tuple)) else ()
    source = raw[0] if raw else None
    safe: list[str | int] = [source if isinstance(source, str) and source in VALIDATION_SOURCES else "<source>"]
    remaining = raw[1:MAX_VALIDATION_LOC_DEPTH]
    for index, segment in enumerate(remaining, start=1):
        is_extra_leaf = error_type == "extra_forbidden" and index == len(raw) - 1
        if is_extra_leaf:
            safe.append("<extra>")
        elif isinstance(segment, int) and not isinstance(segment, bool):
            safe.append(segment if 0 <= segment <= MAX_VALIDATION_LOC_INDEX else "<index>")
        else:
            safe.append("<field>")
    if error_type == "extra_forbidden" and len(raw) > MAX_VALIDATION_LOC_DEPTH:
        safe[-1] = "<extra>"
    return safe


def _safe_validation_detail(value: Any) -> dict[str, Any]:
    error = value if isinstance(value, dict) else {}
    error_type = _normalized_validation_type(error.get("type"))
    message = VALIDATION_MESSAGES.get(error_type, VALIDATION_FALLBACK_DETAIL["msg"])
    return {
        "loc": _safe_validation_loc(error.get("loc"), error_type=error_type),
        "type": error_type,
        "msg": message[:MAX_VALIDATION_MESSAGE_LENGTH],
    }


def _validation_response_size(content: dict[str, Any]) -> int:
    return len(
        json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    )


def _safe_validation_response_content(errors: Any) -> dict[str, list[dict[str, Any]]]:
    raw_errors = errors if isinstance(errors, list) else []
    details = [_safe_validation_detail(item) for item in raw_errors[:MAX_VALIDATION_ERRORS]]
    if not details:
        details = [dict(VALIDATION_FALLBACK_DETAIL)]
    content = {"detail": details}
    while len(details) > 1 and _validation_response_size(content) > MAX_VALIDATION_RESPONSE_BYTES:
        details.pop()
    if _validation_response_size(content) > MAX_VALIDATION_RESPONSE_BYTES:
        content = {"detail": [dict(VALIDATION_FALLBACK_DETAIL)]}
    return content

app = FastAPI(
    title="TNAS VMS API",
    version=APP_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def safe_request_validation_error(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_safe_validation_response_content(exc.errors()),
    )


@app.on_event("startup")
def startup():
    Path(settings.storage_root).mkdir(parents=True, exist_ok=True)
    Path(settings.storage_previews).mkdir(parents=True, exist_ok=True)
    Path(settings.storage_exports).mkdir(parents=True, exist_ok=True)

    init_db()
    db = SessionLocal()
    try:
        ensure_system_settings(db)
        ensure_archive_roots(db)
        migrate_archive_root_identities(db)
        write_archive_roots_runtime_files(db)
        ensure_owner_migration(db)
        ensure_admin(db)
        run_startup_due_check(db)
    finally:
        db.close()

    refresh_hardware_capabilities()
    start_archive_root_activation_closeout_worker()
    start_automatic_retention_worker()
    start_archive_integrity_worker()
    start_archive_migration_worker()
    start_cleanup_worker()


@app.on_event("shutdown")
def shutdown():
    stop_archive_migration_worker()
    stop_archive_integrity_worker()
    stop_automatic_retention_worker()
    stop_cleanup_worker()
    stop_all_streams()


@app.get("/")
def root():
    return {
        "name": "TNAS VMS API",
        "status": "ok",
        "version": APP_VERSION,
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
    }


@app.get("/system/info")
def system_info(current_user=Depends(require_permission("manage_settings"))):
    return {
        "app_env": settings.app_env,
        "default_live_stream": settings.default_live_stream,
        "default_record_stream": settings.default_record_stream,
        "storage_root": settings.storage_root,
        "storage_previews": settings.storage_previews,
        "storage_exports": settings.storage_exports,
    }


app.include_router(auth_router)
app.include_router(audit_router)
app.include_router(archive_exports_router)
app.include_router(maintenance_router)
app.include_router(migrations_router)
app.include_router(restore_router)
app.include_router(maintenance_overview_router)
app.include_router(settings_router)
app.include_router(users_router)
app.include_router(cameras_router)
app.include_router(viewer_cameras_router)
app.include_router(previews_router)
app.include_router(recordings_router)
app.include_router(storage_router)
app.include_router(hardware_router)
app.include_router(live_router)
app.include_router(chronology_router)
