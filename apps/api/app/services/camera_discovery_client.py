"""Private IPC transport. API remains socket-free with respect to Docker."""
from __future__ import annotations

import http.client
import json
import os
import socket


class DiscoveryUnavailable(Exception):
    pass


class DiscoveryRequestError(Exception):
    def __init__(self, status, code):
        self.status = status
        self.code = code


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(os.environ.get("CAMERA_DISCOVERY_SOCKET", "/run/km-vms-discovery/control.sock"))


def discovery_request(path, payload):
    connection = UnixConnection("localhost", timeout=6)
    try:
        connection.request("POST", path, json.dumps(payload).encode(), {"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read(1048577)
        if len(body) > 1048576:
            raise DiscoveryUnavailable()
        result = json.loads(body)
        if response.status != 200:
            raise DiscoveryRequestError(response.status, result.get("code", "discovery_unavailable"))
        return result
    except DiscoveryRequestError:
        raise
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise DiscoveryUnavailable() from exc
    finally:
        connection.close()
