#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


LOCALES = {"en", "ru", "zh-CN"}
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
INITIAL_SLOT_RE = re.compile(r"^initial-[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
SENSITIVE_RE = re.compile(
    r"(github_pat_|ghp_|Bearer\s+|rtsp://[^@\s]+@|"
    r"postgresql://[^:\s]+:[^@\s]+@|"
    r"-----BEGIN [^-]*PRIVATE KEY-----)",
    re.IGNORECASE,
)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def plain_text(
    value: Any,
    *,
    field: str,
    max_length: int,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str:
        fail(f"{field} must be plain text")
    text = value.strip()
    if (
        (not text and not allow_empty)
        or len(text) > max_length
        or CONTROL_RE.search(text)
        or SENSITIVE_RE.search(text)
    ):
        fail(f"{field} is invalid")
    return text


def text_map(
    value: Any,
    *,
    field: str,
    max_length: int,
) -> dict[str, str]:
    if type(value) is not dict or set(value) != LOCALES:
        fail(f"{field} locale map is invalid")
    return {
        locale: plain_text(
            value[locale],
            field=f"{field}.{locale}",
            max_length=max_length,
        )
        for locale in ("en", "ru", "zh-CN")
    }


def changelog(value: Any, *, field: str) -> list[str]:
    if type(value) is not list or len(value) > 20:
        fail(f"{field} must be a bounded list")
    return [
        plain_text(
            item,
            field=f"{field}[{index}]",
            max_length=180,
        )
        for index, item in enumerate(value)
    ]


def changelog_map(
    value: Any,
    *,
    field: str,
) -> dict[str, list[str]]:
    if type(value) is not dict or set(value) != LOCALES:
        fail(f"{field} locale map is invalid")
    return {
        locale: changelog(
            value[locale],
            field=f"{field}.{locale}",
        )
        for locale in ("en", "ru", "zh-CN")
    }


def parse_timestamp(value: str) -> str:
    text = plain_text(value, field="installed_at", max_length=80)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        fail("installed_at is invalid")
    if parsed.tzinfo is None:
        fail("installed_at timezone is missing")
    return text


def build_identity(args: argparse.Namespace) -> dict[str, Any]:
    try:
        descriptor = json.loads(
            Path(args.descriptor).read_text(encoding="utf-8")
        )
    except Exception:
        fail("release descriptor is unreadable")
    if type(descriptor) is not dict:
        fail("release descriptor must be an object")
    if (
        descriptor.get("schema_version") != 1
        or descriptor.get("product") != "KM VMS"
    ):
        fail("release descriptor core identity is invalid")
    version = plain_text(
        descriptor.get("version"),
        field="version",
        max_length=80,
    )
    if not VERSION_RE.fullmatch(version):
        fail("version is invalid")
    commit = str(args.commit or "").strip().lower()
    if commit and not COMMIT_RE.fullmatch(commit):
        fail("commit is invalid")
    installed_by = plain_text(
        args.installed_by,
        field="installed_by",
        max_length=100,
    )
    metadata_source = plain_text(
        args.metadata_source,
        field="metadata_source",
        max_length=100,
    )
    if (
        not TOKEN_RE.fullmatch(installed_by)
        or not TOKEN_RE.fullmatch(metadata_source)
    ):
        fail("identity metadata token is invalid")
    metadata_status = plain_text(
        args.metadata_status,
        field="metadata_status",
        max_length=20,
    )
    if metadata_status not in {"precompose", "partial", "complete"}:
        fail("metadata_status is invalid")
    identity: dict[str, Any] = {
        "schema_version": 1,
        "product": "KM VMS",
        "version": version,
        "release_channel": plain_text(
            descriptor.get("release_channel"),
            field="release_channel",
            max_length=80,
        ),
        "source_kind": plain_text(
            descriptor.get("source_kind"),
            field="source_kind",
            max_length=80,
        ),
        "source_repo": plain_text(
            descriptor.get("source_repo"),
            field="source_repo",
            max_length=160,
        ),
        "source_ref": plain_text(
            descriptor.get("source_ref"),
            field="source_ref",
            max_length=120,
        ),
        "commit_sha": commit or None,
        "installed_at": parse_timestamp(args.installed_at),
        "installed_by": installed_by,
        "metadata_status": metadata_status,
        "metadata_source": metadata_source,
    }
    identity_mode = str(args.identity_mode or "").strip()
    slot_kind = str(args.slot_kind or "").strip()
    slot_id = str(args.slot_id or "").strip().lower()
    if identity_mode or slot_kind or slot_id:
        if (
            identity_mode != "inventory_bound"
            or slot_kind != "initial_install_snapshot"
            or not INITIAL_SLOT_RE.fullmatch(slot_id)
            or commit
        ):
            fail("inventory-bound initial identity is invalid")
        identity["identity_mode"] = identity_mode
        identity["slot_kind"] = slot_kind
        identity["slot_id"] = slot_id
    for field, max_length in (("title", 160), ("summary", 800)):
        if field not in descriptor:
            continue
        text = plain_text(
            descriptor[field],
            field=field,
            max_length=max_length,
            allow_empty=True,
        )
        if text:
            identity[field] = text
    if "changelog" in descriptor:
        identity["changelog"] = changelog(
            descriptor["changelog"],
            field="changelog",
        )
    for field, max_length in (
        ("title_i18n", 160),
        ("summary_i18n", 800),
    ):
        if field in descriptor:
            identity[field] = text_map(
                descriptor[field],
                field=field,
                max_length=max_length,
            )
    if "changelog_i18n" in descriptor:
        identity["changelog_i18n"] = changelog_map(
            descriptor["changelog_i18n"],
            field="changelog_i18n",
        )
    return identity


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build one bounded KM VMS installed release identity.",
    )
    parser.add_argument("--descriptor", required=True)
    parser.add_argument("--commit", default="")
    parser.add_argument("--installed-at", required=True)
    parser.add_argument("--installed-by", required=True)
    parser.add_argument("--metadata-status", required=True)
    parser.add_argument("--metadata-source", required=True)
    parser.add_argument("--identity-mode", default="")
    parser.add_argument("--slot-kind", default="")
    parser.add_argument("--slot-id", default="")
    args = parser.parse_args()
    print(
        json.dumps(
            build_identity(args),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
