# -*- coding: utf-8 -*-
"""
config.py - Cấu hình cho Grok Auto Register
Tất cả key đều đọc từ .env hoặc biến môi trường
"""

import os


def _load_dotenv(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


_load_dotenv()


def _env(name, default=""):
    return os.environ.get(name, default)


def _env_int(name, default):
    try:
        return int(_env(name, str(default)) or default)
    except (TypeError, ValueError):
        return int(default)


# ---- Captcha solver (chọn 1 trong 3) ----
YESCAPTCHA_API_KEY = _env("YESCAPTCHA_API_KEY", "")
YESCAPTCHA_API_BASE = _env("YESCAPTCHA_API_BASE", "https://api.yescaptcha.com")

CAPSOLVER_API_KEY = _env("CAPSOLVER_API_KEY", "")

EZCAPTCHA_API_KEY = _env("EZCAPTCHA_API_KEY", "")
EZCAPTCHA_API_BASE = _env("EZCAPTCHA_API_BASE", "https://api.ez-captcha.com")

# ---- Local Captcha Solver (Playwright, miễn phí, không cần API key) ----
LOCAL_CAPTCHA = _env("LOCAL_CAPTCHA", "false").strip().lower() in ("1", "true", "yes")
LOCAL_CAPTCHA_HEADLESS = _env("LOCAL_CAPTCHA_HEADLESS", "true").strip().lower() in ("1", "true", "yes")
LOCAL_CAPTCHA_TIMEOUT = _env_int("LOCAL_CAPTCHA_TIMEOUT", 30)
LOCAL_CAPTCHA_RETRIES = _env_int("LOCAL_CAPTCHA_RETRIES", 3)

# ---- Temp email provider ----
# Chọn: yyds | gptmail | moemail | cfmail
TEMP_EMAIL_PROVIDER = _env("TEMP_EMAIL_PROVIDER", "gptmail").strip().lower() or "gptmail"

# YYDS Mail
YYDS_BASE_URL = _env("YYDS_BASE_URL", "https://maliapi.215.im")
YYDS_API_KEY = _env("YYDS_API_KEY", "")

# GPTMail (có key test mặc định "gpt-test")
GPTMAIL_BASE_URL = _env("GPTMAIL_BASE_URL", "https://mail.chatgpt.org.uk")
GPTMAIL_API_KEY = _env("GPTMAIL_API_KEY", "gpt-test")

# MoeMail
MOEMAIL_BASE_URL = _env("MOEMAIL_BASE_URL", "")
MOEMAIL_API_KEY = _env("MOEMAIL_API_KEY", "")

# Cloudflare Temp Email
CFMAIL_BASE_URL = _env("CFMAIL_BASE_URL", "")
CFMAIL_ADMIN_PASSWORD = _env("CFMAIL_ADMIN_PASSWORD", "")

# ---- Proxy ----
# Clash/V2Ray HTTP proxy (ví dụ: http://127.0.0.1:7890)
CLASH_PROXY = _env("CLASH_PROXY", "")
PROXY_MODE = _env("PROXY_MODE", "clash").strip().lower()

# ---- Token output ----
TOKEN_OUTPUT_DIR = _env("TOKEN_OUTPUT_DIR", "tokens")

# ---- SUB2API (tùy chọn, để upload token sau khi đăng ký) ----
SUB2API_URL = _env("SUB2API_URL", "")
SUB2API_EMAIL = _env("SUB2API_EMAIL", "")
SUB2API_PASSWORD = _env("SUB2API_PASSWORD", "")
SUB2API_GROK_GROUP = _env("SUB2API_GROK_GROUP", "grok")
SUB2API_GROK_PROXY_ID = int(_env("SUB2API_GROK_PROXY_ID", "0") or "0")
