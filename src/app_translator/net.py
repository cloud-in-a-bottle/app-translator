"""One HTTP client configuration, shared by everything that reaches the network.

Two deliberate choices, both learned the hard way:

* **Bind IPv4.** httpx's synchronous client tries resolved addresses one at a time,
  waiting out the full TCP timeout on each. Inside a rootless container IPv6 is
  usually routed nowhere and connects hang rather than failing, so a host with four
  AAAA records costs four timeouts before IPv4 is tried at all — that was 80 seconds
  per fetch of a file from raw.githubusercontent.com. Binding a local IPv4 address
  restricts connections to IPv4. Every registry and git forge we talk to has IPv4.
* **A short connect timeout**, separate from the read timeout, so an unreachable host
  fails in seconds instead of stalling a page load.
"""

import httpx

CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 20.0


def build_client(*, read_timeout: float = READ_TIMEOUT_SECONDS) -> httpx.Client:
    timeout = httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT_SECONDS)
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=1)
    return httpx.Client(timeout=timeout, follow_redirects=True, transport=transport)
