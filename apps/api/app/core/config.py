from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path


class Settings(BaseSettings):
    app_env: str = "production"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    database_url: str
    jwt_secret: str
    encryption_key: str

    storage_root: str = "/storage/archive"
    storage_previews: str = "/storage/previews"
    storage_exports: str = "/storage/exports"

    default_live_stream: str = "sub"
    default_record_stream: str = "main"
    live_transcode: bool = False
    live_video_codec: str = "auto"
    live_transcode_profile: str = "stable"
    live_unstable_source_target_fps: int = 20
    live_audio_mode: str = "none"
    live_hwaccel_mode: str = "auto"
    live_hwaccel_backend: str = "auto"
    live_hwaccel_device: str = "/dev/dri/renderD128"
    live_idle_ttl_seconds: int = 45
    live_viewer_ttl_seconds: int = 60
    live_cleanup_interval_seconds: int = 10
    live_start_timeout_seconds: int = 20
    live_max_concurrent_transcodes: int | None = 0
    automatic_retention_enabled: bool = True
    automatic_retention_interval_seconds: int = 3600
    automatic_retention_max_candidates: int = 25
    automatic_retention_max_bytes: int = 1 * 1024 * 1024 * 1024

    admin_username: str = "admin"
    admin_password: str = "Admin_Change_Me_2026"
    admin_full_name: str = "Administrator"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("live_max_concurrent_transcodes", mode="before")
    @classmethod
    def parse_live_transcode_limit(cls, value):
        if value is None:
            return 0
        if isinstance(value, str) and value.strip().lower() in {"", "0", "none", "null", "unlimited", "off"}:
            return 0
        return value

    def camera_preview_path(self, camera_id: int) -> Path:
        return Path(self.storage_previews) / "camera-previews" / f"{int(camera_id)}.jpg"

    def camera_preview_url(self, camera_id: int) -> str:
        return f"/previews/camera-previews/{int(camera_id)}.jpg"

    def camera_test_preview_path(self, token: str) -> Path:
        return Path(self.storage_previews) / "camera-tests" / f"{token}.jpg"

    def camera_test_preview_url(self, token: str) -> str:
        return f"/previews/camera-tests/{token}.jpg"


settings = Settings()
