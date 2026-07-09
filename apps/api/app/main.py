import logging
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
from app.services.archive_root_activation import finalize_pending_archive_root_activation, start_archive_root_activation_closeout_worker
from app.services.bootstrap import init_db, ensure_admin, ensure_owner_migration, ensure_system_settings
from app.services.recording_storage import ensure_archive_roots
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
        ensure_owner_migration(db)
        ensure_admin(db)
        finalize_pending_archive_root_activation(db)
        run_startup_due_check(db)
    finally:
        db.close()

    refresh_hardware_capabilities()
    start_archive_root_activation_closeout_worker()
    start_automatic_retention_worker()
    start_cleanup_worker()


@app.on_event("shutdown")
def shutdown():
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
