"""Bounded, unauthenticated camera discovery. No camera configuration or video I/O."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import http.client
import ipaddress
import re
import select
import socket
import ssl
import struct
import time
from urllib.parse import unquote, urlsplit
import uuid
import xml.etree.ElementTree as ET

MAX_HOSTS = 1024
MAX_CANDIDATES = 1024
MAX_PORTS = 12
PRIVATE_NETWORKS = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")))
DEFAULT_ONVIF_PORTS = (80, 443, 2020, 8000, 8080, 8899)
DEFAULT_RTSP_PORTS = (554, 8554)
ONVIF_NS = "http://www.onvif.org/ver10/device/wsdl"
NVT_NS = "http://www.onvif.org/ver10/network/wsdl"
DEVICE_PATHS = ("/onvif/device_service", "/onvif/device_service/")
SOAP_DATE = ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
             '<GetSystemDateAndTime xmlns="' + ONVIF_NS + '"/></s:Body></s:Envelope>').encode()


def private_address(value):
    try:
        address = ipaddress.ip_address(str(value))
        return address if address.version == 4 and any(address in net for net in PRIVATE_NETWORKS) else None
    except ValueError:
        return None


def clean_text(value, maximum=120):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).strip()[:maximum]


def parse_ranges(values):
    if not isinstance(values, list) or len(values) > 8:
        raise ValueError("invalid_networks")
    networks = []
    hosts = set()
    for value in values:
        try:
            network = ipaddress.ip_network(str(value).strip(), strict=False)
        except ValueError as exc:
            raise ValueError("invalid_network") from exc
        if network.version != 4 or not any(network.subnet_of(net) for net in PRIVATE_NETWORKS):
            raise ValueError("private_ipv4_required")
        # Check BEFORE materializing a /8 (or iterating over its hosts).
        if network.num_addresses > MAX_HOSTS + 2:
            raise ValueError("network_too_large")
        networks.append(network)
        hosts.update(map(str, network.hosts()))
        if len(hosts) > MAX_HOSTS:
            raise ValueError("too_many_hosts")
    return networks, sorted(hosts, key=ipaddress.ip_address)


def parse_ports(values, default):
    if values is None:
        return list(default)
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_PORTS:
        raise ValueError("invalid_ports")
    if any(type(value) is not int or not 1 <= value <= 65535 for value in values):
        raise ValueError("invalid_ports")
    return sorted(set(values))


def host_networks():
    """Read the worker's host namespace; never inspect WireGuard keys or config."""
    import fcntl
    from pathlib import Path
    result = {}
    interfaces = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        for _, name in socket.if_nameindex():
            if name == "lo" or name.startswith(("docker", "br-", "veth", "km-sc-", "virbr")):
                continue
            try:
                packed = struct.pack("256s", name[:15].encode())
                address = socket.inet_ntoa(fcntl.ioctl(probe.fileno(), 0x8915, packed)[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(probe.fileno(), 0x891b, packed)[20:24])
                if not private_address(address):
                    continue
                kind = "routed" if name.startswith(("wg", "tun", "tap", "tailscale", "zt")) else "local"
                interfaces[name] = {"address": address, "kind": kind}
                network = ipaddress.ip_network(f"{address}/{mask}", strict=False)
                result[str(network)] = {"cidr": str(network), "interface": name, "kind": kind, "address": address}
            except OSError:
                continue
    try:
        lines = Path("/proc/net/route").read_text().splitlines()[1:]
    except OSError:
        lines = []
    for line in lines:
        fields = line.split()
        if len(fields) < 8 or fields[0] not in interfaces or fields[1] == "00000000":
            continue
        try:
            if not int(fields[3], 16) & 1:
                continue
            address = socket.inet_ntoa(struct.pack("<I", int(fields[1], 16)))
            mask = socket.inet_ntoa(struct.pack("<I", int(fields[7], 16)))
            network = ipaddress.ip_network(f"{address}/{mask}", strict=False)
            if not any(network.subnet_of(net) for net in PRIVATE_NETWORKS):
                continue
            cidr = str(network)
            if network.prefixlen == 32 and any(network.subnet_of(ipaddress.ip_network(key)) for key in result):
                continue  # Do not offer a duplicate gateway/own-address route.
            result.setdefault(cidr, {"cidr": cidr, "interface": fields[0], "kind": "routed", "address": interfaces[fields[0]]["address"]})
        except (ValueError, OSError):
            continue
    return sorted(({**row, "selectable": ipaddress.ip_network(row["cidr"]).num_addresses <= MAX_HOSTS + 2}
                   for row in result.values()), key=lambda row: (row["kind"] != "local", row["cidr"]))


def candidate(host, source):
    return {"id": hashlib.sha256(str(host).encode()).hexdigest()[:20], "host": str(host), "name": "",
            "port": 80, "xaddr_path": "/onvif/device_service", "protocols": [], "onvif_endpoints": [],
            "rtsp_ports": [], "sources": [source], "source": source, "auth_required": False}


def onvif_candidate(xaddr, source="ws_discovery", name=""):
    try:
        url = urlsplit(str(xaddr))
        host = private_address(url.hostname)
        port = url.port or (443 if url.scheme == "https" else 80)
        if not host or url.scheme not in {"http", "https"} or url.username or url.password or not 1 <= port <= 65535:
            return None
        path = url.path or "/onvif/device_service"
        if len(path) > 240 or not re.fullmatch(r"/[A-Za-z0-9_./%:~+@-]*", path):
            return None
        item = candidate(host, source)
        item.update(name=clean_text(name), port=port, xaddr_path=path, protocols=["onvif"],
                    onvif_endpoints=[{"port": port, "path": path, "scheme": url.scheme}])
        return item
    except (ValueError, TypeError):
        return None


def merge_candidate(current, incoming):
    result = dict(current or incoming)
    if not current:
        return result
    for field in ("protocols", "sources", "rtsp_ports", "onvif_endpoints"):
        result[field] = list(current.get(field) or [])
        for entry in incoming.get(field) or []:
            if entry not in result[field]:
                result[field].append(entry)
    if incoming.get("onvif_endpoints") and not current.get("onvif_endpoints"):
        result["port"], result["xaddr_path"] = incoming["port"], incoming["xaddr_path"]
    result["name"] = current.get("name") or incoming.get("name") or ""
    result["auth_required"] = bool(current.get("auth_required") or incoming.get("auth_required"))
    return result


def _onvif_types(types, scopes):
    for item in types:
        namespace = getattr(item, "getNamespace", lambda: "")()
        local = getattr(item, "getLocalname", lambda: "")()
        if (namespace, local) in {(ONVIF_NS, "Device"), (NVT_NS, "NetworkVideoTransmitter")}:
            return True
        text = str(item)
        if (ONVIF_NS in text or NVT_NS in text) and ("Device" in text or "NetworkVideoTransmitter" in text):
            return True
    return any(str(scope).startswith("onvif://") for scope in scopes)


def normalize_services(services):
    result = {}
    for service in services:
        try:
            scopes = list(service.getScopes() or [])
            if not _onvif_types(service.getTypes() or [], scopes):
                continue
            name = next((unquote(str(scope).split("/name/", 1)[1]) for scope in scopes if "/name/" in str(scope)), "")
            for xaddr in service.getXAddrs() or []:
                item = onvif_candidate(xaddr, name=name)
                if item:
                    result[item["host"]] = merge_candidate(result.get(item["host"]), item)
        except Exception:
            continue  # A malformed device must not hide healthy neighbours.
    return list(result.values())


def multicast_discovery(timeout, cancelled, publish):
    from wsdiscovery.discovery import ThreadedWSDiscovery
    allowed = {row["address"] for row in host_networks() if row["kind"] == "local"}

    class LocalDiscovery(ThreadedWSDiscovery):
        def _networkAddressAdded(self, address):
            if str(address) in allowed:
                super()._networkAddressAdded(address)

    discovery = LocalDiscovery()
    try:
        discovery.start()
        services = discovery.searchServices(timeout=timeout) or []
        if not cancelled():
            for item in normalize_services(services):
                publish(item)
    finally:
        discovery.stop()


def probe_packet(message_id):
    return (f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            f'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
            f'<s:Header><a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>'
            f'<a:MessageID>{message_id}</a:MessageID><a:ReplyTo><a:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address></a:ReplyTo>'
            f'<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To></s:Header><s:Body><d:Probe/></s:Body></s:Envelope>').encode()


def parse_probe_matches(data, message_id, networks):
    if len(data) > 65535 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    related = next((element.text for element in root.iter() if element.tag.endswith("}RelatesTo")), None)
    if related != message_id:
        return []
    found = []
    for match in (element for element in root.iter() if element.tag.endswith("}ProbeMatch")):
        scopes = next((element.text or "" for element in match if element.tag.endswith("}Scopes")), "")
        types = next((element.text or "" for element in match if element.tag.endswith("}Types")), "")
        if "onvif://" not in scopes and not (ONVIF_NS.encode() in data or NVT_NS.encode() in data) and "NetworkVideoTransmitter" not in types:
            continue
        name = next((unquote(scope.split("/name/", 1)[1]) for scope in scopes.split() if "/name/" in scope), "")
        xaddrs = next((element.text or "" for element in match if element.tag.endswith("}XAddrs")), "")
        for xaddr in xaddrs.split()[:16]:
            item = onvif_candidate(xaddr, source="address_probe", name=name)
            if item and any(ipaddress.ip_address(item["host"]) in network for network in networks):
                found.append(item)
    return found


def unicast_discovery(hosts, networks, cancelled, publish):
    message_id = "urn:uuid:" + str(uuid.uuid4())
    packet = probe_packet(message_id)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        channel.bind(("0.0.0.0", 0))
        channel.setblocking(False)

        def receive():
            for _ in range(2048):
                try:
                    data, _ = channel.recvfrom(65535)
                except (BlockingIOError, OSError):
                    break
                for item in parse_probe_matches(data, message_id, networks):
                    publish(item)

        for index, host in enumerate(hosts):
            if cancelled():
                return
            try:
                channel.sendto(packet, (host, 3702))
            except OSError:
                pass
            if index % 16 == 0:
                receive()
                time.sleep(.02)  # Bound bursts on slow VPN links.
        until = time.monotonic() + 1.5
        while not cancelled() and time.monotonic() < until:
            if select.select([channel], [], [], .1)[0]:
                receive()


def probe_onvif_address(host, port):
    connection = None
    try:
        if port == 443:
            # Anonymous discovery only: never send credentials or establish trust.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            connection = http.client.HTTPSConnection(host, port, timeout=.8, context=context)
        else:
            connection = http.client.HTTPConnection(host, port, timeout=.8)
        connection.request("POST", DEVICE_PATHS[0], SOAP_DATE,
                           {"Content-Type": "application/soap+xml; charset=utf-8", "Connection": "close", "User-Agent": "KM-VMS-Discovery"})
        response = connection.getresponse()
        chunks = []
        remaining = 16384
        deadline = time.monotonic() + 1.2
        while remaining and time.monotonic() < deadline:
            part = response.read1(remaining)
            if not part:
                break
            chunks.append(part)
            remaining -= len(part)
        body = b"".join(chunks)
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            return None
        root = ET.fromstring(body)
        namespaces = ("{" + ONVIF_NS + "}", "{http://www.onvif.org/ver10/error}")
        if not any(str(element.tag).startswith(namespaces) for element in root.iter()):
            return None
        scheme = "https" if port == 443 else "http"
        item = onvif_candidate(f"{scheme}://{host}:{port}{DEVICE_PATHS[0]}", source="address_probe")
        item["auth_required"] = response.status in {401, 403} or any(element.tag.endswith("}Fault") for element in root.iter())
        return item
    except (OSError, ValueError, http.client.HTTPException, ET.ParseError):
        return None
    finally:
        if connection is not None:
            connection.close()


def probe_rtsp_address(host, port):
    try:
        with socket.create_connection((host, port), timeout=.8) as channel:
            channel.sendall(f"OPTIONS rtsp://{host}:{port}/ RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: KM-VMS-Discovery\r\n\r\n".encode())
            response = b""
            deadline = time.monotonic() + 1.2
            while b"\n" not in response and len(response) < 2048 and time.monotonic() < deadline:
                part = channel.recv(2048 - len(response))
                if not part:
                    break
                response += part
        match = re.match(rb"RTSP/(?:1\.0|2\.0) ([1-5][0-9]{2})[ \r\n]", response)
        if not match:
            return None
        item = candidate(host, "address_probe")
        item.update(protocols=["rtsp"], rtsp_ports=[port], port=port, xaddr_path="", auth_required=match[1] in {b"401", b"403"})
        return item
    except OSError:
        return None


def address_discovery(hosts, onvif_ports, rtsp_ports, protocols, cancelled, publish, progress):
    def check(host):
        for ports, probe in ((rtsp_ports if "rtsp" in protocols else [], probe_rtsp_address),
                             (onvif_ports if "onvif" in protocols else [], probe_onvif_address)):
            for port in ports:
                if cancelled():
                    return
                item = probe(host, port)
                if item:
                    publish(item)
    with ThreadPoolExecutor(max_workers=24, thread_name_prefix="camera-address-probe") as executor:
        futures = [executor.submit(check, host) for host in hosts]
        for completed, future in enumerate(as_completed(futures), 1):
            future.result()
            progress(completed)
            if cancelled():
                for pending in futures:
                    pending.cancel()
                break
