from contextlib import contextmanager
import ipaddress
import re
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from . import __version__


class RemoteError(Exception):
    pass


def normalize_url(value):
    value = str(value).strip().rstrip(".,;|>\"'")
    if not re.match(r"^https?://", value, re.I):
        raise ValueError("Use a complete http:// or https:// address.")
    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        raise ValueError("Addresses containing passwords are not supported.")
    if not parsed.hostname or any(c.isspace() for c in value) or "\\" in value:
        raise ValueError("Invalid server address.")
    try:
        port = parsed.port
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid hostname or port.") from exc
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip and (ip.is_link_local or ip.is_multicast or ip.is_unspecified):
        raise ValueError("Link-local, multicast, and unspecified addresses cannot be used.")
    authority = f"[{host}]" if ":" in host else host
    if port and (parsed.scheme.lower(), port) not in (("http", 80), ("https", 443)):
        authority += f":{port}"
    return urlunsplit((parsed.scheme.lower(), authority, parsed.path.rstrip("/"), parsed.query, ""))


def origin(value):
    url = urlsplit(value)
    return url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80)


def source_link(base, value):
    if not value:
        return ""
    url = normalize_url(urljoin(base.rstrip("/") + "/", str(value)))
    original, target = origin(base), origin(url)
    upgrade = original[0] == "http" and target[0] == "https" and original[1] == target[1] and original[2] == 80 and target[2] == 443
    if target != original and not upgrade:
        raise ValueError("The source pointed to a different server. Import that server separately.")
    return url


def parse_servers(text):
    if len(text) > 2_000_000:
        raise ValueError("The server list is too large (maximum 2 MB).")
    raw = re.findall(r"https?://[^\s|<>\"'\]\[{}]+", text.replace("\\/", "/"), re.I)
    urls, invalid, seen = [], [], set()
    for candidate in raw:
        try:
            url = normalize_url(candidate)
        except ValueError as exc:
            invalid.append({"url": candidate[:300], "error": str(exc)})
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    if len(urls) > 5000:
        raise ValueError("Import at most 5,000 servers at a time.")
    return {"urls": urls, "invalid": invalid, "duplicates": len(raw) - len(urls) - len(invalid)}


class Network:
    """Serialize requests per origin, including streamed downloads and cover requests."""

    def __init__(self, config):
        self.config = config
        self._guard = threading.Lock()
        self._hosts = {}
        self._local = threading.local()

    def _session(self):
        if not hasattr(self._local, "session"):
            session = requests.Session()
            session.trust_env = False
            session.headers["User-Agent"] = f"OpenShelf/{__version__} (personal catalog client)"
            self._local.session = session
        return self._local.session

    @contextmanager
    def request(self, url, *, base, headers=None, params=None):
        try:
            current = source_link(base, url)
        except ValueError as exc:
            raise RemoteError(str(exc)) from exc
        key = origin(base)
        with self._guard:
            gate = self._hosts.setdefault(key, {"lock": threading.Lock(), "next": 0})
        with gate["lock"]:
            time.sleep(max(0, gate["next"] - time.monotonic()))
            response = None
            try:
                for _ in range(5):
                    for address in socket.getaddrinfo(urlsplit(current).hostname, urlsplit(current).port or 80, type=socket.SOCK_STREAM):
                        ip = ipaddress.ip_address(address[4][0])
                        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                            raise RemoteError("Server resolves to a disallowed network address.")
                    response = self._session().get(current, params=params, headers=headers,
                        timeout=(self.config.connect_timeout, self.config.read_timeout),
                        stream=True, allow_redirects=False)
                    if response.status_code not in (301, 302, 303, 307, 308):
                        break
                    target = normalize_url(urljoin(response.url, response.headers.get("Location", "")))
                    response.close()
                    if origin(current)[0] == "https" and origin(target)[0] != "https":
                        raise RemoteError("The server tried to redirect a secure connection to HTTP.")
                    try:
                        source_link(base, target)
                    except ValueError as exc:
                        raise RemoteError("Redirected to another server; import its address explicitly.") from exc
                    current, params = target, None
                else:
                    raise RemoteError("Too many redirects.")
                if response.status_code in (401, 403):
                    raise RemoteError("This server requires access permission or authentication.")
                if response.status_code >= 400:
                    raise RemoteError(f"Server returned HTTP {response.status_code}.")
                yield response
            except (requests.RequestException, OSError) as exc:
                if isinstance(exc, requests.exceptions.SSLError):
                    raise RemoteError("The server's TLS certificate could not be verified.") from exc
                raise RemoteError(f"Cannot reach this server ({type(exc).__name__}).") from exc
            finally:
                if response is not None:
                    response.close()
                gate["next"] = time.monotonic() + self.config.request_delay

    def read(self, url, *, base, params=None, limit=16_000_000):
        with self.request(url, base=base, params=params) as response:
            data = bytearray()
            for part in response.iter_content(65536):
                data.extend(part)
                if len(data) > limit:
                    raise RemoteError("Server response exceeds the metadata size limit.")
            return bytes(data), response.url

    def json(self, url, *, base, params=None):
        import json
        body, effective = self.read(url, base=base, params=params)
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise RemoteError("This address did not return a Calibre catalog.") from exc
        if not isinstance(value, dict):
            raise RemoteError("Unsupported catalog response.")
        return value, effective
