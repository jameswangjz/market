"""SSRF-safe health requests for the synchronous gateway integration.

At save time, call ``validate_upstream_url(url)`` for shape validation only.
At health time, call ``validated_http_request("GET", url, timeout=3)``.
The returned, closed httpx.Response contains at most MAX_RESPONSE_BYTES of
uncompressed body; ``upstream_body_truncated`` reports a clipped prefix.
Redirect responses are returned but never followed. Validation raises
ValueError; DNS/transport failures raise httpx.HTTPError.

UPSTREAM_ALLOWED_HOSTS is a comma-separated list of exact hostnames, and
UPSTREAM_ALLOWED_CIDRS a comma-separated list of intentional private networks.
Either can authorize private addresses, but neither overrides hard denials.
Public targets do not require an allowlist. Every DNS answer must pass.

Integration can use response.extensions["validated_upstream_ip"] for an
APISIX upstream node (e.g. route.validated_ip), rather than resolving again.
``validated_upstream_addresses`` contains every validated address as strings.
Keep the original hostname for upstream Host and TLS SNI/certificate checks.
These addresses are a snapshot of this probe, not a permanent authorization.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import os
import re
import socket
from collections.abc import Mapping
from contextlib import aclosing
from urllib.parse import unquote, urlsplit

import httpx

MIN_TIMEOUT_SECONDS = 0.1
MAX_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 4096
MAX_URL_LENGTH = 2048

_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                  "100.64.0.0/10", "fc00::/7")
)
_METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(value)
    for value in ("168.63.129.16", "100.100.100.200", "fd00:ec2::254", "fd20:ce::254")
)
_TRANSITION_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("2002::/16", "2001::/32", "64:ff9b::/96", "64:ff9b:1::/48")
)
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_BAD_ESCAPE = re.compile(r"%(?:2f|5c|3f|23|40)|%(?![0-9a-f]{2})", re.IGNORECASE)


def _hostname(value: str) -> str:
    host = value.encode("idna").decode("ascii").lower().rstrip(".")
    if len(host) > 253 or not host or not all(
        _HOST_LABEL.fullmatch(label) for label in host.split(".")
    ):
        raise ValueError("Invalid upstream hostname")
    return host


def validate_upstream_url(url: str) -> httpx.URL:
    """Validate URL shape without DNS or network I/O; return a normalized URL.

    This does not authorize an address. Always use validated_http_request for
    probes. Encoded path delimiters, controls, and nested escapes are rejected
    to avoid conflicting URL interpretations by downstream proxies.
    """
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise ValueError("Invalid upstream URL length")
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or any(
        char in url for char in "\\?#"
    ):
        raise ValueError("Upstream URL contains forbidden characters")
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            raise ValueError("Upstream must be an absolute HTTP(S) URL")
        if "@" in parts.netloc or "%" in parts.netloc:
            raise ValueError("Upstream authority cannot contain credentials or escapes")
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError("Invalid upstream port")
        if parts.netloc.endswith(":"):
            raise ValueError("Invalid upstream port")
        path = parts.path
        for _ in range(8):
            if _BAD_ESCAPE.search(path) or any(
                ord(char) <= 32 or ord(char) == 127 or char in "\\?#" for char in path
            ):
                raise ValueError("Unsafe upstream path encoding")
            decoded = unquote(path, errors="strict")
            if decoded == path:
                break
            path = decoded
        else:
            raise ValueError("Excessive upstream path encoding")
        parsed = httpx.URL(url)
        host = parsed.raw_host.decode("ascii")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            host = _hostname(host)
        else:
            if "%" in host:
                raise ValueError("Scoped upstream addresses are forbidden")
            host = str(address)
        if parts.hostname is None:
            raise ValueError("Missing upstream hostname")
        return parsed.copy_with(host=host)
    except (httpx.InvalidURL, UnicodeError) as exc:
        raise ValueError("Invalid upstream URL") from exc


def _policy() -> tuple[set[str], tuple]:
    hosts = {
        _hostname(value.strip())
        for value in os.environ.get("UPSTREAM_ALLOWED_HOSTS", "").split(",")
        if value.strip()
    }
    try:
        networks = tuple(
            ipaddress.ip_network(value.strip(), strict=True)
            for value in os.environ.get("UPSTREAM_ALLOWED_CIDRS", "").split(",")
            if value.strip()
        )
    except ValueError as exc:
        raise ValueError("Invalid UPSTREAM_ALLOWED_CIDRS configuration") from exc
    return hosts, networks


def _validate_address(address: str, host: str, hosts: set[str], networks: tuple) -> str:
    ip = ipaddress.ip_address(address)
    if "%" in address:
        raise ValueError("Scoped upstream addresses are forbidden")
    effective = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) else None
    effective = effective or ip
    if (
        effective.is_loopback or effective.is_link_local or effective.is_multicast
        or effective.is_unspecified or effective.is_reserved
        or (isinstance(effective, ipaddress.IPv6Address) and effective.is_site_local)
        or effective in _METADATA_ADDRESSES
        or any(ip in network for network in _TRANSITION_NETWORKS)
    ):
        raise ValueError("Forbidden upstream address")
    if effective.is_global:
        return str(ip)
    private = any(effective in network for network in _PRIVATE_NETWORKS)
    allowed = host in hosts or any(effective in network for network in networks)
    if not private or not allowed:
        raise ValueError("Nonpublic upstream address requires explicit authorization")
    return str(ip)


async def _resolve(url: httpx.URL) -> tuple[str, ...]:
    hosts, networks = _policy()
    host = url.raw_host.decode("ascii")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            answers = await asyncio.get_running_loop().getaddrinfo(
                host, url.port or (443 if url.scheme == "https" else 80),
                family=socket.AF_UNSPEC, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
            )
        except OSError as exc:
            raise httpx.ConnectError("Upstream DNS resolution failed") from exc
        addresses = []
        for family, _, _, _, sockaddr in answers:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                raise ValueError("Unsupported upstream DNS address family")
            addresses.append(_validate_address(sockaddr[0], host, hosts, networks))
    else:
        addresses = [_validate_address(host, host, hosts, networks)]
    if not addresses:
        raise ValueError("Upstream DNS returned no addresses")
    return tuple(dict.fromkeys(addresses))


async def _validated_http_request_async(
    method: str,
    url: str,
    timeout: float,
    follow_redirects: bool = False,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Probe using a validated, pinned IP, original Host, and original TLS SNI.

    Only GET/HEAD and finite timeouts of at least 0.1 seconds are accepted;
    larger timeouts are capped at 10 seconds. The timeout bounds DNS plus the
    entire request, including streaming. Caller
    Host is replaced, compression is disabled, TLS verification stays enabled,
    proxy/certificate environment is ignored, and retries are disabled.
    """
    if not isinstance(method, str) or method.upper() not in {"GET", "HEAD"}:
        raise ValueError("Upstream health method must be GET or HEAD")
    method = method.upper()
    if follow_redirects is not False:
        raise ValueError("Upstream redirects cannot be enabled")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (
        math.isfinite(timeout) and MIN_TIMEOUT_SECONDS <= timeout
    ):
        raise ValueError("Upstream timeout must be finite and at least 0.1 seconds")
    timeout = min(timeout, MAX_TIMEOUT_SECONDS)
    original = validate_upstream_url(url)
    request_headers = httpx.Headers(headers)
    if any(
        byte < 32 or byte == 127
        for name, value in request_headers.raw for byte in name + value
    ):
        raise ValueError("Upstream headers cannot contain controls")
    if any(name in request_headers for name in ("content-length", "transfer-encoding", "upgrade")):
        raise ValueError("Upstream health request cannot contain framing/upgrade headers")
    host = original.raw_host.decode("ascii")
    authority = f"[{host}]" if ":" in host else host
    if original.port is not None:
        authority += f":{original.port}"
    request_headers["Host"] = authority
    request_headers["Accept-Encoding"] = "identity"
    original_request = httpx.Request(method, original, headers=request_headers)
    try:
        async with asyncio.timeout(timeout):
            addresses = await _resolve(original)
            pinned = original.copy_with(host=addresses[0])
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(trust_env=False, retries=0),
                trust_env=False, follow_redirects=False, timeout=timeout,
            ) as client:
                request = client.build_request(method, pinned, headers=request_headers)
                request.extensions["sni_hostname"] = host
                response = await client.send(request, stream=True, follow_redirects=False)
                try:
                    body = b""
                    truncated = False
                    if method == "GET":
                        encoding = response.headers.get("content-encoding", "identity")
                        if encoding.lower().strip() not in {"", "identity"}:
                            raise httpx.DecodingError("Compressed upstream health response", request=original_request)
                        async with aclosing(response.aiter_raw(chunk_size=MAX_RESPONSE_BYTES + 1)) as chunks:
                            async for chunk in chunks:
                                body = chunk[:MAX_RESPONSE_BYTES]
                                truncated = len(chunk) > MAX_RESPONSE_BYTES
                                break
                    extensions = dict(response.extensions)
                    extensions.update(
                        validated_upstream_addresses=addresses,
                        validated_upstream_ip=addresses[0],
                        upstream_body_truncated=truncated,
                    )
                    response_headers = httpx.Headers(response.headers)
                    if method == "GET":
                        for name in ("content-length", "transfer-encoding"):
                            response_headers.pop(name, None)
                        response_headers["Content-Length"] = str(len(body))
                    return httpx.Response(
                        response.status_code, headers=response_headers, content=body,
                        request=original_request, extensions=extensions,
                    )
                finally:
                    await response.aclose()
    except TimeoutError as exc:
        raise httpx.TimeoutException("Upstream health deadline exceeded", request=original_request) from exc


def validated_http_request(
    method: str,
    url: str,
    timeout: float,
    follow_redirects: bool = False,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Synchronous integration callable; async callers should use asyncio.to_thread.

    A private event loop provides a deadline across DNS, connect, and streaming.
    Closing it without waiting for its resolver executor keeps a slow OS DNS
    lookup from extending the caller's deadline. No global HTTP state changes.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("Use asyncio.to_thread(validated_http_request, ...) in async code")
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            _validated_http_request_async(method, url, timeout, follow_redirects, headers)
        )
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        pending = asyncio.all_tasks(loop)
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
