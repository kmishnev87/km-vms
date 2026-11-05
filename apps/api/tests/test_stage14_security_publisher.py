"""Actual API/helper contracts, with only external publisher metadata mocked."""
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import sys
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load(relative):
    name = "stage14_publisher_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def policy():
    return _load("scripts/km_vms_helper_handlers/publisher.py")


def _source(version="0.9.1"):
    return {"kind": "trusted_manifest", "channel": "stable", "version": version,
            "source_type": "github_tarball", "repo": "kmishnev87/km-vms",
            "ref": "v" + version, "commit": "a" * 40, "apply_ref": "a" * 40}


def _metadata(source, *, annotated=False):
    tag = "v" + source["version"]
    result = {
        "/releases/tags/" + tag: {"draft": False, "tag_name": tag, "published_at": "2026-11-05T00:00:00Z", "target_commitish": "not-an-authority"},
        "/git/ref/tags/" + tag: {"ref": "refs/tags/" + tag,
                                "object": {"type": "tag" if annotated else "commit", "sha": "b" * 40 if annotated else source["commit"]}},
    }
    if annotated:
        result["/git/tags/" + "b" * 40] = {"object": {"type": "commit", "sha": source["commit"]}}
    return result


@pytest.mark.parametrize("version", ["0.9.1", "0.8.29", "1.4.0", "1.4.0-beta.1"])
@pytest.mark.parametrize("annotated", [False, True])
@pytest.mark.parametrize("pinned_ref", [False, True])
def test_any_official_published_matching_tag_can_update_without_current_version_pin(policy, version, annotated, pinned_ref):
    source = _source(version)
    if pinned_ref:
        source["ref"] = source["commit"]
    records = _metadata(source, annotated=annotated)
    calls = []

    def fetch(path, deadline):
        calls.append(path)
        assert deadline > 0
        return records[path]

    assert policy.verify_published_source(source, get_json=fetch) == source
    assert len(calls) == (3 if annotated else 2)


@pytest.mark.parametrize("patch", [{"repo": "foreign/project"}, {"ref": "main"}, {"ref": "c" * 40}, {"ref": None}, {"ref": {}}, {"version": "0.9.2"}, {"version": None}, {"apply_ref": "c" * 40}])
def test_untrusted_schema_is_rejected_before_any_network_or_child(policy, patch):
    def forbidden(*_):
        raise AssertionError("invalid source reached network")

    with pytest.raises(policy.PublishedSourceError):
        policy.verify_published_source({**_source(), **patch}, get_json=forbidden)


@pytest.mark.parametrize("patch", [{"draft": True}, {"draft": None}, {"published_at": None}, {"published_at": "nonsense"}, {"published_at": "2026-11-05"}, {"tag_name": "v0.9.0"}])
@pytest.mark.parametrize("pinned_ref", [False, True])
def test_draft_unpublished_malformed_or_wrong_release_cannot_authorize(policy, patch, pinned_ref):
    source = _source()
    if pinned_ref:
        source["ref"] = source["commit"]
    records = _metadata(source)
    records["/releases/tags/v0.9.1"].update(patch)
    with pytest.raises(policy.PublishedSourceError):
        policy.verify_published_source(source, get_json=lambda path, _: records[path])


@pytest.mark.parametrize("pinned_ref", [False, True])
def test_ref_identity_commit_mismatch_and_tag_cycles_are_rejected(policy, pinned_ref):
    source = _source()
    if pinned_ref:
        source["ref"] = source["commit"]
    for bad_object in ({"type": "commit", "sha": "c" * 40}, {"type": "tree", "sha": "a" * 40}, None):
        records = _metadata(source)
        records["/git/ref/tags/v0.9.1"]["object"] = bad_object
        with pytest.raises(policy.PublishedSourceError):
            policy.verify_published_source(source, get_json=lambda path, _: records[path])
    records = _metadata(source, annotated=True)
    records["/git/tags/" + "b" * 40]["object"] = {"type": "tag", "sha": "b" * 40}
    calls = []

    def cycle(path, _):
        calls.append(path)
        return records[path]

    with pytest.raises(policy.PublishedSourceError):
        policy.verify_published_source(source, get_json=cycle)
    assert len(calls) == 2 + policy.MAX_TAG_HOPS


@pytest.mark.parametrize("annotated", [False, True])
@pytest.mark.parametrize("matching_commit", [False, True])
def test_real_release_descriptor_api_projection_reaches_installed_publisher(monkeypatch, annotated, matching_commit):
    from app.services import update_apply, update_check

    payload = json.loads((ROOT / "release/km-vms-release.json").read_text(encoding="utf-8"))
    tag = "v" + payload["version"]
    commit = "a" * 40
    calls = []

    def resolve_tag(repo, requested_tag):
        assert repo == "kmishnev87/km-vms"
        assert requested_tag == tag
        return commit

    def read_descriptor(url):
        assert url == f"https://raw.githubusercontent.com/kmishnev87/km-vms/{tag}/release/km-vms-release.json"
        return dict(payload)

    monkeypatch.setattr(update_check, "_resolve_public_tag_commit", resolve_tag)
    monkeypatch.setattr(update_check, "_read_public_release_payload", read_descriptor)
    manifest = update_check._manifest_from_public_release_tag("kmishnev87/km-vms", tag)
    source = update_apply._sanitized_source(asdict(manifest))
    assert source["ref"] == source["commit"] == source["apply_ref"] == commit
    assert manifest.git_ref == tag

    records = _metadata(source, annotated=annotated)
    if not matching_commit:
        path = "/git/tags/" + "b" * 40 if annotated else "/git/ref/tags/" + tag
        records[path]["object"]["sha"] = "c" * 40

    def fetch(path, deadline):
        calls.append(path)
        assert deadline > 0
        return records[path]

    helper = _load("scripts/km-vms-update-helper.py")
    publisher = sys.modules[helper._operation_handlers.verify_published_source.__module__]
    monkeypatch.setattr(publisher, "_github_json", fetch)
    if matching_commit:
        assert helper.authorize_update_source(source) == source
    else:
        with pytest.raises(helper.HelperError) as result:
            helper.authorize_update_source(source)
        assert result.value.category == "source_acquisition_failed"
    assert calls == list(records)


def test_helper_denies_before_resolving_or_running_any_update_child(monkeypatch, tmp_path):
    helper = _load("scripts/km-vms-update-helper.py")
    calls = []

    def deny(source):
        calls.append("authorize")
        raise helper.HelperError("source_acquisition_failed", "Synthetic rejection")

    monkeypatch.setattr(helper, "authorize_update_source", deny)
    monkeypatch.setattr(helper, "compose_app_dir", lambda: calls.append("compose"))
    monkeypatch.setattr(helper, "run_child_with_progress", lambda *_args, **_kwargs: calls.append("child"))
    with pytest.raises(helper.HelperError):
        helper.run_update({"source": _source()})
    assert calls == ["authorize"]


def test_provider_outage_is_retryable_and_does_not_use_api_cache(monkeypatch):
    helper = _load("scripts/km-vms-update-helper.py")

    def unavailable(_):
        raise helper._operation_handlers.PublishedSourceError(unavailable=True)

    monkeypatch.setattr(helper._operation_handlers, "verify_published_source", unavailable)
    with pytest.raises(helper.HelperError) as result:
        helper.authorize_update_source(_source())
    assert result.value.category == "source_temporarily_unavailable"


def test_resume_uses_existing_local_bootstrap_authority_not_journal_path_or_network(monkeypatch, tmp_path):
    helper = _load("scripts/km-vms-update-helper.py")
    request = {"request_id": "update-" + "1" * 32, "source": _source()}
    journal = {"request_id": request["request_id"], "target_slot": "attacker-controlled-name", "phase": "activating",
               "target_commit": "a" * 40, "target_version": "0.9.1"}
    calls = []
    monkeypatch.setattr(helper, "activation_journal", lambda _: journal)

    def resolve(path):
        calls.append(("trusted-local-bootstrap", path))
        journal["phase"] = "completed"

    monkeypatch.setattr(helper, "resolve_update_source_dir", resolve)
    monkeypatch.setattr(helper, "publish_activation_outcome", lambda *_: True)
    monkeypatch.setattr(helper, "authorize_update_source", lambda *_: pytest.fail("recovery requires no GitHub network"))
    monkeypatch.setattr(helper.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("journal-selected code was executed"))
    assert helper.resume_activation(request, tmp_path) == 0
    assert calls == [("trusted-local-bootstrap", tmp_path)]


def test_invalid_local_recovery_authority_never_falls_back_to_target_bridge(monkeypatch, tmp_path):
    helper = _load("scripts/km-vms-update-helper.py")
    request = {"request_id": "update-" + "2" * 32, "source": _source()}
    monkeypatch.setattr(helper, "activation_journal", lambda _: {
        "request_id": request["request_id"], "target_slot": "forged", "phase": "activating",
        "target_commit": "a" * 40, "target_version": "0.9.1",
    })

    def reject(_):
        raise helper.HelperError("activation_binding_invalid", "Synthetic invalid binding")

    monkeypatch.setattr(helper, "resolve_update_source_dir", reject)
    monkeypatch.setattr(helper.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("untrusted child"))
    with pytest.raises(helper.HelperError):
        helper.resume_activation(request, tmp_path)


def test_mismatched_journal_target_is_rejected_before_any_recovery_delegate(monkeypatch, tmp_path):
    helper = _load("scripts/km-vms-update-helper.py")
    request = {"request_id": "update-" + "3" * 32, "source": _source()}
    monkeypatch.setattr(helper, "activation_journal", lambda _: {
        "request_id": request["request_id"], "phase": "activating",
        "target_commit": "c" * 40, "target_version": "0.9.0",
    })
    monkeypatch.setattr(helper, "resolve_update_source_dir", lambda *_: pytest.fail("mismatched target reached recovery"))
    with pytest.raises(helper.HelperError) as result:
        helper.resume_activation(request, tmp_path)
    assert result.value.category == "activation_journal_invalid"
