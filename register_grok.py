# -*- coding: utf-8 -*-
"""
Grok (x.ai) Auto Register + Export
Tự động đăng ký tài khoản Grok và xuất email|refresh_token

Usage:
    python register_grok.py --count 5
    python register_grok.py --count 10 --workers 3
"""

import argparse
import builtins
import json
import os
import random
import sys
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

import requests

from xconsole_client import XConsoleAuthClient, config as C

from common import proxy_switch
from common.temp_email import create_mailbox, _scan_once
from common.session_export import save_grok_token, save_grok_full_account
from common.token_upload_state import mark_uploaded

try:
    from config import (
        YESCAPTCHA_API_KEY,
        YESCAPTCHA_API_BASE,
        CAPSOLVER_API_KEY,
        EZCAPTCHA_API_KEY,
        EZCAPTCHA_API_BASE,
        LOCAL_CAPTCHA,
        LOCAL_CAPTCHA_HEADLESS,
        LOCAL_CAPTCHA_TIMEOUT,
        LOCAL_CAPTCHA_RETRIES,
        SUB2API_URL,
        SUB2API_EMAIL,
        SUB2API_PASSWORD,
        SUB2API_GROK_GROUP,
        SUB2API_GROK_PROXY_ID,
    )
except Exception:
    YESCAPTCHA_API_KEY = ""
    YESCAPTCHA_API_BASE = "https://api.yescaptcha.com"
    CAPSOLVER_API_KEY = ""
    EZCAPTCHA_API_KEY = ""
    EZCAPTCHA_API_BASE = "https://api.ez-captcha.com"
    LOCAL_CAPTCHA = False
    LOCAL_CAPTCHA_HEADLESS = True
    LOCAL_CAPTCHA_TIMEOUT = 30
    LOCAL_CAPTCHA_RETRIES = 3
    SUB2API_URL = ""
    SUB2API_EMAIL = ""
    SUB2API_PASSWORD = ""
    SUB2API_GROK_GROUP = "grok"
    SUB2API_GROK_PROXY_ID = 0

try:
    from config import TEMP_EMAIL_PROVIDER
except Exception:
    TEMP_EMAIL_PROVIDER = "yyds"

PROVIDER = TEMP_EMAIL_PROVIDER
PLATFORM = "grok"
CLASH_PROXY = proxy_switch.effective_proxy_url()
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

GROK_SENDER = ("x.ai", "grok", "noreply", "no-reply")
GROK_SUBJECT = ("code", "verify", "verification", "grok", "x.ai", "confirm",
                "確認", "認証", "コード", "验证", "驗證")
CODE_REGEX = r"\b((?=[A-Z0-9-]*[A-Z])(?:[A-Z0-9]{2,4}-[A-Z0-9]{2,4}|[A-Z0-9]{6}))\b"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

_io_lock = threading.Lock()
_builtin_print = builtins.print


def _safe_print(*args, **kwargs):
    with _io_lock:
        _builtin_print(*args, **kwargs)


def _tprint(*args, **kwargs):
    _safe_print(*args, **kwargs)


_captcha_semaphore = threading.Semaphore(4)


class ExportWriter:
    def __init__(self, count):
        os.makedirs(DATA_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._filename = f"grok_{ts}_{count}.txt"
        self._filepath = os.path.join(DATA_DIR, self._filename)
        self._lock = threading.Lock()
        self._written = 0
        with open(self._filepath, "w", encoding="utf-8") as f:
            pass
        _builtin_print(f"  [export] Output: {self._filepath}")

    @property
    def filepath(self):
        return self._filepath

    @property
    def written(self):
        return self._written

    def append(self, email, refresh_token):
        line = f"{email}|{refresh_token}\n"
        with self._lock:
            with open(self._filepath, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
            self._written += 1
        return self._written


def _rand_password():
    return "Pw" + os.urandom(6).hex() + "!a#A"


def _rand_name():
    import random
    import string
    w = random.choice("BCDFGHJKLMNPQRST") + random.choice("aeiou") + \
        "".join(random.choices(string.ascii_lowercase, k=4))
    return w.capitalize()


def solve_turnstile(sitekey, page_url, action=None, cdata=None, max_wait=140):
    # ── 1. Local solver (Playwright, miễn phí) ──
    if LOCAL_CAPTCHA:
        try:
            from common.local_turnstile import solve_turnstile_local
            print(f"  [local-captcha] solving Turnstile (headless={LOCAL_CAPTCHA_HEADLESS})...")
            token = solve_turnstile_local(
                website_url=page_url,
                website_key=sitekey,
                headless=LOCAL_CAPTCHA_HEADLESS,
                timeout=LOCAL_CAPTCHA_TIMEOUT,
                retries=LOCAL_CAPTCHA_RETRIES,
                debug=True,
                max_wait=max_wait,
            )
            if token:
                print(f"  [local-captcha] solved (token len={len(token)})")
                return token
            print("  [local-captcha] failed, trying API solvers...")
        except Exception as e:
            print(f"  [local-captcha] error: {str(e)[:120]}")
            print("  [local-captcha] falling back to API solvers...")

    # ── 2. YesCaptcha (API, trả phí) ──
    if YESCAPTCHA_API_KEY:
        try:
            from xconsole_client.solver import YesCaptchaSolver
            solver = YesCaptchaSolver(
                YESCAPTCHA_API_KEY,
                endpoint=YESCAPTCHA_API_BASE,
                timeout=max_wait,
                poll_interval=3,
                debug=False,
            )
            token = solver.solve_turnstile(
                website_url=page_url,
                website_key=sitekey,
                premium=True,
                fallback_non_premium=True,
            )
            if token:
                print(f"  [yescaptcha] solved (token len={len(token)})")
                return token
        except Exception as e:
            print(f"  [yescaptcha] error: {str(e)[:80]}")
    if CAPSOLVER_API_KEY:
        try:
            task = {"type": "AntiTurnstileTaskProxyLess", "websiteURL": page_url, "websiteKey": sitekey}
            meta = {}
            if action:
                meta["action"] = action
            if cdata:
                meta["cdata"] = cdata
            if meta:
                task["metadata"] = meta
            resp = requests.post("https://api.capsolver.com/createTask",
                                 json={"clientKey": CAPSOLVER_API_KEY, "task": task}, timeout=30)
            data = resp.json()
            if data.get("errorId", 1) == 0:
                task_id = data["taskId"]
                print(f"  [capsolver] turnstile task: {task_id}")
                start = time.time()
                while time.time() - start < max_wait:
                    time.sleep(5)
                    r = requests.post("https://api.capsolver.com/getTaskResult",
                                      json={"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}, timeout=30).json()
                    st = r.get("status")
                    if st == "ready":
                        tok = r.get("solution", {}).get("token")
                        print(f"  [capsolver] solved (token len={len(tok or '')})")
                        return tok
                    if st == "failed" or r.get("errorId"):
                        print(f"  [capsolver] failed: {r.get('errorDescription', '')}")
                        break
            else:
                print(f"  [capsolver] create error: {data.get('errorDescription', data)}")
        except Exception as e:
            print(f"  [capsolver] error: {str(e)[:80]}")
    if EZCAPTCHA_API_KEY:
        try:
            resp = requests.post(f"{EZCAPTCHA_API_BASE}/createTask", json={
                "clientKey": EZCAPTCHA_API_KEY,
                "task": {"type": "TurnstileTaskProxyless", "websiteURL": page_url, "websiteKey": sitekey},
            }, timeout=30)
            data = resp.json()
            if data.get("errorId", 1) == 0:
                task_id = data["taskId"]
                print(f"  [ezcaptcha] turnstile task: {task_id}")
                start = time.time()
                while time.time() - start < max_wait:
                    time.sleep(5)
                    r = requests.post(f"{EZCAPTCHA_API_BASE}/getTaskResult",
                                      json={"clientKey": EZCAPTCHA_API_KEY, "taskId": task_id}, timeout=30).json()
                    st = r.get("status")
                    if st == "ready":
                        tok = r.get("solution", {}).get("token")
                        print(f"  [ezcaptcha] solved (token len={len(tok or '')})")
                        return tok
                    if st == "failed" or r.get("errorId"):
                        print(f"  [ezcaptcha] failed: {r.get('errorDescription', '')}")
                        break
            else:
                print(f"  [ezcaptcha] create error: {data.get('errorDescription', data)}")
        except Exception as e:
            print(f"  [ezcaptcha] error: {str(e)[:80]}")
    return None


def poll_code_sync(mb, max_wait=150, poll=5):
    start = time.time()
    while time.time() - start < max_wait:
        code = _scan_once(mb["id"], mb["provider"], mb["email"], mb.get("token"),
                          None, None, GROK_SENDER, GROK_SUBJECT, CODE_REGEX)
        if code:
            print(f"  [temp-email] code found: {code}")
            return code
        print(f"  [temp-email] waiting for code... ({int(time.time()-start)}s/{max_wait}s)")
        time.sleep(poll)
    print("  [temp-email] timeout, no code")
    return None


def create_mailbox_retry(provider, tries=4):
    last = None
    for i in range(tries):
        try:
            return create_mailbox(provider=provider)
        except Exception as e:
            last = e
            print(f"  [temp-email] fail (try {i+1}/{tries}): {str(e)[:70]}")
            time.sleep(2)
    raise RuntimeError(f"Mailbox creation failed: {str(last)[:100]}")


def _read_refresh_token(email, retries=3, delay=2):
    try:
        from config import TOKEN_OUTPUT_DIR
    except Exception:
        TOKEN_OUTPUT_DIR = "tokens"

    auth_path = os.path.join(TOKEN_OUTPUT_DIR, "grok", email, "auth.json")
    for attempt in range(retries):
        if not os.path.isfile(auth_path):
            if attempt < retries - 1:
                time.sleep(delay)
                continue
            return None
        try:
            with open(auth_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for _key, entry in data.items():
                if isinstance(entry, dict) and entry.get("refresh_token"):
                    return entry["refresh_token"]
        except (json.JSONDecodeError, OSError):
            pass
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def _try_export_account(email, writer):
    rt = _read_refresh_token(email, retries=4, delay=3)
    if rt:
        n = writer.append(email, rt)
        _tprint(f"  [export] #{n} saved: {email} | {rt[:30]}...")
        return True
    else:
        _tprint(f"  [export] WARNING: no refresh_token for {email}")
        return False


def register_one(index, total, sub2api=False, sub2api_group="", mailbox_attempts=6,
                 code_timeout=75, proxy_url=None, debug=True, tag=""):
    email = ""
    print(f"\n{tag} #{index}/{total}" if tag else f"\n#{index}/{total}")
    active_proxy = proxy_url or proxy_switch.effective_proxy_url()
    c = XConsoleAuthClient(debug=debug, proxy=active_proxy, signup_url=SIGNUP_URL,
                           impersonate="chrome131", timeout=40.0)
    try:
        st = c.visit_home()
        print(f"  [1] visit grok home HTTP {st}")
        st = c.load_signup_page()
        print(f"  [2] load signup page HTTP {st}  sitekey={c.turnstile_sitekey}")
        sitekey = c.turnstile_sitekey or C.TURNSTILE_SITEKEY

        mb = None
        r = None
        code = None
        password = _rand_password()
        for mailbox_try in range(1, max(1, mailbox_attempts) + 1):
            try:
                candidate = create_mailbox_retry(PROVIDER, tries=2)
            except Exception as e:
                print(f"  [temp-email] mailbox {mailbox_try}/{mailbox_attempts} failed: {str(e)[:90]}")
                continue
            email = candidate["email"]
            print(f"  [3] temp mailbox {mailbox_try}/{mailbox_attempts}: {email} ({candidate['provider']})")
            r = c.create_email_validation_code(email)
            print(f"  [4] CreateEmailValidationCode ok={r.ok} http={r.http_status} grpc={r.grpc_status}")
            if r.ok:
                candidate_code = poll_code_sync(
                    candidate, max_wait=max(15, code_timeout), poll=5
                )
                if candidate_code:
                    mb = candidate
                    code = candidate_code
                    break
                print("  [temp-email] code sent but not received, switching mailbox")
                continue
            print(f"  [temp-email] domain rejected, switching: {r.trailers}")
            time.sleep(1)
        if not mb or not code:
            print(f"  [FAIL] no code after {mailbox_attempts} mailboxes")
            return None

        v = c.verify_email_validation_code(email, code)
        print(f"  [5] VerifyEmailValidationCode ok={v.ok} grpc={v.grpc_status}")
        if not v.ok:
            alt = code.replace("-", "").replace(" ", "")
            if alt != code:
                v = c.verify_email_validation_code(email, alt)
                print(f"  [5] retry(no separator {alt}) ok={v.ok} grpc={v.grpc_status}")
        if not v.ok:
            print(f"  [FAIL] code verification failed: {v.trailers}")
            return None

        try:
            c.validate_password(email, password)
        except Exception as e:
            print(f"  [6] validate_password skipped: {str(e)[:50]}")

        print(f"  [7] solving Turnstile (sitekey={sitekey})")
        _captcha_semaphore.acquire()
        try:
            turnstile = solve_turnstile(sitekey, SIGNUP_URL)
        finally:
            _captcha_semaphore.release()
        if not turnstile:
            print("  [FAIL] Turnstile solve failed")
            return None

        first, last = _rand_name(), _rand_name()
        res = c.create_account(
            email=email, given_name=first, family_name=last,
            password=password, email_validation_code=code,
            turnstile_token=turnstile, castle_request_token="",
            conversion_id=str(uuid.uuid4()),
        )
        print(f"  [8] create_account ok={res.ok} http={res.http_status}")
        if not res.ok:
            err = c.extract_signup_error(res.rsc_body)
            print(f"  [FAIL] account creation failed: {err}")
            return None

        sso = c.fetch_sso_token(email=email, password=password, save=False, retries=4)
        if not sso:
            print("  [7] RSC no sso, fallback to CreateSession password login")
            _captcha_semaphore.acquire()
            try:
                turnstile2 = solve_turnstile(sitekey, C.SIGNIN_URL) or turnstile
            finally:
                _captcha_semaphore.release()
            sso = c.obtain_session_via_password(
                email=email, password=password, turnstile_token=turnstile2, retries=3)
        if not sso:
            print("  [FAIL] account created but no sso token")
            return None

        save_grok_full_account(sso, email=email, password=password)
        print(f"  [OK] grok full account saved  email={email} pw={password}")
        if sub2api:
            from common.uploaders import upload_sub2api_grok

            ok, msg = upload_sub2api_grok(
                SUB2API_URL,
                SUB2API_EMAIL,
                SUB2API_PASSWORD,
                sub2api_group or SUB2API_GROK_GROUP,
                sso,
                account_email=email,
                proxy_id=SUB2API_GROK_PROXY_ID,
                local_proxy=proxy_switch.effective_proxy_url(),
            )
            print(f"  [{'OK' if ok else 'FAIL'}] {msg}")
            if not ok:
                print("  [hint] SSO saved, can retry: python tools/upload_tokens.py grok")
                return None
            try:
                mark_uploaded("grok", "sub2api", email)
            except Exception as e:
                print(f"  [warn] SUB2API imported but mark failed: {e}")

        return {"email": email, "sso": sso}

    except Exception as e:
        print(f"  ERROR: {e}")
        return None
    finally:
        c.close()


def main():
    parser = argparse.ArgumentParser(
        description="Grok Auto Register + Export email|refresh_token")
    parser.add_argument("--count", "-n", type=int, default=1)
    parser.add_argument("--workers", "-w", type=int, default=1)
    parser.add_argument("--provider", default="")
    parser.add_argument("--sub2api", action="store_true")
    parser.add_argument("--sub2api-group", default="")
    parser.add_argument("--mailbox-attempts", type=int, default=6)
    parser.add_argument("--code-timeout", type=int, default=75)
    args = parser.parse_args()

    if args.sub2api and not (SUB2API_URL and SUB2API_EMAIL and SUB2API_PASSWORD):
        parser.error("--sub2api requires SUB2API_URL/SUB2API_EMAIL/SUB2API_PASSWORD")

    workers = max(1, args.workers)

    global PROVIDER
    if args.provider.strip():
        PROVIDER = args.provider.strip()
    print(f"  Temp email provider: {PROVIDER}")

    print("=" * 50)
    print(f"  Grok Auto Register  count={args.count} workers={workers}")
    print("=" * 50)

    results = []
    lock = threading.Lock()
    writer = ExportWriter(args.count)

    if workers <= 1:
        for i in range(1, args.count + 1):
            try:
                result = register_one(
                    i, args.count, args.sub2api, args.sub2api_group,
                    args.mailbox_attempts, args.code_timeout,
                )
                results.append(result)

                if result and result.get("email"):
                    _try_export_account(result["email"], writer)
            except Exception as e:
                print(f"  #{i} fatal: {e}")
                results.append(None)
    else:
        builtins.print = _safe_print
        try:
            _tprint(f"\n  Multi-threaded: {workers} workers\n")
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = []
                for i in range(1, args.count + 1):
                    f = executor.submit(
                        lambda idx: register_one(idx, args.count, args.sub2api, args.sub2api_group,
                                                  args.mailbox_attempts, args.code_timeout, debug=False),
                        i
                    )
                    futures.append(f)
                for f in as_completed(futures):
                    try:
                        result = f.result()
                        results.append(result)
                        if result and result.get("email"):
                            _try_export_account(result["email"], writer)
                    except Exception as e:
                        _tprint(f"  [thread] error: {e}")
                        results.append(None)
        finally:
            builtins.print = _builtin_print

    ok = sum(1 for r in results if r)
    print(f"\n{'='*50}\n  success: {ok}/{len(results)}\n{'='*50}")
    print(f"  [export] total written: {writer.written} accounts -> {writer.filepath}")

    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
