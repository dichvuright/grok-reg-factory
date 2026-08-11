# -*- coding: utf-8 -*-
"""
common/local_turnstile.py — Local Playwright-based Cloudflare Turnstile solver.

Solves Turnstile captchas using a real Chromium browser (Playwright),
no API key required. Designed as a drop-in for grok_reg_factory's
solve_turnstile() flow.

Features:
  - Synchronous API (compatible with ThreadPoolExecutor)
  - Advanced stealth JS (10+ anti-detection layers)
  - Human-like Bézier curve mouse movement
  - Fast polling (0.5s interval)
  - Configurable headless/non-headless mode
  - Debug screenshot on failure
  - Exponential backoff retries

Config via env / config.py:
  LOCAL_CAPTCHA=true          # enable local solver
  LOCAL_CAPTCHA_HEADLESS=true # headless or visible browser
  LOCAL_CAPTCHA_TIMEOUT=30    # page load timeout (seconds)
  LOCAL_CAPTCHA_RETRIES=3     # max retry attempts
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import sys
import threading
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

# ─── Stealth JS ─────────────────────────────────────────────────────────
_STEALTH_JS = """
(() => {
  // 1. Hide webdriver flag
  Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

  // 2. Realistic languages
  Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en']
  });

  // 3. Realistic plugins (mimic Chrome defaults)
  Object.defineProperty(navigator, 'plugins', {
    get: () => {
      const p = [
        {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
         description: 'Portable Document Format'},
        {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
         description: ''},
        {name: 'Native Client', filename: 'internal-nacl-plugin',
         description: ''},
      ];
      p.length = 3;
      return p;
    }
  });

  // 4. Chrome runtime object
  window.chrome = {
    runtime: {
      onConnect: {addListener: () => {}, removeListener: () => {}},
      onMessage: {addListener: () => {}, removeListener: () => {}},
      sendMessage: () => {},
      connect: () => ({onMessage: {addListener: () => {}}, postMessage: () => {}}),
    },
    loadTimes: () => ({
      requestTime: Date.now() / 1000 - Math.random() * 5,
      startLoadTime: Date.now() / 1000 - Math.random() * 3,
      commitLoadTime: Date.now() / 1000 - Math.random() * 2,
      finishDocumentLoadTime: Date.now() / 1000 - Math.random(),
      finishLoadTime: Date.now() / 1000,
      firstPaintTime: Date.now() / 1000 - Math.random() * 2,
      firstPaintAfterLoadTime: 0,
      navigationType: 'Other',
      wasFetchedViaSpdy: false,
      wasNpnNegotiated: true,
      npnNegotiatedProtocol: 'h2',
      wasAlternateProtocolAvailable: false,
      connectionInfo: 'h2',
    }),
    csi: () => ({
      onloadT: Date.now(),
      startE: Date.now() - Math.random() * 2000,
      pageT: Math.random() * 5000,
    }),
  };

  // 5. Spoof WebGL vendor/renderer
  const getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return 'Google Inc. (NVIDIA)';
    if (param === 37446) return 'ANGLE (NVIDIA, GeForce GTX 1650)';
    return getParameter.call(this, param);
  };

  // 6. Permissions API
  if (navigator.permissions) {
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (params) => {
      if (params.name === 'notifications') {
        return Promise.resolve({state: Notification.permission || 'prompt'});
      }
      return origQuery(params);
    };
  }

  // 7. Canvas fingerprint noise
  const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function(type) {
    if (type === 'image/png' || !type) {
      const ctx = this.getContext('2d');
      if (ctx) {
        try {
          const img = ctx.getImageData(0, 0, this.width, this.height);
          for (let i = 0; i < Math.min(img.data.length, 40); i += 4) {
            img.data[i] = img.data[i] ^ 1;
          }
          ctx.putImageData(img, 0, 0);
        } catch(e) {}
      }
    }
    return origToDataURL.apply(this, arguments);
  };

  // 8. Hardware info
  Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
  Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

  // 9. Connection info
  if (navigator.connection) {
    try {
      Object.defineProperty(navigator.connection, 'rtt', {get: () => 50});
      Object.defineProperty(navigator.connection, 'downlink', {get: () => 10});
      Object.defineProperty(navigator.connection, 'effectiveType', {get: () => '4g'});
    } catch(e) {}
  }
})();
"""

# ─── Token extraction JS ────────────────────────────────────────────────
_EXTRACT_TOKEN_JS = """
() => {
    // Method 1: hidden input fields
    const i = document.querySelector('[name="cf-turnstile-response"]')
        || document.querySelector('input[name*="turnstile"]');
    if (i && i.value && i.value.length > 20) return i.value;

    // Method 2: turnstile API with widget lookup
    try {
        if (window.turnstile) {
            const containers = document.querySelectorAll('[data-sitekey], .cf-turnstile');
            for (const el of containers) {
                try {
                    const r = window.turnstile.getResponse(el);
                    if (r && r.length > 20) return r;
                } catch(e) {}
            }
            try {
                const r = window.turnstile.getResponse();
                if (r && r.length > 20) return r;
            } catch(e) {}
        }
    } catch(e) {}

    return null;
}
"""

# ─── Constants ───────────────────────────────────────────────────────────
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_POLL_INTERVAL = 0.5       # seconds between token checks
_POLL_MAX_CHECKS = 40      # 0.5s * 40 = 20s max polling
_MOUSE_SETTLE = 0.3        # seconds after mouse movement
_CHECKBOX_TIMEOUT = 5000   # ms to wait for checkbox


class LocalTurnstileSolver:
    """Playwright-based local Turnstile solver with synchronous API.

    Thread-safe: each call to solve() runs its own asyncio event loop
    in the calling thread. Multiple threads can call solve() concurrently.

    Usage:
        solver = LocalTurnstileSolver(headless=True, timeout=30, retries=3)
        solver.start()    # launch browser (call once)
        token = solver.solve(website_url, website_key)  # blocking, returns str
        solver.stop()     # cleanup (call once)
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout: int = 30,
        retries: int = 3,
        debug: bool = False,
        on_progress: Optional[Any] = None,
    ):
        self._headless = headless
        self._timeout = timeout
        self._retries = retries
        self._debug = debug
        self._on_progress = on_progress

        # Playwright objects — managed in the async loop thread
        self._playwright = None
        self._browser = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._started = False
        self._lock = threading.Lock()

    def _progress(self, msg: str) -> None:
        if self._debug:
            print(f"  [local-captcha] {msg}")
        if self._on_progress:
            try:
                self._on_progress(msg)
            except Exception:
                raise

    # ── Lifecycle (sync) ─────────────────────────────────────────────

    def start(self) -> None:
        """Launch Playwright browser in a background event loop thread."""
        if self._started:
            return
        with self._lock:
            if self._started:
                return

            self._loop = asyncio.new_event_loop()
            self._loop_thread = threading.Thread(
                target=self._run_loop, daemon=True, name="local-captcha-loop"
            )
            self._loop_thread.start()

            # Wait for browser to start
            future = asyncio.run_coroutine_threadsafe(self._async_start(), self._loop)
            future.result(timeout=30)
            self._started = True
            self._progress("browser started")

    def stop(self) -> None:
        """Shutdown browser and event loop."""
        if not self._started:
            return
        with self._lock:
            if not self._started:
                return
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._async_stop(), self._loop
                )
                future.result(timeout=10)
            except Exception:
                pass
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
            self._started = False
            self._progress("browser stopped")

    def _run_loop(self) -> None:
        """Run the asyncio event loop in the background thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _async_start(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-default-apps",
                "--disable-background-networking",
                "--disable-sync",
                "--no-first-run",
            ],
        )

    async def _async_stop(self) -> None:
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass

    # ── Public API (sync, blocking) ──────────────────────────────────

    def solve(
        self,
        website_url: str,
        website_key: str,
        *,
        max_wait: float = 0,
    ) -> Optional[str]:
        """Solve a Turnstile challenge. Blocking, returns token or None.

        Args:
            website_url: Page URL with Turnstile widget
            website_key: Turnstile sitekey (0x4...)
            max_wait: Override total timeout (0 = use self._timeout * self._retries)

        Returns:
            Token string on success, None on failure
        """
        if not self._started:
            self.start()

        future = asyncio.run_coroutine_threadsafe(
            self._async_solve(website_url, website_key),
            self._loop,
        )
        total_timeout = max_wait or (self._timeout * self._retries + 30)
        try:
            token = future.result(timeout=total_timeout)
            return token
        except Exception as e:
            self._progress(f"solve failed: {e}")
            return None

    # ── Async internals ──────────────────────────────────────────────

    async def _async_solve(self, website_url: str, website_key: str) -> str:
        """Async solve with retries + exponential backoff."""
        last_error: Exception | None = None
        for attempt in range(self._retries):
            try:
                token = await self._solve_once(website_url, website_key)
                return token
            except Exception as exc:
                last_error = exc
                self._progress(
                    f"attempt {attempt + 1}/{self._retries} failed: {exc}"
                )
                if attempt < self._retries - 1:
                    backoff = min(2 ** attempt, 8)
                    await asyncio.sleep(backoff)

        raise RuntimeError(
            f"Local Turnstile failed after {self._retries} attempts: {last_error}"
        )

    async def _solve_once(self, website_url: str, website_key: str) -> str:
        """Single solve attempt using a fresh browser context."""
        assert self._browser is not None

        context = await self._browser.new_context(
            user_agent=_DEFAULT_UA,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            color_scheme="light",
            timezone_id="America/New_York",
        )
        page = await context.new_page()
        await page.add_init_script(_STEALTH_JS)

        try:
            timeout_ms = self._timeout * 1000

            # Navigate to target page
            await page.goto(
                website_url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            self._progress(f"page loaded: {website_url}")

            # Wait for Turnstile iframe to appear (up to 15s)
            try:
                await page.wait_for_selector(
                    'iframe[src*="challenges.cloudflare.com"], '
                    'iframe[src*="turnstile"]',
                    timeout=15_000,
                )
                self._progress("turnstile iframe detected")
            except Exception:
                self._progress("no turnstile iframe found after 15s, continuing")

            # Human-like mouse movement
            await self._human_mouse_move(page, 400, 300)
            await asyncio.sleep(_MOUSE_SETTLE)

            # Try clicking the Turnstile checkbox
            try:
                iframe_el = page.frame_locator(
                    'iframe[src*="challenges.cloudflare.com"], '
                    'iframe[src*="turnstile"]'
                )
                checkbox = iframe_el.locator(
                    'input[type="checkbox"], .ctp-checkbox-label, label'
                )
                await checkbox.click(timeout=_CHECKBOX_TIMEOUT)
                self._progress("checkbox clicked")
            except Exception:
                self._progress("no checkbox found, waiting for auto-solve")

            # Fast polling for token
            for i in range(_POLL_MAX_CHECKS):
                token = await page.evaluate(_EXTRACT_TOKEN_JS)
                if token:
                    self._progress(
                        f"token obtained (len={len(token)}) "
                        f"after {i * _POLL_INTERVAL:.1f}s"
                    )
                    return token
                await asyncio.sleep(_POLL_INTERVAL)

            # Debug: capture screenshot on failure
            await self._debug_capture(page)

            raise RuntimeError("Turnstile token not obtained within timeout")

        finally:
            await context.close()

    async def _debug_capture(self, page: Any) -> None:
        """Capture debug info on solve failure."""
        try:
            # Save screenshot
            debug_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "data",
            )
            os.makedirs(debug_dir, exist_ok=True)
            ts = int(time.time())
            path = os.path.join(debug_dir, f"captcha_debug_{ts}.png")
            await page.screenshot(path=path, full_page=True)
            self._progress(f"debug screenshot saved: {path}")

            # Log page state
            title = await page.title()
            url = page.url
            self._progress(f"page: title='{title}' url='{url}'")

            debug_info = await page.evaluate("""
                () => {
                    const iframes = document.querySelectorAll('iframe');
                    const inputs = document.querySelectorAll(
                        'input[name*="turnstile"], input[name*="cf-"]'
                    );
                    return {
                        iframeCount: iframes.length,
                        iframeSrcs: [...iframes].map(f => f.src).slice(0, 5),
                        inputCount: inputs.length,
                        hasTurnstileObj: !!window.turnstile,
                        bodySnippet: (document.body?.innerText || '').substring(0, 300),
                    };
                }
            """)
            self._progress(f"debug: {debug_info}")
        except Exception as e:
            self._progress(f"debug capture failed: {e}")

    @staticmethod
    async def _human_mouse_move(page: Any, target_x: int, target_y: int) -> None:
        """Move mouse along a Bézier curve — looks human to bot detection."""
        start_x = random.randint(0, 200)
        start_y = random.randint(0, 200)

        cp_x = (start_x + target_x) / 2 + random.randint(-80, 80)
        cp_y = (start_y + target_y) / 2 + random.randint(-80, 80)

        steps = random.randint(18, 30)
        for i in range(steps + 1):
            t = i / steps
            x = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * cp_x + t ** 2 * target_x
            y = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * cp_y + t ** 2 * target_y
            await page.mouse.move(x, y)

            base_delay = 0.008
            ease = math.sin(t * math.pi)
            await asyncio.sleep(base_delay + ease * 0.012 + random.uniform(0, 0.005))


# ─── Module-level singleton ─────────────────────────────────────────────
# Reused across threads to avoid launching multiple browsers.
_solver_instance: Optional[LocalTurnstileSolver] = None
_solver_lock = threading.Lock()


def get_local_solver(
    *,
    headless: bool = True,
    timeout: int = 30,
    retries: int = 3,
    debug: bool = False,
) -> LocalTurnstileSolver:
    """Get or create the module-level LocalTurnstileSolver singleton."""
    global _solver_instance
    if _solver_instance is not None and _solver_instance._started:
        return _solver_instance
    with _solver_lock:
        if _solver_instance is not None and _solver_instance._started:
            return _solver_instance
        _solver_instance = LocalTurnstileSolver(
            headless=headless,
            timeout=timeout,
            retries=retries,
            debug=debug,
        )
        _solver_instance.start()
        return _solver_instance


def solve_turnstile_local(
    website_url: str,
    website_key: str,
    *,
    headless: bool = True,
    timeout: int = 30,
    retries: int = 3,
    debug: bool = False,
    max_wait: float = 0,
) -> Optional[str]:
    """Convenience function: solve Turnstile locally via Playwright.

    Returns token string on success, None on failure.
    Thread-safe — uses a shared browser singleton.
    """
    try:
        solver = get_local_solver(
            headless=headless,
            timeout=timeout,
            retries=retries,
            debug=debug,
        )
        return solver.solve(website_url, website_key, max_wait=max_wait)
    except Exception as e:
        if debug:
            print(f"  [local-captcha] error: {e}")
        return None
