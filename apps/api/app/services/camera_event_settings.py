"""Explicit per-camera ingestion; separate from personal notifications/detection."""
from fastapi import HTTPException

from app.services.onvif_events import DeviceClient, EventDeviceError


def read_event_capabilities(*, host, port, username, password, onvif_path=None):
    client = None
    try:
        client = DeviceClient(host, port, username, password, onvif_path)
        return client.event_capabilities()
    except EventDeviceError as exc:
        if exc.code == "events_unsupported":
            return {"supported": False, "status": "unsupported", "kinds": []}
        return {"supported": None, "status": "unknown", "kinds": []}
    except Exception:
        return {"supported": None, "status": "unknown", "kinds": []}
    finally:
        if client is not None:
            client.close()  # No subscription was created by this capability read.


def validate_event_receiving(enabled, connection):
    if not enabled:
        return
    if connection.get("protocol") != "onvif":
        raise HTTPException(status_code=422, detail={"code": "event_reception_onvif_required"})
    result = read_event_capabilities(**{key: connection.get(key) for key in ("host", "port", "username", "password", "onvif_path")})
    if result["supported"] is not True:
        raise HTTPException(status_code=409, detail={"code": "event_reception_support_unconfirmed"})
