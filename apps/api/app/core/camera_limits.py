"""Supported camera policy bounds, shared by validation and archive consumers."""

MAX_CAMERA_NAME_LENGTH = 255
MAX_RETENTION_DAYS = 36500  # Up to one hundred years; not an unlimited sentinel.


def retention_days_supported(value: int) -> bool:
    return 1 <= value <= MAX_RETENTION_DAYS
