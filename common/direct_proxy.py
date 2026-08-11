"""Residential proxy parsing and pool state shared by all runners."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


@dataclass(frozen=True)
class ProxySpec:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def server(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def url(self) -> str:
        credentials = ""
        if self.username:
            credentials = quote(self.username, safe="")
            if self.password:
                credentials += f":{quote(self.password, safe='')}"
            credentials += "@"
        return f"{self.scheme}://{credentials}{self.host}:{self.port}"

    def as_playwright(self) -> dict:
        value = {"server": self.server}
        if self.username:
            value["username"] = self.username
        if self.password:
            value["password"] = self.password
        return value


def parse_proxy(value: str | None) -> ProxySpec | None:
    """Parse http(s)/socks4/socks5 proxy URLs and host:port shorthand."""
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = raw if "://" in raw else f"http://{raw}"
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https", "socks4", "socks5"}:
        raise ValueError(f"unsupported proxy scheme: {parsed.scheme}")
    if not parsed.hostname or not parsed.port:
        raise ValueError("proxy must include host and port")
    return ProxySpec(
        scheme=parsed.scheme.lower(),
        host=parsed.hostname,
        port=int(parsed.port),
        username=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
    )


def _env(environ=None):
    return os.environ if environ is None else environ


def _pool_values(environ=None) -> list[str]:
    env = _env(environ)
    raw = env.get("REG_FACTORY_PROXY_POOL") or env.get("RESIDENTIAL_PROXY_POOL") or ""
    normalized = raw.replace("\r", "").replace(",", "\n").replace(";", "\n")
    return [item.strip() for item in normalized.split("\n") if item.strip()]


def proxy_pool(environ=None) -> list[ProxySpec]:
    return [proxy for item in _pool_values(environ) if (proxy := parse_proxy(item)) is not None]


def _state_path(environ=None) -> Path:
    env = _env(environ)
    configured = str(env.get("REG_FACTORY_PROXY_STATE_FILE") or "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / "runtime" / "state" / "residential_proxy_index.txt"


def _active_index(size: int, environ=None) -> int:
    if size <= 0:
        return 0
    try:
        return int(_state_path(environ).read_text(encoding="utf-8").strip()) % size
    except (OSError, ValueError):
        return 0


def configured_proxy(explicit: str | None = None, environ=None) -> ProxySpec | None:
    """Return the active residential proxy, preferring the configured pool."""
    if explicit is not None:
        return parse_proxy(explicit)
    env = _env(environ)
    pool = proxy_pool(env)
    if pool:
        return pool[_active_index(len(pool), env)]
    raw = (
        env.get("REG_FACTORY_PROXY")
        or env.get("RESIDENTIAL_PROXY")
        or env.get("DIRECT_PROXY")
        or ""
    )
    return parse_proxy(raw)


def rotate_proxy_pool(environ=None) -> ProxySpec | None:
    """Advance the residential proxy pool and persist the active index."""
    env = _env(environ)
    pool = proxy_pool(env)
    if not pool:
        return configured_proxy(environ=env)
    index = (_active_index(len(pool), env) + 1) % len(pool)
    path = _state_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(str(index), encoding="utf-8")
    os.replace(tmp, path)
    return pool[index]


def redact_proxy(value: str | ProxySpec | None) -> str:
    """Return a display-safe proxy URL without credentials."""
    try:
        spec = value if isinstance(value, ProxySpec) else parse_proxy(value)
    except ValueError:
        return "invalid proxy"
    return spec.server if spec else ""
