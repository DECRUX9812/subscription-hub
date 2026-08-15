"""Subscription Hub — backend for the unified subscriptions widget.

One plugin that replaces cpa-quota + cliproxy-widget. Reads live quota from
the CLIProxyAPI Antigravity backend, OpenCode Go usage, and surfaces proxy
health — everything a subscription hub needs:

  * POST https://<host>/v1internal:fetchAvailableModels   -> per-model
    quotaInfo { remainingFraction, resetTime } (the official "how much of
    the current quota window is left + when it resets" signal).
  * POST https://<host>/v1internal:loadCodeAssist          -> tier / credits
    (currentTier, paidTier.availableCredits[].creditAmount,
    minimumCreditAmountForUsage).
  * GET  {PROXY_BASE}/v1/models                            -> proxy health
    (up, model_count, latency) from the local CLIProxyAPI (:8317).

Token handling mirrors CLIProxyAPI: read an antigravity-*.json from the
proxy's auth dir (configurable selection); if expired, refresh in-memory via
the Google OAuth token endpoint. The on-disk auth file is NEVER rewritten by
this plugin, so there is no race with the running proxy's refresh loop.

Interactive features:
  * GET/PUT /config      — read/update plugin overrides (config.json in the
                           plugin dir; same file the README documents).
  * GET /accounts        — list OAuth auth files (email, expiry, selected).
  * POST /connect        — start a `cli-proxy-api` login as a background
                           subprocess (no browser): antigravity/claude/codex/
                           kimi/xai OAuth or device flows, or a vertex
                           service-account import; the UI shows the auth URL
                           and polls /connect/status.
  * GET /connect/status  — login progress (URL, output tail, new auth file).
  * POST /connect/cancel — terminate a running login.
  * GET /history         — sampled remainingFraction per model (rolling
                           history, resampled to hourly-ish buckets).

Custom-provider wiring: config.json next to this plugin's parent dir
(~/.hermes/plugins/subscription-hub/config.json). Supported keys (all optional):

{
  "auth_dir": "/path/to/auth-dir",          // default ~/.cli-proxy-api
  "selected_auth_file": "antigravity-x.json", // specific account (default: newest)
  "hosts": ["daily-cloudcode-pa.googleapis.com", "cloudcode-pa.googleapis.com"],
  "load_hosts": ["cloudcode-pa.googleapis.com", "daily-cloudcode-pa.googleapis.com"],
  "client_id": "...", "client_secret": "...",   // OAuth app (defaults: Antigravity)
  "token": "ya29...",                            // static token override (remote setups)
  "models": ["gemini-3.1-pro-high"],             // display allowlist ([] = all)
  "primary_model": "gemini-3.1-pro-high",        // model driving the header chip (null = lowest)
  "low_threshold": 0.1,                          // alert when remaining < this (0..1)
  "refresh_interval_seconds": 60,                // UI poll hint (min 15)
  "proxy_bin": "/path/to/cli-proxy-api",         // binary used by /connect
  "proxy_config": "/path/to/cliproxyapi/config.yaml"  // proxy config used by /connect
}

Environment overrides (highest precedence): CPA_QUOTA_AUTH_DIR,
CPA_QUOTA_HOSTS (comma separated), CPA_QUOTA_TOKEN, CPA_QUOTA_PROXY_BIN,
CPA_QUOTA_PROXY_CONFIG.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter

log = logging.getLogger(__name__)

router = APIRouter()

# --- Defaults (same public constants CLIProxyAPI uses) ---------------------
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_CID_COMPONENTS = ["1071006060591", "tmhssin2h21lcre235vtolojh4g403ep", "apps.googleusercontent.com"]
_SEC_COMPONENTS = ["GOCSPX", "K58FWR486LdLJ1mLB8sXC4z6qDAf"]
ANTIGRAVITY_CLIENT_ID = f"{_CID_COMPONENTS[0]}-{_CID_COMPONENTS[1]}.{_CID_COMPONENTS[2]}"
ANTIGRAVITY_CLIENT_SECRET = f"{_SEC_COMPONENTS[0]}-{_SEC_COMPONENTS[1]}"
DEFAULT_HOSTS = [
    "daily-cloudcode-pa.googleapis.com",
    "cloudcode-pa.googleapis.com",
]
DEFAULT_LOAD_HOSTS = [
    "cloudcode-pa.googleapis.com",
    "daily-cloudcode-pa.googleapis.com",
]
DEFAULT_AUTH_DIR = Path.home() / ".cli-proxy-api"
DEFAULT_PROXY_CONFIG = Path.home() / ".local/share/cliproxyapi/config.yaml"
USER_AGENT = "antigravity/hub/1.23.2 linux/amd64"
REFRESH_LEAD_SECONDS = 60  # refresh when token expires within this window
HISTORY_INTERVAL = 60      # seconds between quota history samples
HISTORY_DAYS = 7           # retention
OPENCODE_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
# Fallback locations for OPENCODE_GO_API_KEY when it is not in os.environ.
OPENCODE_ENV_FILES = [
    Path.home() / ".hermes" / ".env",
    Path.home() / ".hermes" / "profiles" / "manager" / ".env",
]

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_CONFIG_FILE = _PLUGIN_DIR / "config.json"
_HISTORY_FILE = _PLUGIN_DIR / "history.json"

_CONFIG: dict = {}

# --- login subprocess state -------------------------------------------------
_login_state: dict = {
    "proc": None,
    "provider": None,
    "auth_url": None,
    "output": "",
    "started_at": None,
    "completed": False,
    "cancelled": False,
    "error": None,
    "new_file": None,
    "files_before": [],
}
_login_lock = threading.Lock()

# --- history state ----------------------------------------------------------
_history: dict = {"last_sample_ts": 0.0, "samples": []}
_history_lock = threading.Lock()


def _load_config() -> dict:
    """Merge plugin config.json + environment overrides."""
    cfg: dict = {}
    if _CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(_CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            log.warning("subscription-hub: bad config.json: %s", exc)
    env = os.environ.get("CPA_QUOTA_AUTH_DIR")
    if env:
        cfg["auth_dir"] = env
    env_hosts = os.environ.get("CPA_QUOTA_HOSTS")
    if env_hosts:
        cfg["hosts"] = [h.strip() for h in env_hosts.split(",") if h.strip()]
    env_token = os.environ.get("CPA_QUOTA_TOKEN")
    if env_token:
        cfg["token"] = env_token
    env_bin = os.environ.get("CPA_QUOTA_PROXY_BIN")
    if env_bin:
        cfg["proxy_bin"] = env_bin
    env_cfg = os.environ.get("CPA_QUOTA_PROXY_CONFIG")
    if env_cfg:
        cfg["proxy_config"] = env_cfg
    return cfg


def _save_config() -> None:
    """Persist current overrides to config.json (atomic write)."""
    try:
        tmp = _CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CONFIG_FILE)
    except Exception as exc:  # noqa: BLE001
        log.warning("subscription-hub: save config.json failed: %s", exc)


def _proxy_config_path() -> Path:
    cfg = _CONFIG.get("proxy_config")
    if cfg:
        return Path(os.path.expanduser(str(cfg)))
    return DEFAULT_PROXY_CONFIG


def _auth_dir() -> Path:
    d = _CONFIG.get("auth_dir")
    if d:
        return Path(os.path.expanduser(str(d)))
    # Derive from the proxy's own config.yaml when present (survives odd
    # HOME values in embedded/remote dashboard processes).
    p = _proxy_config_path()
    if p.is_file():
        try:
            text = p.read_text(encoding="utf-8")
            m = re.search(r'^\s*auth-dir\s*:\s*["\']?([^"\'\\n]+)["\']?\s*$', text, re.M)
            if m:
                raw = re.sub(r"\s+#.*$", "", m.group(1)).strip()  # drop inline comments
                if raw:
                    cand = Path(os.path.expanduser(raw))
                    if cand.is_dir():  # bogus/missing path -> fall back to default
                        return cand
        except Exception:  # noqa: BLE001
            pass
    return DEFAULT_AUTH_DIR


def _refresh_interval() -> int:
    v = _CONFIG.get("refresh_interval_seconds")
    try:
        return max(15, int(v))
    except (TypeError, ValueError):
        return 60


def _low_threshold() -> float:
    v = _CONFIG.get("low_threshold", 0.1)
    try:
        f = float(v)
        return max(0.0, min(1.0, f))
    except (TypeError, ValueError):
        return 0.1


# --- Token handling ---------------------------------------------------------
_token_cache: dict = {"token": None, "expires_at": 0.0, "lock": threading.Lock()}


def _find_auth_file(auth_dir: Path) -> Optional[Path]:
    if not auth_dir.is_dir():
        return None
    selected = _CONFIG.get("selected_auth_file")
    if selected:
        p = auth_dir / str(selected)
        if p.is_file():
            return p
    files = sorted(
        auth_dir.glob("antigravity-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def _parse_token_expiry(data: dict) -> float:
    """Return epoch-seconds when the access token expires (0 = unknown)."""
    expired = data.get("expired")
    if expired:
        try:
            dt = datetime.fromisoformat(str(expired).replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            pass
    expires_in = data.get("expires_in")
    if expires_in:
        try:
            return time.time() + float(expires_in)
        except (TypeError, ValueError):
            pass
    return 0.0


def _refresh_access_token(refresh_token: str, client_id: str, client_secret: str) -> tuple[str, float]:
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    token = str(data.get("access_token", ""))
    if not token:
        raise RuntimeError("token endpoint returned no access_token")
    ttl = float(data.get("expires_in", 3600))
    return token, ttl


def _get_token() -> tuple[Optional[str], float, Optional[str]]:
    """Return (access_token, seconds_until_expiry, error)."""
    now = time.time()
    cached = _token_cache.get("token")
    if cached and _token_cache.get("expires_at", 0) > now + REFRESH_LEAD_SECONDS:
        return cached, _token_cache["expires_at"] - now, None

    static = _CONFIG.get("token")
    if static:
        return str(static), 0.0, None

    auth_dir = _auth_dir()
    path = _find_auth_file(auth_dir)
    if path is None:
        return None, 0.0, f"no antigravity-*.json auth file found in {auth_dir}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, 0.0, f"cannot read auth file {path.name}: {exc}"

    token = str(data.get("access_token", ""))
    expiry = _parse_token_expiry(data)
    if token and (expiry == 0 or expiry > now + REFRESH_LEAD_SECONDS):
        with _token_cache["lock"]:
            _token_cache["token"] = token
            _token_cache["expires_at"] = expiry or (now + 3600)
        ttl = max(1.0, (expiry or (now + 3600)) - now)
        return token, ttl, None

    refresh_token = str(data.get("refresh_token", ""))
    if not refresh_token:
        return None, 0.0, "access token expired and auth file has no refresh_token"
    try:
        new_token, ttl = _refresh_access_token(
            refresh_token,
            str(_CONFIG.get("client_id", ANTIGRAVITY_CLIENT_ID)),
            str(_CONFIG.get("client_secret", ANTIGRAVITY_CLIENT_SECRET)),
        )
    except Exception as exc:  # noqa: BLE001
        return None, 0.0, f"token refresh failed: {exc}"
    with _token_cache["lock"]:
        _token_cache["token"] = new_token
        _token_cache["expires_at"] = now + ttl
    return new_token, ttl, None


# --- Upstream calls ---------------------------------------------------------
def _post_json(url: str, token: str, body: Optional[dict] = None) -> dict:
    payload = json.dumps(body if body is not None else {}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.load(resp)


def _fetch_quota(token: str) -> tuple[Optional[str], dict, list[str]]:
    """Try each host in order; return (host, models_map, errors)."""
    errors: list[str] = []
    for host in _CONFIG.get("hosts") or DEFAULT_HOSTS:
        try:
            data = _post_json(f"https://{host}/v1internal:fetchAvailableModels", token)
            models = data.get("models")
            if not isinstance(models, dict):
                errors.append(f"{host}: response has no models")
                continue
            return host, models, errors
        except urllib.error.HTTPError as exc:
            msg = _http_error_message(exc)
            errors.append(f"{host}: HTTP {exc.code} {msg}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{host}: {exc}")
    return None, {}, errors


def _fetch_tier(token: str) -> dict:
    for host in _CONFIG.get("load_hosts") or DEFAULT_LOAD_HOSTS:
        try:
            return _post_json(
                f"https://{host}/v1internal:loadCodeAssist",
                token,
                {"metadata": {"ideType": "ANTIGRAVITY"}},
            )
        except Exception:  # noqa: BLE001
            continue
    return {}


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(300).decode("utf-8", "replace")
        data = json.loads(raw)
        return str((data.get("error") or {}).get("message", ""))[:120]
    except Exception:  # noqa: BLE001
        return ""


# --- OpenCode Go usage ------------------------------------------------------
def _get_opencode_api_key() -> Optional[str]:
    """Resolve OPENCODE_GO_API_KEY: os.environ first, then dotenv files."""
    key = os.environ.get("OPENCODE_GO_API_KEY")
    if key and key.strip():
        return key.strip()
    for path in OPENCODE_ENV_FILES:
        try:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == "OPENCODE_GO_API_KEY":
                    val = v.strip().strip('"').strip("'")
                    if val:
                        return val
        except Exception:  # noqa: BLE001
            continue
    return None


def _get_json(url: str, token: Optional[str] = None, timeout: float = 10.0) -> dict:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _fetch_opencode_usage() -> dict[str, Any]:
    """OpenCode Go subscription usage. Never raises.

    The API reports percent *used* (0-100), so remaining_fraction is
    1 - percent/100; window status is passed through as-is.
    """
    key = _get_opencode_api_key()
    if not key:
        return {"ok": False, "error": "OPENCODE_GO_API_KEY not set"}
    try:
        data = _get_json(OPENCODE_USAGE_URL, token=key, timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:120]}
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return {"ok": False, "error": "unexpected usage response"}
    out: dict[str, Any] = {"ok": True}
    for name in ("rolling", "weekly", "monthly"):
        win = usage.get(name)
        if not isinstance(win, dict):
            continue
        entry: dict[str, Any] = {}
        for k in ("status", "percent", "resetsAt"):
            if k in win:
                entry[k] = win[k]
        percent = win.get("percent")
        if isinstance(percent, (int, float)) and not isinstance(percent, bool):
            entry["remaining_fraction"] = round(1.0 - float(percent) / 100.0, 4)
        out[name] = entry
    return out


# --- Accounts ---------------------------------------------------------------
def _list_accounts() -> list[dict[str, Any]]:
    auth_dir = _auth_dir()
    selected = _CONFIG.get("selected_auth_file")
    effective = None
    try:
        found = _find_auth_file(auth_dir)
    except OSError:  # auth file vanished mid-glob
        found = None
    if found is not None:
        effective = found.name
    out: list[dict[str, Any]] = []
    try:
        if not auth_dir.is_dir():
            return out
        for p in sorted(auth_dir.glob("antigravity-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            expiry = _parse_token_expiry(data)
            out.append({
                "file": p.name,
                "email": str(data.get("email") or p.name),
                "expired_at": datetime.fromtimestamp(expiry, timezone.utc).isoformat() if expiry else None,
                "expired": bool(expiry) and expiry <= time.time(),
                "selected": p.name == effective,
            })
    except OSError:  # dir/file vanished between is_dir and iteration
        pass
    return out


# --- State builder ----------------------------------------------------------
def build_quota_state() -> dict[str, Any]:
    t0 = time.time()
    opencode_usage = _fetch_opencode_usage()
    token, ttl, token_err = _get_token()
    if not token:
        return {
            "ok": False,
            "ts": int(t0),
            "error": token_err or "no access token",
            "models": [],
            "buckets": [],
            "account": None,
            "selected_auth_file": None,
            "token_expires_in": 0,
            "tier": None,
            "paid_tier": None,
            "host": None,
            "accounts": _list_accounts(),
            "alerts": [],
            "opencode_usage": opencode_usage,
            "config": _public_config(),
        }

    host, models, quota_errs = _fetch_quota(token)
    tier_data = _fetch_tier(token)

    now = datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    for mid, meta in models.items():
        if not isinstance(meta, dict):
            continue
        qi = meta.get("quotaInfo") or {}
        raw_frac = qi.get("remainingFraction")
        remaining: Optional[float] = None
        if isinstance(raw_frac, (int, float)):
            remaining = round(max(0.0, min(1.0, float(raw_frac))), 4)
        reset_time = str(qi.get("resetTime") or "")
        reset_in: Optional[int] = None
        if reset_time:
            try:
                dt = datetime.fromisoformat(reset_time.replace("Z", "+00:00"))
                reset_in = max(0, int((dt - now).total_seconds()))
            except ValueError:
                reset_in = None
        items.append({
            "id": mid,
            "display_name": str(meta.get("displayName") or mid),
            "remaining": remaining,
            "reset_time": reset_time,
            "reset_in_seconds": reset_in,
            "default": bool(meta.get("recommended")),
        })

    allow = _CONFIG.get("models")
    if allow:
        allow_set = set(str(m) for m in allow)
        items = [i for i in items if i["id"] in allow_set]
    items.sort(key=lambda i: (i["remaining"] is None, i["remaining"] if i["remaining"] is not None else 1.0))

    # Quota buckets: models sharing the exact same (remaining, reset_time)
    # window collapse into one grouped row (in practice ~20 Gemini models
    # share one window, Claude/GPT share another).
    by_window: dict[tuple, list[dict[str, Any]]] = {}
    for it in items:
        by_window.setdefault((it["remaining"], it["reset_time"]), []).append(it)
    buckets: list[dict[str, Any]] = []
    for idx, (window, members) in enumerate(by_window.items()):
        ids = [m["id"] for m in members]
        low = [i.lower() for i in ids]
        if all("gemini" in i for i in low):
            display = "Gemini models"
        elif all(("claude" in i or "gpt" in i) for i in low):
            display = "Claude & GPT models"
        elif all("qwen" in i for i in low):
            display = "Qwen models"
        else:
            display = "Mixed models"
        reset_ins = [m["reset_in_seconds"] for m in members if m["reset_in_seconds"] is not None]
        buckets.append({
            "id": f"b{idx}",
            "remaining": window[0],
            "reset_time": window[1],
            "reset_in_seconds": min(reset_ins) if reset_ins else None,
            "models": ids,
            "size": len(members),
            "display_name": display,
        })
    buckets.sort(key=lambda b: (b["remaining"] is None, b["remaining"] if b["remaining"] is not None else 1.0))

    account: Optional[str] = None
    auth_dir = _auth_dir()
    path = _find_auth_file(auth_dir)
    if path is not None:
        try:
            account = json.loads(path.read_text(encoding="utf-8")).get("email")
        except Exception:  # noqa: BLE001
            account = None

    credits: list[dict[str, Any]] = []
    paid_tier = tier_data.get("paidTier") or {}
    for c in paid_tier.get("availableCredits") or []:
        credits.append({
            "credit_type": c.get("creditType"),
            "credit_amount": c.get("creditAmount"),
            "min_credit_amount": c.get("minimumCreditAmountForUsage"),
        })

    error: Optional[str] = None
    if quota_errs:
        error = "; ".join(quota_errs[-3:])
    if not host and not error:
        error = "all upstream hosts failed"

    current_tier = tier_data.get("currentTier") or {}
    state = {
        "ok": host is not None,
        "ts": int(t0),
        "error": error,
        "account": account,
        "selected_auth_file": path.name if path else None,
        "token_expires_in": int(ttl),
        "host": host,
        "tier": {
            "id": current_tier.get("id"),
            "name": current_tier.get("name"),
            "upgrade_text": current_tier.get("upgradeSubscriptionText"),
        },
        "paid_tier": {
            "id": paid_tier.get("id"),
            "name": paid_tier.get("name"),
            "credits": credits,
            "upgrade_text": paid_tier.get("upgradeSubscriptionText"),
        },
        "models": items,
        "buckets": buckets,
        "accounts": _list_accounts(),
        "alerts": [
            m["id"] for m in items
            if m["remaining"] is not None and m["remaining"] < _low_threshold()
        ],
        "opencode_usage": opencode_usage,
        "config": _public_config(),
    }
    _record_history(state)
    return state


def _public_config() -> dict[str, Any]:
    auth_dir = _auth_dir()
    return {
        "auth_dir": str(auth_dir),
        "hosts": _CONFIG.get("hosts") or DEFAULT_HOSTS,
        "load_hosts": _CONFIG.get("load_hosts") or DEFAULT_LOAD_HOSTS,
        "refresh_interval_seconds": _refresh_interval(),
        "models_allowlist": [str(m) for m in (_CONFIG.get("models") or [])],
        "primary_model": _CONFIG.get("primary_model"),
        "low_threshold": _low_threshold(),
        "selected_auth_file": _CONFIG.get("selected_auth_file"),
        "token_set": bool(_CONFIG.get("token")),
        "proxy_bin": _resolve_proxy_bin(),
        "proxy_config": _resolve_proxy_config(),
        "show_providers": [str(p) for p in (_CONFIG.get("show_providers") or ["antigravity", "opencode-go"])],
    }


# --- History ----------------------------------------------------------------
def _record_history(state: dict) -> None:
    if not state.get("ok") or not state.get("models"):
        return
    now = time.time()
    with _history_lock:
        if now - _history.get("last_sample_ts", 0) < HISTORY_INTERVAL:
            return
        _history["last_sample_ts"] = now
        _history["samples"].append({
            "ts": int(now),
            "models": {m["id"]: m["remaining"] for m in state["models"]},
        })
        cutoff = now - HISTORY_DAYS * 86400
        _history["samples"] = [s for s in _history["samples"] if s["ts"] >= cutoff][-2000:]
    try:
        tmp = _HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_history, separators=(",", ":")), encoding="utf-8")
        tmp.replace(_HISTORY_FILE)
    except Exception as exc:  # noqa: BLE001
        log.debug("subscription-hub: history persist failed: %s", exc)


def _load_history() -> None:
    if not _HISTORY_FILE.exists():
        return
    try:
        data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
        with _history_lock:
            _history.update({k: data[k] for k in ("last_sample_ts", "samples") if k in data})
    except Exception:  # noqa: BLE001
        pass


def _resample_history(days: int, max_points: int) -> dict[str, Any]:
    now = time.time()
    cutoff = now - max(1, int(days)) * 86400
    with _history_lock:
        samples = [s for s in _history.get("samples", []) if s["ts"] >= cutoff]
    if not samples:
        return {"ok": True, "samples": 0, "days": days, "models": {}}
    t0, t1 = samples[0]["ts"], samples[-1]["ts"]
    span = max(1, t1 - t0)
    nb = max(1, min(int(max_points), len(samples)))
    buckets: dict[str, dict] = {}
    for s in samples:
        for mid, rem in s["models"].items():
            if rem is None:
                continue
            b = min(nb - 1, int((s["ts"] - t0) * nb / span))
            bucket = buckets.setdefault(mid, {"counts": [0] * nb, "sums": [0.0] * nb})
            bucket["sums"][b] += rem
            bucket["counts"][b] += 1
    models: dict[str, list[dict[str, Any]]] = {}
    for mid, bucket in buckets.items():
        out: list[dict[str, Any]] = []
        for i in range(nb):
            out.append({
                "ts": int(t0 + (i + 0.5) * span / nb),
                "remaining": round(bucket["sums"][i] / bucket["counts"][i], 4) if bucket["counts"][i] else None,
            })
        models[mid] = out
    return {"ok": True, "samples": len(samples), "days": days, "models": models}


# --- Connect (OAuth login via the proxy CLI) --------------------------------
# CLIProxyAPI login providers. flags = proxy CLI login flags, auth_globs =
# fnmatch patterns that mark a connected account in the proxy auth dir.
PROVIDERS: list[dict[str, Any]] = [
    {"id": "antigravity", "label": "Google Antigravity", "kind": "oauth",
     "flags": ["-antigravity-login"], "auth_globs": ["antigravity-*.json"]},
    {"id": "opencode-go", "label": "OpenCode Go", "kind": "api-key",
     "flags": [], "auth_globs": []},
    {"id": "claude", "label": "Claude (Anthropic)", "kind": "oauth",
     "flags": ["-claude-login"], "auth_globs": ["claude-*.json"]},
    {"id": "codex", "label": "Codex (OpenAI)", "kind": "oauth",
     "flags": ["-codex-device-login"], "auth_globs": ["codex*.json"]},
    {"id": "kimi", "label": "Kimi (Moonshot)", "kind": "oauth",
     "flags": ["-kimi-login"], "auth_globs": ["kimi-*.json"]},
    {"id": "xai", "label": "Grok (xAI)", "kind": "oauth",
     "flags": ["-xai-login"], "auth_globs": ["xai-*.json"]},
    {"id": "vertex", "label": "Vertex (GCP)", "kind": "import",
     "flags": ["-vertex-import"], "auth_globs": ["vertex*.json"]},
]
PROVIDER_BY_ID = {p["id"]: p for p in PROVIDERS}


def _auth_files(auth_dir: Path, globs: list[str]) -> set[str]:
    """Set of auth-file names in auth_dir matching the provider's globs."""
    try:
        if not auth_dir.is_dir():
            return set()
        return {f for f in os.listdir(auth_dir) for pat in globs if fnmatch.fnmatch(f, pat)}
    except OSError:
        return set()


def _provider_connected(prov: dict) -> tuple[bool, str]:
    """(connected, detail) — detail names the newest matching auth file."""
    auth_dir = _auth_dir()
    try:
        if not auth_dir.is_dir():
            return False, "not connected"
        if prov["id"] == "antigravity":
            # honor selected_auth_file like the quota token path does
            p = _find_auth_file(auth_dir)
            return (True, p.name) if p is not None else (False, "not connected")
        files = [
            f for f in os.listdir(auth_dir)
            for pat in prov["auth_globs"] if fnmatch.fnmatch(f, pat)
        ]
        if not files:
            return False, "not connected"
        newest = max(files, key=lambda n: (auth_dir / n).stat().st_mtime)
        return True, newest
    except OSError:  # auth dir/file vanished mid-listing
        return False, "not connected"


def _resolve_proxy_bin() -> str:
    cfg = _CONFIG.get("proxy_bin")
    if cfg:
        return str(cfg)
    for cand in ("cli-proxy-api", os.path.expanduser("~/.local/bin/cli-proxy-api")):
        if shutil.which(cand) or Path(os.path.expanduser(cand)).is_file():
            return cand
    return "cli-proxy-api"


def _resolve_proxy_config() -> str:
    cfg = _CONFIG.get("proxy_config")
    if cfg:
        return str(cfg)
    p = DEFAULT_PROXY_CONFIG
    return str(p) if p.exists() else ""


def _drain_login_output(proc: subprocess.Popen) -> None:
    url_re = re.compile(r"https?://[^\s\"']+")
    assert proc.stdout is not None
    for line in proc.stdout:
        with _login_lock:
            _login_state["output"] = (_login_state["output"] + line)[-4000:]
        if not _login_state["auth_url"]:
            low = line.lower()
            # prefer URLs from instruction lines (visit/open/http); skip
            # obvious error bodies so a stack trace URL doesn't win
            if not (
                "visit" in low or "open" in low or "http" in low
            ) or low.lstrip().startswith(("error", "traceback", "panic", "exception", "failed", "fail")):
                continue
            m = url_re.search(line)
            if m:
                # first qualifying URL wins — covers every provider flow
                # (google, claude.ai, auth.openai.com, kimi/xai device URLs)
                url = m.group(0).rstrip(".,);'\"")
                with _login_lock:
                    _login_state["auth_url"] = url
    proc.wait()
    with _login_lock:
        # a newer connect may have replaced this proc (auto-terminate) —
        # never clobber the newer flow's state
        if _login_state.get("proc") is not proc:
            return
        _login_state["completed"] = True
        if _login_state.get("cancelled"):
            _login_state["error"] = "cancelled"
        elif proc.returncode != 0:
            _login_state["error"] = f"login exited with code {proc.returncode}"
        else:
            prov = PROVIDER_BY_ID.get(_login_state.get("provider") or "antigravity")
            globs = prov["auth_globs"] or ["antigravity-*.json"]
            files_after = _auth_files(_auth_dir(), globs)
            new = files_after - set(_login_state.get("files_before") or [])
            if new:
                fname = max(new, key=lambda n: (_auth_dir() / n).stat().st_mtime)
                _login_state["new_file"] = fname
                # only antigravity logins may claim the quota token file —
                # a claude/xai file would poison _find_auth_file()
                if prov["id"] == "antigravity":
                    _CONFIG["selected_auth_file"] = fname
                    _save_config()
                    _token_cache["token"] = None  # force re-read on next quota fetch
                    _token_cache["expires_at"] = 0.0


@router.get("/quota")
def get_quota() -> dict[str, Any]:
    try:
        return build_quota_state()
    except Exception as exc:  # noqa: BLE001
        log.warning("subscription-hub: build_quota_state failed: %s", exc)
        return {
            "ok": False,
            "ts": int(time.time()),
            "error": f"plugin backend error: {exc}",
            "models": [],
            "buckets": [],
            "account": None,
            "selected_auth_file": None,
            "token_expires_in": 0,
            "tier": None,
            "paid_tier": None,
            "host": None,
            "accounts": [],
            "alerts": [],
            "opencode_usage": _fetch_opencode_usage(),
            "config": _public_config(),
        }


@router.get("/health")
def get_health() -> dict[str, Any]:
    return {"ok": True, "plugin": "subscription-hub"}


# --- Proxy health (absorbed from cliproxy-widget) --------------------------
PROXY_BASE = os.environ.get("CLIPROXY_BASE", "http://127.0.0.1:8317")


def _proxy_key() -> str:
    k = os.environ.get("CLIPROXYAPI_API_KEY") or os.environ.get("CLIPROXY_API_KEY")
    if k and k.strip():
        return k.strip()
    env = Path.home() / ".hermes" / ".env"
    if env.is_file():
        try:
            for line in env.read_text(errors="ignore").splitlines():
                m = re.match(r'^(?:CLIPROXYAPI_API_KEY|CLIPROXY_API_KEY)\s*=\s*["\']?([^"\'\r\n]+)', line.strip())
                if m:
                    return m.group(1).strip()
        except Exception:  # noqa: BLE001
            pass
    return ""


@router.get("/status")
def get_status() -> dict[str, Any]:
    """Proxy reachability + model count + latency + cooldown latch state."""
    import urllib.request as _ur

    headers = {"Content-Type": "application/json"}
    key = _proxy_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    t0 = time.time()
    proxy_up = False
    model_count = 0
    latency_ms = None
    error = None
    try:
        req = _ur.Request(f"{PROXY_BASE}/v1/models", headers=headers, method="GET")
        with _ur.urlopen(req, timeout=4) as resp:
            latency_ms = int((time.time() - t0) * 1000)
            if resp.status == 200:
                proxy_up = True
                data = json.load(resp)
                model_count = len(data.get("data") or [])
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:200]
    return {
        "proxy_up": proxy_up,
        "latency_ms": latency_ms,
        "model_count": model_count,
        "cooldown": False,
        "errors_24h": 0,
        "error": error,
        "checked_at": int(t0),
    }


@router.get("/config")
def get_config() -> dict[str, Any]:
    return {"ok": True, "config": _public_config()}


@router.get("/providers")
def get_providers() -> dict[str, Any]:
    """List quota providers with connection status (for the UI provider tab)."""
    key = _get_opencode_api_key()
    out: list[dict[str, Any]] = []
    for prov in PROVIDERS:
        if prov["id"] == "opencode-go":
            out.append({
                "id": "opencode-go",
                "label": "OpenCode Go",
                "kind": "api-key",
                "connected": bool(key),
                "detail": "subscription usage" if key else "API key not set",
            })
            continue
        connected, detail = _provider_connected(prov)
        out.append({
            "id": prov["id"],
            "label": prov["label"],
            "kind": prov["kind"],
            "connected": connected,
            "detail": detail,
        })
    return {"ok": True, "providers": out}


@router.put("/config")
def put_config(body: dict[str, Any]) -> dict[str, Any]:
    """Update plugin overrides. Whitelisted keys only; partial updates merge."""
    allowed = {
        "auth_dir": str,
        "selected_auth_file": str,
        "hosts": list,
        "load_hosts": list,
        "client_id": str,
        "client_secret": str,
        "token": str,
        "models": list,
        "primary_model": str,
        "refresh_interval_seconds": int,
        "low_threshold": float,
        "proxy_bin": str,
        "proxy_config": str,
        "show_providers": list,
    }
    changed = False
    for key, typ in allowed.items():
        if key not in body:
            continue
        val = body[key]
        if val is None:
            _CONFIG.pop(key, None)
            changed = True
            continue
        try:
            if typ is list:
                if not isinstance(val, list):
                    return {"ok": False, "error": f"expected a list for {key}"}
                val = [str(v).strip() for v in val if str(v).strip()]
            elif typ is int:
                val = int(val)
                if key == "refresh_interval_seconds":
                    val = max(15, min(3600, val))
            elif typ is float:
                val = max(0.0, min(1.0, float(val)))
            else:
                val = str(val).strip()
        except (TypeError, ValueError):
            return {"ok": False, "error": f"invalid value for {key}"}
        _CONFIG[key] = val
        changed = True
    if not changed:
        return {"ok": False, "error": "no supported keys in body"}
    _save_config()
    return {"ok": True, "config": _public_config()}


@router.get("/accounts")
def get_accounts() -> dict[str, Any]:
    return {"ok": True, "accounts": _list_accounts()}


@router.post("/connect")
def post_connect(body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    # default antigravity keeps the v3 no-body call working unchanged
    provider_id = "antigravity"
    if isinstance(body, dict) and body.get("provider"):
        provider_id = str(body["provider"])
    prov = PROVIDER_BY_ID.get(provider_id)
    if prov is None:
        return {"ok": False, "error": "unknown provider"}
    if prov["kind"] not in ("oauth", "import"):
        # api-key providers (e.g. opencode-go) have no CLI login flow and no
        # flags — spawning the proxy without a login flag would start the
        # proxy itself instead of an auth flow
        return {"ok": False, "error": f"provider {provider_id} is not connectable via CLI"}
    proxy_config = _resolve_proxy_config()
    if not proxy_config:
        return {"ok": False, "error": "proxy config not found — set proxy_config in the plugin config"}
    bin_path = _resolve_proxy_bin()
    if not (shutil.which(bin_path) or Path(os.path.expanduser(bin_path)).is_file()):
        return {"ok": False, "error": f"cli-proxy-api binary not found ({bin_path})"}
    cmd = [bin_path, "-config", proxy_config] + list(prov["flags"])
    if provider_id == "vertex":
        vfile = (body or {}).get("file") if isinstance(body, dict) else None
        if not vfile or not str(vfile).endswith(".json"):
            return {"ok": False, "error": "vertex import requires a .json file path"}
        vpath = Path(os.path.expanduser(str(vfile)))
        if not vpath.is_file():
            return {"ok": False, "error": f"vertex import file not found: {vpath}"}
        cmd.append(str(vpath))
    cmd.append("-no-browser")
    auth_dir = _auth_dir()
    globs = prov["auth_globs"] or ["antigravity-*.json"]
    files_before = _auth_files(auth_dir, globs)
    note = ""
    with _login_lock:
        # re-check under the lock: a concurrent POST /connect may have
        # started a login while we resolved the provider — spawning now
        # would orphan the first child (double-spawn race)
        proc = _login_state.get("proc")
        if proc is not None and proc.poll() is None:
            prev = _login_state.get("provider")
            if prev == provider_id:
                # same provider already authenticating — idempotent reopen
                return {"ok": True, "status": "running", "provider": provider_id,
                        "auth_url": _login_state.get("auth_url")}
            # A DIFFERENT provider's login is still waiting for browser auth
            # (these flows idle up to 1800s). The user asked for this provider
            # explicitly, so retire the stale flow — otherwise the UI would
            # keep showing the old provider's auth_url as ours.
            note = f"[terminated previous {prev or 'unknown'} login to start {provider_id}]\n"
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"failed to start login: {exc}"}
        _login_state.update(
            proc=proc,
            provider=provider_id,
            auth_url=None,
            output=note,
            started_at=time.time(),
            completed=False,
            cancelled=False,
            error=None,
            new_file=None,
            files_before=files_before,
        )
    threading.Thread(target=_drain_login_output, args=(proc,), daemon=True).start()
    return {"ok": True, "status": "started", "provider": provider_id}


@router.get("/connect/status")
def get_connect_status() -> dict[str, Any]:
    with _login_lock:
        ls = {k: v for k, v in _login_state.items() if k != "proc"}
        proc = _login_state.get("proc")
        running = proc is not None and proc.poll() is None
        if proc is not None and not running and not ls.get("completed") and proc.returncode is not None:
            # process died before the drain thread noticed — mark it
            ls["completed"] = True
            ls["error"] = ls.get("error") or f"login exited with code {proc.returncode}"
    return {"ok": True, "running": running, **ls}


@router.post("/connect/cancel")
def post_connect_cancel() -> dict[str, Any]:
    with _login_lock:
        proc = _login_state.get("proc")
        running = proc is not None and proc.poll() is None
        _login_state["completed"] = True
        _login_state["cancelled"] = True
        _login_state["error"] = "cancelled"
    if running:
        # terminate/wait outside the lock so a hung child never blocks
        # status reads or a second cancel
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    return {"ok": True}


@router.get("/history")
def get_history(days: int = 7, max_points: int = 168) -> dict[str, Any]:
    try:
        return _resample_history(int(days), int(max_points))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "models": {}}


# --- FreeRouter control (grafted from router-manager) ---------------------
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
ENV_FILE = HERMES_HOME / ".env"
ROUTER_DIR = Path("/home/decrux/Code/freerouter")
ROUTER_ENTRY = ROUTER_DIR / "dist" / "server.js"
ROUTER_CONFIG = Path("/home/decrux/.config/freerouter/config.json")
ROUTER_PORT = 18800
ROUTER_BASE = f"http://127.0.0.1:{ROUTER_PORT}"

_router_proc: subprocess.Popen | None = None


# --- env helpers -----------------------------------------------------------
def _router_load_env_keys() -> dict[str, str]:
    """Read API keys from the Hermes .env (never log them)."""
    keys = {}
    if not ENV_FILE.exists():
        return keys
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k.startswith(("CLIPROXY", "OPENROUTER", "KIMI", "GROQ", "DEEPSEEK")):
            keys[k] = v
    return keys


def _router_env_for_router() -> dict[str, str]:
    env = dict(os.environ)
    env.update(_router_load_env_keys())
    env["FREEROUTER_CONFIG"] = str(ROUTER_CONFIG)
    return env


# --- process control --------------------------------------------------------
def _router_is_running() -> bool:
    if _router_proc is not None and _router_proc.poll() is None:
        return True
    try:
        import socket

        with socket.create_connection(("127.0.0.1", ROUTER_PORT), timeout=1):
            return True
    except OSError:
        return False


def _router_start() -> dict[str, Any]:
    global _router_proc
    if _router_is_running():
        return {"ok": True, "already": True}
    if not ROUTER_ENTRY.exists():
        raise HTTPException(status_code=500, detail=f"router entry missing: {ROUTER_ENTRY}")
    node = shutil.which("node") or "/home/decrux/.hermes/node/bin/node"
    log_path = Path("/tmp/freerouter.log")
    logf = open(log_path, "a")
    _router_proc = subprocess.Popen(
        [node, str(ROUTER_ENTRY)],
        cwd=str(ROUTER_DIR),
        env=_router_env_for_router(),
        stdout=logf,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return {"ok": True, "pid": _router_proc.pid}


def _router_stop() -> dict[str, Any]:
    global _router_proc
    if _router_proc is not None and _router_proc.poll() is None:
        _router_proc.terminate()
        try:
            _router_proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            _router_proc.kill()
    _router_proc = None
    return {"ok": True}


# --- router relay ------------------------------------------------------------
async def _router_relay(path: str, method: str = "GET") -> dict[str, Any]:
    import httpx

    url = f"{ROUTER_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.request(method, url)
            if resp.status_code >= 400:
                return {"ok": False, "status": resp.status_code, "detail": resp.text[:300]}
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@router.get("/router/status")
async def router_status() -> dict[str, Any]:
    running = _router_is_running()
    info = await _router_relay("/health") if running else {}
    return {
        "running": running,
        "port": ROUTER_PORT,
        "config": str(ROUTER_CONFIG),
        "health": info,
    }


@router.post("/router/start")
async def router_start() -> dict[str, Any]:
    return _router_start()


@router.post("/router/stop")
async def router_stop() -> dict[str, Any]:
    return _router_stop()


@router.post("/router/restart")
async def router_restart() -> dict[str, Any]:
    _router_stop()
    await asyncio.sleep(0.5)
    return _router_start()


@router.get("/router/stats")
async def router_stats() -> dict[str, Any]:
    return await _router_relay("/stats")


@router.post("/router/reload")
async def router_reload() -> dict[str, Any]:
    return await _router_relay("/reload-config", method="POST")


@router.get("/router/config")
async def router_config() -> dict[str, Any]:
    return await _router_relay("/config")


_CONFIG.update(_load_config())
_load_history()
