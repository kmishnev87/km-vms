from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from app.services import schema_update_control
from app.services import update_check


ROOT = Path(__file__).resolve().parents[3]
COMMIT = "a" * 40


def _descriptor(**overrides):
    payload = {
        "schema_version": 1,
        "product": "KM VMS",
        "version": "0.8.6",
        "tag": "v0.8.6",
        "title": "Plain release title",
        "summary": "Plain release summary",
        "title_i18n": {
            "en": "English release title",
            "ru": "Русское название релиза",
            "zh-CN": "中文版本标题",
        },
        "summary_i18n": {
            "en": "English release summary",
            "ru": "Русское описание релиза",
            "zh-CN": "中文版本说明",
        },
        "changelog": ["x" * 180],
        "changelog_i18n": {
            "en": ["English change"],
            "ru": ["Русское изменение"],
            "zh-CN": ["中文变更"],
        },
        "release_channel": "public-github",
        "source_kind": "github-release",
        "source_repo": "kmishnev87/km-vms",
        "source_ref": "v0.8.6",
        "commit_sha": COMMIT,
        "published_at": None,
        "requires_backup": False,
        "requires_manual_action": False,
        "requires_migration": False,
    }
    payload.update(overrides)
    return payload


def _identity(**overrides):
    payload = {
        "schema_version": 1,
        "product": "KM VMS",
        "version": "0.8.6",
        "release_channel": "public-github",
        "source_kind": "github-release",
        "source_repo": "kmishnev87/km-vms",
        "source_ref": "v0.8.6",
        "commit_sha": COMMIT,
        "installed_at": "2026-07-30T10:00:00Z",
        "installed_by": "in_app_helper",
        "metadata_status": "complete",
        "metadata_source": "helper",
    }
    payload.update(overrides)
    return payload


def test_release_note_maps_propagate_without_truncating_180_chars(
    tmp_path: Path,
    monkeypatch,
):
    manifest = update_check._manifest_from_release_payload(_descriptor())
    assert manifest.title_i18n["ru"] == "Русское название релиза"
    assert manifest.summary_i18n["zh-CN"] == "中文版本说明"
    assert manifest.breaking_changes == ["x" * 180]
    assert manifest.changelog_i18n["en"] == ["English change"]

    identity = _identity(
        title="Plain release title",
        summary="Plain release summary",
        changelog=["x" * 180],
        title_i18n=_descriptor()["title_i18n"],
        summary_i18n=_descriptor()["summary_i18n"],
        changelog_i18n=_descriptor()["changelog_i18n"],
    )
    (tmp_path / ".km-vms-release.json").write_text(
        json.dumps(identity, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        update_check,
        "installed_build_metadata",
        lambda: {
            "app_version": "0.8.6",
            "git_commit": None,
            "metadata_source": "development_fallback",
        },
    )
    installed = update_check.read_installed_update_state(app_root=tmp_path)
    assert installed.installed_title_i18n == identity["title_i18n"]
    assert installed.installed_changelog == ["x" * 180]

    result = update_check.UpdateCheckResult(
        status="update_available",
        installed=installed,
        latest=manifest,
        blockers=[],
        warnings=[],
        checked_at="2026-07-30T10:01:00Z",
        manifest_source_status="public_github_release",
    )
    public = update_check._result_payload(result)
    assert public["available_release"]["title_i18n"] == identity["title_i18n"]
    assert public["installed_release"]["summary_i18n"] == identity["summary_i18n"]

    snapshot = update_check._trusted_apply_snapshot_from_payload(
        public,
        now=datetime(2026, 7, 30, 10, 1),
    )
    assert snapshot is not None
    assert snapshot["latest"]["changelog_i18n"] == identity["changelog_i18n"]
    assert snapshot["latest"]["breaking_changes"] == ["x" * 180]


@pytest.mark.parametrize(
    "field,value",
    [
        (
            "title_i18n",
            {"en": "English", "ru": "Русский", "zh-CN": "中文", "de": "Deutsch"},
        ),
        ("summary_i18n", {"en": "English", "ru": "Русский"}),
        ("changelog_i18n", {"en": ["ok"], "ru": ["ok"], "zh-CN": "wrong"}),
        ("changelog", ["x" * 181]),
    ],
)
def test_release_note_contract_rejects_unknown_wrong_or_oversize_values(
    field,
    value,
):
    payload = _descriptor()
    payload[field] = value
    with pytest.raises(update_check.UpdateCheckBlocked):
        update_check._manifest_from_release_payload(payload)


def test_installed_identity_without_notes_stays_valid_and_honestly_empty(
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / ".km-vms-release.json").write_text(
        json.dumps(_identity()),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        update_check,
        "installed_build_metadata",
        lambda: {
            "app_version": "0.8.6",
            "git_commit": None,
            "metadata_source": "development_fallback",
        },
    )
    installed = update_check.read_installed_update_state(app_root=tmp_path)
    assert installed.status == "known"
    assert installed.installed_title is None
    assert installed.installed_summary is None
    assert installed.installed_changelog == []
    assert not any(
        item.code == "release_identity_notes_invalid"
        for item in installed.warnings
    )


def test_schema_identity_accepts_known_optional_notes_only(
    tmp_path: Path,
    monkeypatch,
):
    identity_path = tmp_path / ".km-vms-release.json"
    monkeypatch.setattr(schema_update_control, "RELEASE_PATH", identity_path)
    request = {"source": {"commit": COMMIT}}

    for notes in (
        {},
        {"title": "Old plain title", "summary": "Old plain summary"},
        {
            "title_i18n": _descriptor()["title_i18n"],
            "summary_i18n": _descriptor()["summary_i18n"],
            "changelog_i18n": _descriptor()["changelog_i18n"],
        },
    ):
        identity_path.write_text(
            json.dumps({**_identity(), **notes}, ensure_ascii=False),
            encoding="utf-8",
        )
        assert schema_update_control.target_identity(request) == (
            "0.8.6",
            COMMIT,
        )

    invalid = {**_identity(), "unknown_note": "not allowlisted"}
    identity_path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(schema_update_control.SchemaControlError):
        schema_update_control.target_identity(request)

    malformed = {
        **_identity(),
        "title_i18n": {"en": "English", "ru": "Русский"},
    }
    identity_path.write_text(
        json.dumps(malformed, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(schema_update_control.SchemaControlError):
        schema_update_control.target_identity(request)


def test_identity_builder_copies_only_allowlisted_descriptor_notes(
    tmp_path: Path,
):
    descriptor_path = tmp_path / "release.json"
    descriptor = _descriptor()
    descriptor.pop("title")
    descriptor.pop("summary")
    descriptor.pop("changelog")
    descriptor_path.write_text(
        json.dumps(descriptor, ensure_ascii=False),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/km-vms-release-identity.py"),
            "--descriptor",
            str(descriptor_path),
            "--commit",
            COMMIT,
            "--installed-at",
            "2026-07-30T10:00:00Z",
            "--installed-by",
            "in_app_helper",
            "--metadata-status",
            "complete",
            "--metadata-source",
            "helper",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    identity = json.loads(result.stdout)
    assert "title" not in identity
    assert "summary" not in identity
    assert "changelog" not in identity
    assert identity["title_i18n"] == descriptor["title_i18n"]
    assert identity["changelog_i18n"] == descriptor["changelog_i18n"]

    forbidden = (
        "Public GitHub Release Identity and Drift-Proof Update Status",
        "Public GitHub install/update identity and update status hardening",
        "KM VMS <tag>",
    )
    for relative in (
        "scripts/install.sh",
        "scripts/update.sh",
        "scripts/km-vms-adopt-release-identity.sh",
        "scripts/km-vms-release-slots.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert not any(item in text for item in forbidden)
