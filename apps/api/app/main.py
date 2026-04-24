from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.db.session import SessionLocal
from app.routers.auth import router as auth_router
from app.routers.cameras import router as cameras_router
from app.routers.chronology import router as chronology_router
from app.routers.live import router as live_router
from app.routers.recordings import router as recordings_router
from app.routers.storage import router as storage_router
from app.services.bootstrap import init_db, ensure_admin
from app.services.live_hls import start_cleanup_worker, stop_all_streams, stop_cleanup_worker

app = FastAPI(
    title="TNAS VMS API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
        ensure_admin(db)
    finally:
        db.close()

    start_cleanup_worker()


@app.on_event("shutdown")
def shutdown():
    stop_cleanup_worker()
    stop_all_streams()


@app.get("/")
def root():
    return {
        "name": "TNAS VMS API",
        "status": "ok",
        "version": "1.0.0",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "app_env": settings.app_env,
        "storage_root": settings.storage_root,
        "storage_exists": Path(settings.storage_root).exists(),
    }


@app.get("/system/info")
def system_info():
    return {
        "app_env": settings.app_env,
        "default_live_stream": settings.default_live_stream,
        "default_record_stream": settings.default_record_stream,
        "storage_root": settings.storage_root,
        "storage_previews": settings.storage_previews,
        "storage_exports": settings.storage_exports,
    }


app.include_router(auth_router)
app.include_router(cameras_router)
app.include_router(recordings_router)
app.include_router(storage_router)
app.include_router(live_router)
app.include_router(chronology_router)
