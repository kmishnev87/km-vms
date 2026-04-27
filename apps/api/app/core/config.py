from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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


settings = Settings()
