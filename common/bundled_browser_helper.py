"""Keep one Playwright Chromium profile alive and expose its CDP endpoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

# The helper is launched by absolute path, so Python's import root is
# ``common/`` instead of the project directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.direct_proxy import parse_proxy


async def _wait_for_cdp(profile_dir: Path) -> tuple[str, str]:
    active_port = profile_dir / "DevToolsActivePort"
    for _ in range(160):
        if active_port.exists():
            lines = active_port.read_text(encoding="utf-8", errors="ignore").splitlines()
            if lines and lines[0].strip():
                port = int(lines[0].strip())
                version_url = f"http://127.0.0.1:{port}/json/version"
                try:
                    with urllib.request.urlopen(version_url, timeout=1.5) as response:
                        payload = json.load(response)
                    ws = str(payload.get("webSocketDebuggerUrl") or "").strip()
                    if ws:
                        return ws, f"http://127.0.0.1:{port}"
                except Exception:
                    pass
        await asyncio.sleep(0.25)
    raise RuntimeError("bundled Chromium CDP endpoint did not become ready")


async def run(args: argparse.Namespace) -> None:
    profile_dir = Path(args.profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    browser_path = str(Path(args.browser_path).resolve())
    proxy = parse_proxy(args.proxy)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            executable_path=browser_path,
            headless=False,
            proxy=proxy.as_playwright() if proxy else None,
            args=["--remote-debugging-port=0"],
            viewport={"width": 1440, "height": 900},
        )
        try:
            ws, http = await _wait_for_cdp(profile_dir)
            print(f"BUNDLED_BROWSER: {http}", flush=True)
            print(f"BUNDLED_BROWSER_WS: {ws}", flush=True)
            while not stop.is_set():
                # Windows' ``os.kill(pid, 0)`` is not a reliable liveness probe
                # for a process launched from another virtual environment. The
                # adapter explicitly terminates this helper, so only use the
                # parent watchdog where the probe is well-defined.
                if args.parent_pid and os.name != "nt":
                    try:
                        os.kill(int(args.parent_pid), 0)
                    except OSError:
                        break
                await asyncio.sleep(1)
        finally:
            await context.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--browser-path", required=True)
    parser.add_argument("--proxy", default="")
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
