from datetime import datetime
import re

from pydantic import BaseModel, Field, field_serializer


DELETED_CAMERA_MARKER_RE = re.compile(r"__deleted_\d+_\d+$")


def active_camera_display_value(value: str, deleted_at: datetime | None) -> str:
    if deleted_at is not None:
        return value
    return DELETED_CAMERA_MARKER_RE.sub("", str(value or ""))


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
    validation_token: str | None = None
    onvif_probe_token: str | None = None
    manual_confirm_unverified: bool = False


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
    validation_token: str | None = None
    onvif_probe_token: str | None = None
    manual_confirm_unverified: bool = False


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
    rtsp_host: str | None = None
    rtsp_port: int | None = None
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
    deleted_at: datetime | None = None
    preview_url: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_serializer("name")
    def serialize_active_name(self, value: str) -> str:
        return active_camera_display_value(value, self.deleted_at)

    @field_serializer("storage_folder_name")
    def serialize_active_storage_folder_name(self, value: str) -> str:
        return active_camera_display_value(value, self.deleted_at)

    class Config:
        from_attributes = True
