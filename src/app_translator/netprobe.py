"""Timing breakdown for one outbound HTTPS request, measured from inside the container.

Exists because a fetch that takes 80 seconds and one that takes 80 milliseconds look
identical from the outside, and the difference is usually DNS, or a connect retrying an
address family that does not work here.
"""

import socket
import ssl
import time
from urllib.parse import urlparse

import attr

CONNECT_TIMEOUT_SECONDS = 10.0


@attr.s(auto_attribs=True, frozen=True)
class AddressResult:
    family: str
    address: str
    seconds: float
    error: str = ""


@attr.s(auto_attribs=True, frozen=True)
class ProbeResult:
    host: str
    port: int
    dns_seconds: float
    dns_error: str
    addresses: tuple[AddressResult, ...]
    tls_seconds: float = 0.0
    tls_error: str = ""


def _family_name(family: int) -> str:
    return {socket.AF_INET: "ipv4", socket.AF_INET6: "ipv6"}.get(family, str(family))


def probe(url: str) -> ProbeResult:
    """Resolve a URL's host, then time a TCP connect to every address it returns."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    started = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        dns_error = ""
    except OSError as exc:
        return ProbeResult(
            host=host,
            port=port,
            dns_seconds=time.monotonic() - started,
            dns_error=f"{type(exc).__name__}: {exc}",
            addresses=(),
        )
    dns_seconds = time.monotonic() - started

    results: list[AddressResult] = []
    tls_seconds = 0.0
    tls_error = ""
    for family, _type, _proto, _canon, sockaddr in infos:
        address = str(sockaddr[0])
        connect_started = time.monotonic()
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT_SECONDS)
        try:
            sock.connect(sockaddr)
            elapsed = time.monotonic() - connect_started
            results.append(AddressResult(family=_family_name(family), address=address, seconds=elapsed))
            if not tls_seconds and parsed.scheme == "https":
                tls_started = time.monotonic()
                try:
                    context = ssl.create_default_context()
                    with context.wrap_socket(sock, server_hostname=host):
                        tls_seconds = time.monotonic() - tls_started
                except (ssl.SSLError, OSError) as exc:
                    tls_error = f"{type(exc).__name__}: {exc}"
        except OSError as exc:
            results.append(
                AddressResult(
                    family=_family_name(family),
                    address=address,
                    seconds=time.monotonic() - connect_started,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
        finally:
            sock.close()

    return ProbeResult(
        host=host,
        port=port,
        dns_seconds=dns_seconds,
        dns_error=dns_error,
        addresses=tuple(results),
        tls_seconds=tls_seconds,
        tls_error=tls_error,
    )
