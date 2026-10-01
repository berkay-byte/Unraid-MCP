"""Container healthcheck probe: URL selection and exit codes."""

from __future__ import annotations

import ssl
import urllib.error

import pytest

from unraid_mcp import healthcheck
from unraid_mcp.healthcheck import health_url, probe


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "http://127.0.0.1:6750/health"),
        ({"UNRAID_MCP_HOST": "0.0.0.0"}, "http://127.0.0.1:6750/health"),
        ({"UNRAID_MCP_HOST": "::", "UNRAID_MCP_PORT": "9000"}, "http://127.0.0.1:9000/health"),
        ({"UNRAID_MCP_HOST": "::1"}, "http://[::1]:6750/health"),
        ({"UNRAID_MCP_HOST": "10.0.0.5"}, "http://10.0.0.5:6750/health"),
        (
            {"UNRAID_MCP_HOST": "0.0.0.0", "UNRAID_MCP_TLS_CERT": "c", "UNRAID_MCP_TLS_KEY": "k"},
            "https://127.0.0.1:6750/health",
        ),
        ({"UNRAID_MCP_TLS_CERT": "c"}, "http://127.0.0.1:6750/health"),
    ],
)
def test_health_url(env, expected):
    assert health_url(env) == expected


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_probe_ok_http(monkeypatch):
    seen = {}

    def fake(url, timeout, context):
        seen.update(url=url, context=context)
        return _Resp(200)

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", fake)
    assert probe({}) == 0
    assert seen == {"url": "http://127.0.0.1:6750/health", "context": None}


def test_probe_tls_skips_verification(monkeypatch):
    seen = {}

    def fake(url, timeout, context):
        seen.update(url=url, context=context)
        return _Resp(200)

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", fake)
    assert probe({"UNRAID_MCP_TLS_CERT": "c", "UNRAID_MCP_TLS_KEY": "k"}) == 0
    assert seen["url"].startswith("https://")
    assert seen["context"].verify_mode == ssl.CERT_NONE
    assert seen["context"].check_hostname is False


def test_probe_non_200_is_unhealthy(monkeypatch):
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", lambda *a, **k: _Resp(503))
    assert probe({}) == 1


def test_probe_error_is_unhealthy(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", boom)
    assert probe({}) == 1
