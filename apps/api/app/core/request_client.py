"""Trust client headers only from the shipped nginx peer, never from the LAN."""
from __future__ import annotations

import ipaddress
import socket
import threading
import time

_proxy_lock = threading.Lock()
_proxy_ips: frozenset[str] = frozenset()
_proxy_refresh_at = 0.0
_proxy_lookup_ok = False
_negative_peer = None
_negative_until = 0.0


class ClientIdentityUnavailable(RuntimeError):
    pass


def _canonical_ip(value):
    try:
        address = ipaddress.ip_address(str(value))
        return str(getattr(address, "ipv4_mapped", None) or address)
    except ValueError:
        return None


def _trusted_proxy_ips(peer_ip=None):
    global _proxy_ips, _proxy_refresh_at, _proxy_lookup_ok, _negative_peer, _negative_until
    now = time.monotonic()
    with _proxy_lock:
        new_peer = peer_ip and peer_ip not in _proxy_ips and not (
            peer_ip == _negative_peer and now < _negative_until
        )
        failure_cooldown = not _proxy_lookup_ok and now < _proxy_refresh_at
        if now >= _proxy_refresh_at or (new_peer and not failure_cooldown):
            try:
                entries = socket.getaddrinfo("nginx", 80, type=socket.SOCK_STREAM)
                resolved = frozenset(filter(None, (_canonical_ip(entry[4][0]) for entry in entries)))
                if not resolved:
                    raise OSError("Proxy identity unavailable")
                _proxy_ips = resolved
                _proxy_lookup_ok = True
            except OSError:
                # A temporary DNS failure must not collapse known proxy clients
                # into one address, nor grant trust to a new unverified peer.
                _proxy_lookup_ok = False
            # API can start before nginx. Retry failed discovery on demand; no
            # polling thread, broad private-network trust or caller-supplied host.
            _proxy_refresh_at = time.monotonic() + (30 if _proxy_lookup_ok else 2)
            _negative_peer = peer_ip if peer_ip not in _proxy_ips else None
            _negative_until = time.monotonic() + 1
        return _proxy_ips


def request_client_ip(request, *, strict=False):
    if request is None:
        return None
    peer = getattr(getattr(request, "client", None), "host", None)
    peer_ip = _canonical_ip(peer)
    forwarded_text = request.headers.get("x-real-ip", "")
    if not forwarded_text:
        return peer_ip or (str(peer)[:100] if peer else None)
    if peer_ip and peer_ip in _trusted_proxy_ips(peer_ip):
        # Shipped nginx overwrites this header. X-Forwarded-For can contain an
        # arbitrary caller-controlled chain and is deliberately not consulted.
        forwarded = _canonical_ip(forwarded_text)
        if forwarded:
            return forwarded
        if strict:
            raise ClientIdentityUnavailable()
    elif strict and _canonical_ip(forwarded_text) and not _proxy_lookup_ok:
        # Fail transiently BEFORE the login budget, rather than letting one
        # caller's failures lock every client behind an unresolved proxy peer.
        raise ClientIdentityUnavailable()
    return peer_ip or (str(peer)[:100] if peer else None)
