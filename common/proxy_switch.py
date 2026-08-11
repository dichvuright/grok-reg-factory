"""Unified Clash and residential proxy selection.

Supported modes:
  clash_auto     rotate among responsive Clash leaf nodes
  clash_fixed    always enforce CLASH_FIXED_NODE
  residential   use REG_FACTORY_PROXY or REG_FACTORY_PROXY_POOL
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request

from common import direct_proxy

try:
    import config  # noqa: F401
except Exception:
    pass

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CLASH_API = os.environ.get("CLASH_API", "http://127.0.0.1:9097")
CLASH_SECRET = os.environ.get("CLASH_SECRET", "")
CLASH_PROXY = os.environ.get("CLASH_PROXY", "http://127.0.0.1:7897")
DEFAULT_GROUP = os.environ.get("CLASH_GROUP", "GLOBAL")
DIRECT_PROXY = (
    os.environ.get("REG_FACTORY_PROXY")
    or os.environ.get("RESIDENTIAL_PROXY")
    or os.environ.get("DIRECT_PROXY")
    or ""
).strip()
NO_CLASH = os.environ.get("REG_FACTORY_NO_CLASH", "").strip().lower() in {"1", "true", "yes", "on"}

_MODE_ALIASES = {
    "auto": "clash_auto",
    "clash": "clash_auto",
    "clash_auto": "clash_auto",
    "rotate": "clash_auto",
    "fixed": "clash_fixed",
    "clash_fixed": "clash_fixed",
    "residential": "residential",
    "direct": "residential",
    "proxy": "residential",
    "none": "direct",
    "off": "direct",
}

_PLATFORMS = ("outlook", "claude", "chatgpt", "grok", "kiro")


def _env(environ=None):
    return os.environ if environ is None else environ


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def platform_name(environ=None) -> str:
    value = str(_env(environ).get("REG_FACTORY_PLATFORM") or "").strip().lower()
    return value if value in _PLATFORMS else ""


def proxy_mode(environ=None) -> str:
    env = _env(environ)
    platform = platform_name(env)
    platform_mode = str(env.get(f"{platform.upper()}_PROXY_MODE") or "").strip().lower() if platform else ""
    if platform_mode in {"inherit", "global", "default"}:
        platform_mode = ""
    raw = platform_mode or str(
        env.get("PROXY_MODE") or env.get("REG_FACTORY_PROXY_MODE") or ""
    ).strip().lower()
    if raw:
        return _MODE_ALIASES.get(raw, "clash_auto")
    if direct_proxy.configured_proxy(environ=env) or _truthy(env.get("REG_FACTORY_NO_CLASH")):
        return "residential"
    return "clash_auto"


def fixed_node(environ=None) -> str:
    env = _env(environ)
    platform = platform_name(env)
    if platform:
        specific = str(env.get(f"{platform.upper()}_CLASH_FIXED_NODE") or "").strip()
        if specific:
            return specific
    return str(env.get("CLASH_FIXED_NODE") or env.get("CLASH_NODE") or "").strip()


def effective_proxy_url(environ=None) -> str:
    env = _env(environ)
    if proxy_mode(env) == "residential":
        proxy = direct_proxy.configured_proxy(environ=env)
        return proxy.url if proxy else ""
    if proxy_mode(env) == "direct":
        return ""
    return str(env.get("CLASH_PROXY") or "http://127.0.0.1:7897").strip()


def platform_environment(environ=None, platform: str = "") -> dict:
    """Return a child environment with the selected platform egress applied."""
    env = dict(_env(environ))
    normalized = str(platform or "").strip().lower()
    if normalized:
        if normalized not in _PLATFORMS:
            raise ValueError(f"unsupported proxy platform: {normalized}")
        env["REG_FACTORY_PLATFORM"] = normalized
    proxy = effective_proxy_url(env)
    proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    if proxy:
        for key in proxy_keys:
            env[key] = proxy
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
    else:
        for key in proxy_keys:
            env.pop(key, None)
    return env


def apply_platform_environment(platform: str, environ=None) -> dict:
    """Apply a platform route to the current process before a direct CLI run."""
    target = _env(environ)
    configured = platform_environment(target, platform)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if key not in configured:
            target.pop(key, None)
    target.update(configured)
    return target


def browser_proxy_fields(environ=None) -> dict:
    """Convert the selected egress into BitBrowser profile fields."""
    proxy = direct_proxy.parse_proxy(effective_proxy_url(environ))
    if proxy is None:
        return {"proxyMethod": 1, "proxyType": "noproxy"}
    if proxy.scheme == "socks4":
        raise ValueError("BitBrowser profiles support HTTP(S) or SOCKS5 proxies, not SOCKS4")
    fields = {
        "proxyMethod": 2,
        "proxyType": "socks5" if proxy.scheme == "socks5" else "http",
        "host": proxy.host,
        "port": str(proxy.port),
    }
    if proxy.username:
        fields["proxyUserName"] = proxy.username
    if proxy.password:
        fields["proxyPassword"] = proxy.password
    return fields


def is_direct_mode(environ=None) -> bool:
    return proxy_mode(environ) in {"residential", "direct"}


def _api(environ=None) -> str:
    return str(_env(environ).get("CLASH_API") or "http://127.0.0.1:9097").rstrip("/")


def _group(group=None, environ=None) -> str:
    return group or str(_env(environ).get("CLASH_GROUP") or "GLOBAL")


def _headers(environ=None):
    secret = str(_env(environ).get("CLASH_SECRET") or "")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    return headers


def _get(path, environ=None):
    req = urllib.request.Request(f"{_api(environ)}{path}", headers=_headers(environ))
    with urllib.request.urlopen(req, timeout=8) as response:
        return json.loads(response.read())


def list_nodes(group=None, environ=None):
    if is_direct_mode(environ):
        return ["residential"] if proxy_mode(environ) == "residential" else ["direct"]
    data = _get(f"/proxies/{urllib.parse.quote(_group(group, environ))}", environ)
    return data.get("all", [])


def available_clash_nodes(group=None, environ=None):
    """Return Clash leaf nodes even when another proxy mode is active."""
    names = (_get(f"/proxies/{urllib.parse.quote(_group(group, environ))}", environ).get("all") or [])
    return _filter_concrete_nodes(names, environ)


def _filter_concrete_nodes(names, environ=None):
    metadata = ("套餐", "剩余", "重置", "到期", "官网", "http://", "https://")
    try:
        catalog = (_get("/proxies", environ).get("proxies") or {})
        group_types = {"Selector", "URLTest", "Fallback", "LoadBalance"}
        return [
            name for name in names
            if name not in {"DIRECT", "REJECT"}
            and not any(marker in name for marker in metadata)
            and (catalog.get(name) or {}).get("type") not in group_types
        ]
    except Exception:
        return [
            name for name in names
            if (any(char.isdigit() for char in name)
                or (name and 0x1F1E6 <= ord(name[0]) <= 0x1F1FF))
            and not any(marker in name for marker in metadata)
        ]


def current_node(group=None, environ=None):
    mode = proxy_mode(environ)
    if mode == "residential":
        return direct_proxy.redact_proxy(effective_proxy_url(environ)) or "residential"
    if mode == "direct":
        return "direct"
    data = _get(f"/proxies/{urllib.parse.quote(_group(group, environ))}", environ)
    return data.get("now")


def set_node(name, group=None, force=False, environ=None):
    mode = proxy_mode(environ)
    if mode in {"residential", "direct"}:
        return True
    target = fixed_node(environ) if mode == "clash_fixed" and not force else str(name or "").strip()
    if not target:
        raise ValueError("CLASH_FIXED_NODE is required in clash_fixed mode")
    data = json.dumps({"name": target}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{_api(environ)}/proxies/{urllib.parse.quote(_group(group, environ))}",
        data=data,
        method="PUT",
        headers=_headers(environ),
    )
    urllib.request.urlopen(req, timeout=8).read()
    return True


def node_delay(name, url="https://www.google.com/generate_204", timeout_ms=5000, environ=None):
    if is_direct_mode(environ):
        return None
    try:
        data = _get(
            f"/proxies/{urllib.parse.quote(name)}/delay"
            f"?timeout={timeout_ms}&url={urllib.parse.quote(url)}", environ
        )
        return data.get("delay")
    except Exception:
        return None


def concrete_nodes(group=None, environ=None):
    if is_direct_mode(environ):
        return list_nodes(group, environ)
    return _filter_concrete_nodes(list_nodes(group, environ), environ)


def _mojibake_candidates(value: str):
    """Yield common repeated UTF-8-as-Latin-1 repairs for legacy .env values."""
    current = str(value or "").strip()
    seen = set()
    for _ in range(3):
        if not current or current in seen:
            break
        seen.add(current)
        yield current
        try:
            repaired = current.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if repaired == current:
            break
        current = repaired


def resolve_fixed_node(environ=None) -> str:
    """Resolve a configured fixed node, repairing legacy mojibake when possible."""
    requested = fixed_node(environ)
    if not requested:
        raise ValueError("固定节点模式需要配置 CLASH_FIXED_NODE")
    nodes = available_clash_nodes(environ=environ)
    for candidate in _mojibake_candidates(requested):
        if candidate in nodes:
            return candidate
    raise ValueError(
        f"固定节点不存在或已下线: {requested!r}；请在代理设置中重新选择节点"
    )


def ensure_proxy_mode(environ=None):
    """Apply mode constraints before a task starts."""
    mode = proxy_mode(environ)
    if mode == "clash_fixed":
        node = resolve_fixed_node(environ)
        set_node(node, force=True, environ=environ)
    return {
        "mode": mode,
        "platform": platform_name(environ) or "global",
        "node": current_node(environ=environ),
        "proxy": effective_proxy_url(environ),
    }


def _call_rotate_url(environ=None):
    env = _env(environ)
    url = str(
        env.get("REG_FACTORY_PROXY_ROTATE_URL")
        or env.get("RESIDENTIAL_PROXY_ROTATE_URL")
        or ""
    ).strip()
    if not url:
        return ""
    method = str(env.get("REG_FACTORY_PROXY_ROTATE_METHOD") or "GET").upper()
    if method not in {"GET", "POST"}:
        raise ValueError("REG_FACTORY_PROXY_ROTATE_METHOD must be GET or POST")
    request = urllib.request.Request(url, data=b"" if method == "POST" else None, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=20) as response:
        return response.read(512).decode("utf-8", "replace").strip()


def rotate_proxy(group=None, environ=None):
    """Rotate the configured egress and return a serializable result."""
    mode = proxy_mode(environ)
    if mode == "clash_fixed":
        ensure_proxy_mode(environ)
        return {"ok": True, "mode": mode, "node": current_node(environ=environ), "changed": False}
    if mode == "residential":
        before = effective_proxy_url(environ)
        active = direct_proxy.rotate_proxy_pool(environ)
        response = _call_rotate_url(environ)
        after = active.url if active else effective_proxy_url(environ)
        return {
            "ok": bool(after),
            "mode": mode,
            "node": direct_proxy.redact_proxy(after) or "residential",
            "changed": before != after or bool(response),
            "provider_response": response[:160],
        }
    if mode == "direct":
        return {"ok": True, "mode": mode, "node": "direct", "changed": False}

    nodes = concrete_nodes(group, environ)
    if not nodes:
        return {"ok": False, "mode": mode, "node": None, "changed": False, "error": "Clash 代理组没有叶子节点"}
    try:
        current = current_node(group, environ)
    except Exception:
        current = None
    start = (nodes.index(current) + 1) if current in nodes else 0
    ordered = nodes[start:] + nodes[:start]
    for node in ordered:
        delay = node_delay(node, environ=environ)
        if delay is None:
            continue
        set_node(node, group, force=True, environ=environ)
        return {"ok": True, "mode": mode, "node": node, "changed": node != current, "latency_ms": delay}
    return {"ok": False, "mode": mode, "node": current, "changed": False, "error": "没有响应正常的 Clash 节点"}


def find_working_node(
    test_url="https://grok.com/", group=None,
    challenge_markers=("just a moment", "performing security"),
    required_markers=(), warmup_url=None, candidates=None,
    settle=3, timeout=18, verbose=True, environ=None,
):
    mode = proxy_mode(environ)
    if mode == "residential":
        if verbose:
            print("  [proxy] residential proxy configured; Clash rotation skipped")
        return "residential" if effective_proxy_url(environ) else None
    if mode == "direct":
        return "direct"
    if mode == "clash_fixed":
        node = fixed_node(environ)
        if not node:
            return None
        candidates = [node]

    try:
        from curl_cffi import requests as creq
    except ImportError:
        creq = None
    nodes = list(candidates or concrete_nodes(group, environ))
    if mode == "clash_auto":
        random.shuffle(nodes)
    for name in nodes:
        try:
            set_node(name, group, environ=environ)
        except Exception:
            continue
        time.sleep(settle)
        if creq is None:
            return name
        try:
            proxies = {
                "http": effective_proxy_url(environ),
                "https": effective_proxy_url(environ),
            }
            if warmup_url:
                session = creq.Session(impersonate="chrome131", http_version="v2")
                session.proxies = proxies
                try:
                    session.get(warmup_url, allow_redirects=True, timeout=timeout)
                    response = session.get(test_url, allow_redirects=True, timeout=timeout)
                finally:
                    session.close()
            else:
                response = creq.get(
                    test_url, impersonate="chrome131", proxies=proxies, timeout=timeout
                )
            body = response.text[:200000].lower()
            challenged = any(marker.lower() in body for marker in challenge_markers)
            required_ok = all(marker.lower() in body for marker in required_markers)
            ok = response.status_code == 200 and not challenged and required_ok
            if verbose:
                reason = "PASS" if ok else ("INCOMPLETE" if not required_ok else "BLOCK")
                print(f"  [node] {name}: HTTP {response.status_code} {reason}")
            if ok:
                return name
        except Exception as exc:
            if verbose:
                print(f"  [node] {name}: ERR {str(exc)[:30]}")
    return None


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "list":
        print("current:", current_node())
        for item in list_nodes():
            print(" ", item)
    elif args[0] == "current":
        print(current_node())
    elif args[0] == "rotate":
        print(json.dumps(rotate_proxy(), ensure_ascii=False))
    elif args[0] == "set" and len(args) > 1:
        set_node(args[1])
        time.sleep(1)
        print("switched ->", current_node())
