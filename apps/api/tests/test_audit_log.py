from app.core.sanitization import redact_text as shared_redact_text
from app.services.audit_log import create_event, redact_text, sanitize_metadata


def test_audit_redaction_masks_sensitive_metadata():
    payload = sanitize_metadata(
        {
            "password": "plain-secret",
            "access_token": "abc.def.ghi",
            "Authorization": "Bearer real-token",
            "camera": {
                "rtsp_url": "rtsp://admin:camera-pass@192.168.1.20/stream1",
                "name": "Front gate",
            },
            "items": [{"refresh_token": "refresh-secret"}],
        }
    )

    text = str(payload)
    assert "plain-secret" not in text
    assert "real-token" not in text
    assert "camera-pass" not in text
    assert "refresh-secret" not in text
    assert payload["password"] == "***"
    assert payload["access_token"] == "***"
    assert payload["Authorization"] == "***"
    assert payload["camera"]["rtsp_url"] == "rtsp://admin:***@192.168.1.20/stream1"
    assert payload["camera"]["name"] == "Front gate"


def test_audit_text_redaction_masks_tokens_and_rtsp_credentials():
    redacted = redact_text(
        "Authorization: Bearer secret-token rtsp://user:pass@host/live?access_token=secret-query"
    )

    assert "secret-token" not in redacted
    assert "pass@host" not in redacted
    assert "secret-query" not in redacted
    assert "Bearer ***" in redacted
    assert "rtsp://user:***@host/live" in redacted
    assert "access_token=***" in redacted


def test_audit_redact_text_compatibility_alias_matches_shared_sanitization():
    values = [
        None,
        "",
        "normal text",
        "rtsp://user:pass@host/live",
        "Authorization: Bearer sample-token",
        "http://host/path?access_token=query-token",
        "Cookie: sessionid=session-token; other=1",
        "postgresql://user:pass@db/app",
    ]

    for value in values:
        assert redact_text(value) == shared_redact_text(value)


def test_noop_camera_update_audit_event_is_skipped_before_persistence():
    event = create_event(
        category="cameras",
        event_type="cameras.updated",
        message_ru="camera updated",
        metadata={"changed": {}, "credential_changed": False},
    )

    assert event is None
