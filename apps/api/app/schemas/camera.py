from datetime import datetime
from pydantic import BaseModel, Field


class CameraBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    protocol: str
    host: str
    port: int

    username: str | None = None
    password: str | None = None

    rtsp_main_url: str | None = None
    rtsp_sub_url: str | None = None
    rtsp_host: str | None = None
    rtsp_port: int | None = None
    rtsp_transport: str | None = None

    onvif_path: str | None = None
    onvif_profile_token: str | None = None
    onvif_channel_id: str | None = None

    recording_mode: str = "always"
    default_live_stream: str = "sub"
    default_record_stream: str = "main"

    segment_minutes: int = Field(default=5, ge=1)
    retention_days: int = Field(default=30, ge=1)
    storage_quota_gb: int = Field(default=50, ge=1)
    preview_token: str | None = None


class CameraCreate(CameraBase):
    pass


class CameraUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    protocol: str | None = None
    host: str | None = None
    port: int | None = None

    username: str | None = None
    password: str | None = None

    rtsp_main_url: str | None = None
    rtsp_sub_url: str | None = None
    rtsp_host: str | None = None
    rtsp_port: int | None = None
    rtsp_transport: str | None = None

    onvif_path: str | None = None
    onvif_profile_token: str | None = None
    onvif_channel_id: str | None = None

    recording_mode: str | None = None
    default_live_stream: str | None = None
    default_record_stream: str | None = None

    segment_minutes: int | None = Field(default=None, ge=1)
    retention_days: int | None = Field(default=None, ge=1)
    storage_quota_gb: int | None = Field(default=None, ge=1)

    status: str | None = None
    last_error: str | None = None
    preview_token: str | None = None


class CameraResponse(BaseModel):
    id: int
    name: str
    storage_folder_name: str
    enabled: bool
    protocol: str
    host: str
    port: int
    username: str | None
    rtsp_main_url: str | None
    rtsp_sub_url: str | None
    rtsp_reachable_host: str | None = None
    rtsp_reachable_port: int | None = None
    rtsp_transport: str | None
    onvif_path: str | None
    onvif_profile_token: str | None
    onvif_channel_id: str | None
    recording_mode: str
    default_live_stream: str
    default_record_stream: str
    segment_minutes: int
    retention_days: int
    storage_quota_gb: int
    status: str
    last_error: str | None
    preview_url: str | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
