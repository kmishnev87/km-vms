from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.system_settings import SystemSettings

FALLBACK_TIMEZONE = "UTC"
FILENAME_TIMESTAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})(?=\.[A-Za-z0-9]+$)")


@dataclass(frozen=True)
class TimezoneContext:
    name: str
    zone: ZoneInfo
    fallback_used: bool = False


@dataclass(frozen=True)
class ParsedTimestamp:
    storage_utc: datetime
    compatibility_local: datetime | None
    input_kind: str


def utc_now_storage() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def timezone_context(db: Session | None) -> TimezoneContext:
    raw = None
    if db is not None:
        try:
            row = db.query(SystemSettings).order_by(SystemSettings.id.asc()).first()
            raw = row.timezone if row else None
        except Exception:
            raw = None
    name = str(raw or FALLBACK_TIMEZONE).strip() or FALLBACK_TIMEZONE
    try:
        return TimezoneContext(name=name, zone=ZoneInfo(name), fallback_used=False)
    except Exception:
        return TimezoneContext(name=FALLBACK_TIMEZONE, zone=ZoneInfo(FALLBACK_TIMEZONE), fallback_used=True)


def aware_utc_to_storage_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("aware datetime required")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def storage_naive_utc_to_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=timezone.utc)


def system_local_naive_to_storage_utc(value: datetime, ctx: TimezoneContext) -> datetime:
    if value.tzinfo is not None:
        return aware_utc_to_storage_naive_utc(value)
    local = value.replace(tzinfo=ctx.zone, fold=0)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def storage_utc_to_system(value: datetime | None, ctx: TimezoneContext) -> datetime | None:
    if value is None:
        return None
    return storage_naive_utc_to_aware_utc(value).astimezone(ctx.zone)


def local_naive_to_system(value: datetime | None, ctx: TimezoneContext) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(ctx.zone)
    return value.replace(tzinfo=ctx.zone, fold=0)


def system_display_datetime(value: datetime | None, ctx: TimezoneContext, *, local_naive: bool = False) -> datetime | None:
    return local_naive_to_system(value, ctx) if local_naive else storage_utc_to_system(value, ctx)


def format_system_iso(value: datetime | None, ctx: TimezoneContext, *, local_naive: bool = False) -> str | None:
    converted = system_display_datetime(value, ctx, local_naive=local_naive)
    return converted.replace(microsecond=0).isoformat() if converted else None


def format_system_display(value: datetime | None, ctx: TimezoneContext, *, local_naive: bool = False) -> str | None:
    converted = system_display_datetime(value, ctx, local_naive=local_naive)
    return converted.strftime("%d.%m.%Y, %H:%M:%S") if converted else None


def timestamp_matches_filename(value: datetime | None, filename: str | None) -> bool:
    if value is None or not filename:
        return False
    match = FILENAME_TIMESTAMP_RE.search(str(filename))
    if not match:
        return False
    try:
        parsed = datetime.strptime(match.group(1), "%Y-%m-%d-%H-%M-%S")
    except ValueError:
        return False
    return parsed == value.replace(microsecond=0, tzinfo=None)


def parse_api_timestamp(raw: str, ctx: TimezoneContext, *, field_name: str = "timestamp") -> ParsedTimestamp:
    try:
        value = datetime.fromisoformat(str(raw).strip())
    except Exception as exc:
        raise ValueError(f"Invalid {field_name}") from exc
    if value.tzinfo is not None:
        return ParsedTimestamp(
            storage_utc=aware_utc_to_storage_naive_utc(value),
            compatibility_local=None,
            input_kind="offset-aware",
        )
    return ParsedTimestamp(
        storage_utc=system_local_naive_to_storage_utc(value, ctx),
        compatibility_local=value.replace(tzinfo=None),
        input_kind="system-local-naive",
    )


def parse_local_date(value: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except Exception as exc:
        raise ValueError("Invalid date") from exc


def local_day_storage_bounds(value: date, ctx: TimezoneContext) -> tuple[datetime, datetime, datetime, datetime]:
    start_local = datetime.combine(value, time.min)
    end_naive = datetime.combine(value + timedelta(days=1), time.min)
    return (
        system_local_naive_to_storage_utc(start_local, ctx),
        system_local_naive_to_storage_utc(end_naive, ctx),
        start_local,
        end_naive,
    )


def retention_cutoff_storage(retention_days: int, ctx: TimezoneContext, *, now_utc: datetime | None = None) -> tuple[datetime, datetime]:
    if retention_days <= 0:
        raise ValueError("retention_days must be positive")
    now = now_utc or utc_now_storage()
    aware_now = storage_naive_utc_to_aware_utc(now)
    local_now = aware_now.astimezone(ctx.zone)
    cutoff_date = local_now.date() - timedelta(days=retention_days)
    cutoff_local = datetime.combine(cutoff_date, time.min)
    return system_local_naive_to_storage_utc(cutoff_local, ctx), cutoff_local
