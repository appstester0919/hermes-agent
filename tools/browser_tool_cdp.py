"""User-supplied CDP endpoint resolution (browser.cdp_url / real-profile), dialog-policy config and the per-task CDP supervisor lifecycle.

Split out of ``tools/browser_tool.py``. Facade-owned state is read through ``_bt`` (``tools.browser_tool``, resolved per call) — no import cycle."""

import contextlib
import os
from typing import Tuple
from urllib.parse import urlparse

from agent.proxy_bypass import loopback_request_kwargs
from tools.browser_tool_origin import origin_module as _origin


def _resolve_cdp_override(cdp_url: str) -> str:
    """Normalize a user-supplied CDP endpoint into a concrete websocket URL.

    Full ``ws://.../devtools/browser/...`` endpoints pass through; HTTP discovery roots and bare ``ws://host:port``
    resolve via ``/json/version`` → ``webSocketDebuggerUrl`` (falls back to the raw value with a warning).
    """
    _bt = _origin()
    raw = (cdp_url or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if "/devtools/browser/" in lowered:
        return raw

    discovery_url = raw
    if lowered.startswith(("ws://", "wss://")):
        if not (raw.count(":") == 2 and raw.rstrip("/").rsplit(":", 1)[-1].isdigit() and "/" not in raw.split(":", 2)[-1]):
            return raw
        discovery_url = ("http://" if lowered.startswith("ws://") else "https://") + raw.split("://", 1)[1]
    version_url = discovery_url if discovery_url.lower().endswith("/json/version") else discovery_url.rstrip("/") + "/json/version"

    san = _bt._sanitize_url_for_logs
    try:
        import requests  # lazy — shared module object, test patches still apply
        response = requests.get(version_url, timeout=10, **loopback_request_kwargs(version_url))
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        _bt.logger.warning("Failed to resolve CDP endpoint %s via %s: %s", san(raw), san(version_url), san(exc))
        return raw
    ws_url = str(payload.get("webSocketDebuggerUrl") or "").strip()
    if ws_url:
        _bt.logger.info("Resolved CDP endpoint %s -> %s", san(raw), san(ws_url))
        return ws_url
    _bt.logger.warning("CDP discovery at %s did not return webSocketDebuggerUrl; using raw endpoint", san(version_url))
    return raw


def _get_cdp_override_raw() -> str:
    """Return the *configured* CDP override without any network I/O.

    Precedence: ``BROWSER_CDP_URL`` env (live ``/browser connect``), then ``browser.cdp_url``. Is-it-configured
    gates (check_fns, ``_is_local_mode`` / ``_is_local_backend``, ``hermes doctor``) MUST use this, not
    :func:`_get_cdp_override`: its 10s HTTP discovery against a stale ``cdp_url`` would stall every startup's
    schema build with no error.
    """
    env_override = os.environ.get("BROWSER_CDP_URL", "").strip()
    return env_override or _origin()._browser_cfg("cdp_url", "", lambda v: str(v or "").strip(), "browser.cdp_url from config")


def _get_cdp_override() -> str:
    """Resolved CDP URL override, or "" (skips cloud AND local launch).

    May perform HTTP ``/json/version`` discovery — only call on paths about to *connect*; pure gates must use
    :func:`_get_cdp_override_raw`.
    """
    _bt = _origin()
    return _resolve_cdp_override(raw) if (raw := _get_cdp_override_raw()) else ""


def _cdp_url_to_cli_arg(cdp_url: str) -> str:
    """Convert a concrete websocket CDP URL into the form agent-browser's
    ``--cdp`` flag expects (``<port>`` or ``<host>:<port>``).

    agent-browser's CLI parser (Rust binary) does not understand full
    ``ws://host:port/devtools/browser/<uuid>`` endpoints — it tries to parse
    the entire string as a port number and panics with "Invalid CDP port:
    'ws://...' is not a valid number". Hermes normalizes the user-supplied
    CDP URL into a websocket URL via :func:`_resolve_cdp_override` so all
    discovery flows converge on one shape, but the CLI consumer can't cope
    with that shape. This helper is the bridge.

    Args:
        cdp_url: Either a ``ws://``/``wss://`` websocket endpoint (the common
            case after :func:`_resolve_cdp_override`), a plain ``host:port``,
            or just a port number.

    Returns:
        ``"<port>"`` when the host is ``localhost``/``127.0.0.1``/``::1``
        (agent-browser's default), or ``"<host>:<port>"`` otherwise.
    """
    raw = (cdp_url or "").strip()
    if not raw:
        raise ValueError("Empty CDP URL")

    # Already a plain port — pass through unchanged.
    if raw.isdigit():
        return raw

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    hostname = parsed.hostname  # urlparse strips brackets for IPv6

    # ws://host:port[/path] — reverse-engineer host:port for --cdp.
    if scheme in ("ws", "wss"):
        host = hostname or "localhost"
        # Default ws/wss ports if not explicitly given.
        port = parsed.port or (443 if scheme == "wss" else 80)
    elif scheme in ("http", "https"):
        host = hostname or "localhost"
        port = parsed.port or (443 if scheme == "https" else 80)
    elif hostname is not None:
        # Bare "host:port" parsed successfully (e.g. "example.com:9222" or
        # "[::1]:9222" — urlparse handles both). Keep as-is.
        return raw
    elif ":" in raw:
        # Last-resort: bare "host:port" without scheme. Pass through.
        return raw
    else:
        # Last resort: treat as port. Will fail loudly if not numeric.
        return raw

    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return str(port)
    return f"{host}:{port}"


def _get_dialog_policy_config() -> Tuple[str, float]:
    """Read ``browser.dialog_policy`` + ``browser.dialog_timeout_s``; supervisor defaults when absent/invalid."""
    _bt = _origin()
    # Deferred so browser_tool imports in minimal environments.
    from tools.browser_supervisor_dialogs import DEFAULT_DIALOG_POLICY, DEFAULT_DIALOG_TIMEOUT_S, _VALID_POLICIES
    policy, timeout_s = DEFAULT_DIALOG_POLICY, DEFAULT_DIALOG_TIMEOUT_S
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        browser_cfg = cfg.get("browser", {}) if isinstance(cfg, dict) else {}
        if not isinstance(browser_cfg, dict):
            return policy, timeout_s
        candidate = str(browser_cfg.get("dialog_policy") or DEFAULT_DIALOG_POLICY)
        if candidate in _VALID_POLICIES:
            policy = candidate
        else:
            _bt.logger.debug("Invalid browser.dialog_policy=%r; using default", candidate)
        timeout_raw = browser_cfg.get("dialog_timeout_s")
        try:
            timeout_s = float(timeout_raw) if timeout_raw is not None else DEFAULT_DIALOG_TIMEOUT_S
            if timeout_s <= 0:
                timeout_s = DEFAULT_DIALOG_TIMEOUT_S
        except (TypeError, ValueError):
            timeout_s = DEFAULT_DIALOG_TIMEOUT_S
        return policy, timeout_s
    except Exception:
        return DEFAULT_DIALOG_POLICY, DEFAULT_DIALOG_TIMEOUT_S


def _ensure_cdp_supervisor(task_id: str) -> None:
    """Start a CDP supervisor for ``task_id`` if an endpoint is reachable.

    Idempotent (``get_or_start`` skips an existing ``(task_id, cdp_url)`` and restarts on URL change), so safe on
    every navigate / ``/browser connect``. URL precedence: the CDP override, then the session's own ``cdp_url``
    (cloud providers, e.g. Browserbase). Swallows all errors — a failed attach must not break the session;
    snapshots just lack ``pending_dialogs`` / ``frame_tree``.
    """
    _bt = _origin()
    cdp_url = _get_cdp_override()
    if not cdp_url:
        with _bt._cleanup_lock:
            session_info = _bt._active_sessions.get(task_id, {})
        maybe = str(session_info.get("cdp_url") or "")
        if maybe:
            cdp_url = _resolve_cdp_override(maybe)
    if not cdp_url:
        return
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore[import-not-found]
        policy, timeout_s = _get_dialog_policy_config()
        SUPERVISOR_REGISTRY.get_or_start(task_id=task_id, cdp_url=cdp_url, dialog_policy=policy, dialog_timeout_s=timeout_s)
    except Exception as exc:
        _bt.logger.debug("CDP supervisor attach for task=%s failed (non-fatal): %s", task_id, exc)


def _stop_cdp_supervisor(task_id: str) -> None:
    """Stop the CDP supervisor for ``task_id`` if one exists. No-op otherwise."""
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore[import-not-found]
        SUPERVISOR_REGISTRY.stop(task_id)
    except Exception as exc:
        _origin().logger.debug("CDP supervisor stop for task=%s failed (non-fatal): %s", task_id, exc)
