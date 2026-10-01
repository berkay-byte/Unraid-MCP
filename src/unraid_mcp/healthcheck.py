"""Container liveness probe: ``python -m unraid_mcp.healthcheck``.

Reads only the bind settings (``UNRAID_MCP_HOST``/``PORT``/``TLS_CERT``/
``TLS_KEY``) straight from the environment, so it needs no Unraid API key or
bearer token and never calls the Unraid API. Probes the loopback ``/health``
endpoint over https when the server serves TLS directly, skipping certificate
verification (loopback only; self-signed certs are common). Exits 0 if healthy.
"""

from __future__ import annotations

import os
import ssl
import sys
import urllib.request
from collections.abc import Mapping

_WILDCARD_HOSTS = ("", "0.0.0.0", "::")  # noqa: S104


def health_url(env: Mapping[str, str]) -> str:
    """Loopback ``/health`` URL matching how the server is bound."""
    host = (env.get("UNRAID_MCP_HOST") or "").strip()
    if host in _WILDCARD_HOSTS:
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):  # bare IPv6 literal
        host = f"[{host}]"
    port = (env.get("UNRAID_MCP_PORT") or "6750").strip()
    tls = bool(env.get("UNRAID_MCP_TLS_CERT") and env.get("UNRAID_MCP_TLS_KEY"))
    return f"{'https' if tls else 'http'}://{host}:{port}/health"


def probe(env: Mapping[str, str] | None = None, timeout: float = 3.0) -> int:
    """Return 0 if ``/health`` answers 200, else 1."""
    url = health_url(os.environ if env is None else env)
    context = None
    if url.startswith("https://"):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=context) as resp:  # noqa: S310
            return 0 if resp.status == 200 else 1
    except Exception:
        return 1


def main() -> int:
    return probe()


if __name__ == "__main__":
    sys.exit(main())
