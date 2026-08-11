"""BitBrowser-compatible local adapter backed by bundled Playwright Chromium."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from common.direct_proxy import ProxySpec, parse_proxy


def find_browser_path() -> str:
    """Find the configured or installed Chrome/Chromium executable."""
    candidates = [
        os.environ.get("CUSTOM_BROWSER_PATH", ""),
        os.environ.get("REG_FACTORY_BROWSER_PATH", ""),
        os.environ.get("BUNDLED_BROWSER_PATH", ""),
        shutil.which("chrome") or "",
        shutil.which("chrome.exe") or "",
        shutil.which("chromium") or "",
        shutil.which("chromium.exe") or "",
    ]
    if os.name == "nt":
        for root_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = os.environ.get(root_name, "")
            if root:
                candidates.append(str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"))
                candidates.append(str(Path(root) / "Chromium" / "Application" / "chrome.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return ""


class BundledBrowser:
    provider_name = "bundled"

    def __init__(self, api_base=None):
        del api_base
        root = Path(os.environ.get("REG_FACTORY_DATA_DIR") or Path.cwd() / ".reg-factory-data").resolve()
        self.root = root
        self.profile_root = root / "browser-profiles"
        self.state_path = root / "browser-profiles.json"
        self.profile_root.mkdir(parents=True, exist_ok=True)
        self._processes: dict[str, subprocess.Popen] = {}

    def _load(self) -> dict:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _save(self, value: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(f".{os.getpid()}.tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.state_path)

    def _browser_path(self) -> str:
        path = find_browser_path()
        if path:
            return path
        raise RuntimeError(
            "Chrome/Chromium is unavailable; configure CUSTOM_BROWSER_PATH or REG_FACTORY_BROWSER_PATH"
        )

    @staticmethod
    def _proxy_from_fields(fields: dict) -> ProxySpec | None:
        raw = fields.get("proxy_str") or fields.get("proxyUrl") or fields.get("proxy")
        if raw:
            return parse_proxy(str(raw))
        host = str(fields.get("host") or "").strip()
        port = str(fields.get("port") or "").strip()
        if not host or not port or str(fields.get("proxyType") or "").lower() == "noproxy":
            return None
        scheme = "socks5" if str(fields.get("proxyType") or "").lower() == "socks5" else "http"
        user = str(fields.get("proxyUserName") or "")
        password = str(fields.get("proxyPassword") or "")
        return parse_proxy(f"{scheme}://{user}:{password}@{host}:{port}" if user else f"{scheme}://{host}:{port}")

    def create_browser(self, name="reg_factory", **kwargs):
        profile_id = uuid.uuid4().hex
        profiles = self._load()
        proxy = self._proxy_from_fields(kwargs)
        if proxy is None:
            from common import proxy_switch
            proxy = parse_proxy(proxy_switch.effective_proxy_url())
        profiles[profile_id] = {
            "id": profile_id,
            "name": name,
            "proxy": proxy.url if proxy else "",
            "browserFingerPrint": kwargs.get("browserFingerPrint") or {},
            "createdAt": time.time(),
        }
        self._save(profiles)
        return profile_id

    def list_browsers(self, page=0, page_size=100):
        del page
        values = list(self._load().values())[:page_size]
        return {"success": True, "data": {"list": values, "total": len(values)}}

    def update_browser_fingerprint(self, profile_id, **fingerprint):
        return self._post(
            "/browser/update/partial",
            {"ids": [profile_id], "browserFingerPrint": fingerprint},
        )

    def _profile(self, profile_id: str) -> dict:
        profile = self._load().get(str(profile_id))
        if not profile:
            raise RuntimeError(f"unknown bundled browser profile: {profile_id}")
        return profile

    def open_browser(self, profile_id):
        profile_id = str(profile_id)
        existing = self._processes.get(profile_id)
        if existing and existing.poll() is None:
            return self._read_endpoint(existing)
        profile = self._profile(profile_id)
        profile_dir = self.profile_root / profile_id
        helper = os.environ.get("REG_FACTORY_BROWSER_HELPER", "").strip()
        python = os.environ.get("REG_FACTORY_PYTHON") or sys.executable
        if helper and Path(helper).is_file() and Path(helper).suffix.lower() == ".exe":
            command = [helper]
        else:
            helper_script = Path(helper) if helper else Path(__file__).with_name("bundled_browser_helper.py")
            command = [python, str(helper_script)]
        command += ["--profile-dir", str(profile_dir), "--browser-path", self._browser_path(), "--parent-pid", str(os.getpid())]
        if profile.get("proxy"):
            command += ["--proxy", profile["proxy"]]
        process = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._processes[profile_id] = process
        return self._read_endpoint(process)

    @staticmethod
    def _read_endpoint(process: subprocess.Popen) -> dict:
        deadline = time.monotonic() + 90
        lines = []
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"bundled browser exited before CDP ready: {' '.join(lines[-3:])}")
            line = process.stdout.readline() if process.stdout else ""
            if line:
                lines.append(line.strip())
                if line.startswith("BUNDLED_BROWSER_WS:"):
                    ws = line.split(":", 1)[1].strip()
                    return {"ws": ws, "http": ws.replace("ws://", "http://").rsplit("/devtools/", 1)[0]}
            else:
                time.sleep(0.1)
        raise RuntimeError("bundled browser did not expose CDP in 90 seconds")

    def close_browser(self, profile_id):
        profile_id = str(profile_id)
        process = self._processes.pop(profile_id, None)
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=8)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        return {"success": True}

    def delete_browser(self, profile_id):
        self.close_browser(profile_id)
        profiles = self._load()
        profiles.pop(str(profile_id), None)
        self._save(profiles)
        shutil.rmtree(self.profile_root / str(profile_id), ignore_errors=True)
        return {"success": True}

    def cleanup_browsers(self, keep=0):
        values = sorted(self._load().values(), key=lambda item: item.get("createdAt", 0), reverse=True)
        for profile in values[keep:]:
            self.delete_browser(profile["id"])
        return max(0, len(values) - keep)

    def _post(self, path, data=None, _retries=1):
        del _retries
        payload = dict(data or {})
        if path == "/browser/update":
            profile_id = str(payload.get("id") or "")
            if not profile_id:
                name = str(payload.pop("name", "") or "reg_factory")
                profile_id = self.create_browser(name=name, **payload)
                return {"success": True, "data": self._profile(profile_id)}
            profile = self._profile(profile_id)
            proxy = self._proxy_from_fields(payload)
            if proxy is not None or str(payload.get("proxyType") or "").lower() == "noproxy":
                profile["proxy"] = proxy.url if proxy else ""
            if payload.get("name"):
                profile["name"] = payload["name"]
            profiles = self._load()
            profiles[profile_id] = profile
            self._save(profiles)
            return {"success": True, "data": profile}
        if path == "/browser/update/partial":
            fingerprint = dict(payload.get("browserFingerPrint") or {})
            for profile_id in payload.get("ids") or []:
                profile = self._profile(str(profile_id))
                current = dict(profile.get("browserFingerPrint") or {})
                current.update(fingerprint)
                profile["browserFingerPrint"] = current
                profiles = self._load()
                profiles[str(profile_id)] = profile
                self._save(profiles)
            return {"success": True}
        if path == "/browser/list":
            return self.list_browsers(
                page=int(payload.get("page") or 0),
                page_size=int(payload.get("pageSize") or 100),
            )
        if path == "/browser/open":
            return {"success": True, "data": self.open_browser(payload.get("id"))}
        if path == "/browser/close":
            return self.close_browser(payload.get("id"))
        if path == "/browser/delete":
            return self.delete_browser(payload.get("id"))
        raise RuntimeError(f"unsupported bundled browser API path: {path}")
