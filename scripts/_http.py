"""Tiny shared HTTP helper for the data-fetch scripts.

stdlib-only (urllib), proxy-aware (honors HTTPS_PROXY), and it distinguishes an
egress-policy refusal from a real error so callers can degrade quietly when the
network is locked down (the routine sandbox blocks most domains; GitHub's
runners don't). Same posture as fetch_thumbnails.py.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request


class EgressBlocked(Exception):
    """The network egress policy refused the connection (not a real failure)."""


def ssl_context() -> ssl.SSLContext:
    """Default TLS, honoring a custom CA bundle if the runtime sets one (the
    agent proxy MITMs TLS with its own CA)."""
    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if not ca and os.path.exists("/root/.ccr/ca-bundle.crt"):
        ca = "/root/.ccr/ca-bundle.crt"
    try:
        return ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
    except (ssl.SSLError, OSError):
        return ssl.create_default_context()


def _is_proxy_block(exc) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            via = exc.headers.get("Via", "") or ""
        except Exception:
            via = ""
        return exc.code in (403, 407) and ("agentproxy" in via.lower() or exc.code == 407)
    reason = str(getattr(exc, "reason", exc)).lower()
    return any(s in reason for s in ("tunnel connection failed", "connection refused",
                                     "denied", "forbidden", "proxy", "tunnel"))


def get(url: str, *, timeout: int = 20, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "claude-rss/1.0",
                                               "Accept": "application/json, */*",
                                               **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
            return resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        if _is_proxy_block(exc):
            raise EgressBlocked(f"{url}: {exc}") from exc
        raise


def get_json(url: str, *, timeout: int = 20, headers: dict | None = None):
    return json.loads(get(url, timeout=timeout, headers=headers).decode("utf-8"))
