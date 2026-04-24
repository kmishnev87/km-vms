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

    admin_username: str = "admin"
    admin_password: str = "Admin_Change_Me_2026"
    admin_full_name: str = "Administrator"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )


settings = Settings()
