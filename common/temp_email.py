# -*- coding: utf-8 -*-
"""
common/temp_email.py — 临时邮箱统一接口（纯 HTTP API 取验证码）

参考 grokcli-2api（HM2899/grokcli-2api）：用临时邮箱 HTTP API 直接拉验证码，
免去 Outlook 浏览器登录 + 收件箱轮询的重开销，取码从 ~80-120s 降到 ~10-30s。

支持 5 个 provider：
  - moemail : beilunyang/moemail（自部署）           X-API-Key
  - yyds    : YYDS Mail (vip.215.im / maliapi.215.im) X-API-Key(AC-...) / Bearer token
  - gptmail : mail.chatgpt.org.uk（含公共测试 key）   X-API-Key(gpt-test)
  - cfmail  : dreamhunter2333/cloudflare_temp_email   x-admin-auth / Bearer jwt
  - icloud  : mail.no-replyca.xyz（iCloud / iCloud code） URL query apikey

统一接口：
  create_mailbox(provider=...) -> {"id","email","token","provider","raw"}
  fetch_messages(mailbox, ...) -> [{...,"extracted":{"codes":[],"links":[]}}]
  await poll_verification_code(mailbox, ...) -> code 字符串 或 None

用 requests（项目已有依赖），异步轮询用 run_in_executor 包一层，与 register_grok
里的 turnstile 打码调用同款模式，不引入 httpx 新依赖。
"""

import asyncio
import email as email_parser
import json
import random
import re
import string
import sys
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import requests

try:
    from config import (
        TEMP_EMAIL_PROVIDER,
        ICLOUD_MAIL_API_BASE, ICLOUD_MAIL_API_KEY, ICLOUD_MAIL_TYPE, ICLOUD_MAIL_SERVICE,
        MOEMAIL_BASE_URL, MOEMAIL_API_KEY, MOEMAIL_DOMAIN, MOEMAIL_EXPIRY_MS,
        YYDS_BASE_URL, YYDS_API_KEY,
        GPTMAIL_BASE_URL, GPTMAIL_API_KEY,
        CFMAIL_BASE_URL, CFMAIL_ADMIN_PASSWORD, CFMAIL_SITE_PASSWORD,
        CUSTOM_MAIL_BASE_URL, CUSTOM_MAIL_AUTH_HEADER, CUSTOM_MAIL_API_KEY,
        CUSTOM_MAIL_AUTH_PREFIX, CUSTOM_MAIL_CREATE_METHOD, CUSTOM_MAIL_CREATE_PATH,
        CUSTOM_MAIL_CREATE_BODY, CUSTOM_MAIL_EMAIL_PATH, CUSTOM_MAIL_ID_PATH,
        CUSTOM_MAIL_TOKEN_PATH, CUSTOM_MAIL_FETCH_METHOD, CUSTOM_MAIL_FETCH_PATH,
        CUSTOM_MAIL_FETCH_AUTH, CUSTOM_MAIL_LIST_PATH, CUSTOM_MAIL_DETAIL_PATH,
        CUSTOM_MAIL_MSG_ID_PATH, CUSTOM_MAIL_MSG_PATH,
    )
except Exception:  # pragma: no cover - config 缺失时的兜底默认
    TEMP_EMAIL_PROVIDER = "gptmail"
    ICLOUD_MAIL_API_BASE = "https://mail.no-replyca.xyz"
    ICLOUD_MAIL_API_KEY = ICLOUD_MAIL_TYPE = ICLOUD_MAIL_SERVICE = ""
    MOEMAIL_BASE_URL = "https://moemail.example.com"
    MOEMAIL_API_KEY = MOEMAIL_DOMAIN = ""
    MOEMAIL_EXPIRY_MS = 3600000
    YYDS_BASE_URL = "https://maliapi.215.im"
    YYDS_API_KEY = ""
    GPTMAIL_BASE_URL = "https://mail.chatgpt.org.uk"
    GPTMAIL_API_KEY = "gpt-test"
    CFMAIL_BASE_URL = "https://temp-email-api.awsl.uk"
    CFMAIL_ADMIN_PASSWORD = CFMAIL_SITE_PASSWORD = ""
    CUSTOM_MAIL_BASE_URL = CUSTOM_MAIL_AUTH_HEADER = CUSTOM_MAIL_API_KEY = ""
    CUSTOM_MAIL_AUTH_PREFIX = CUSTOM_MAIL_CREATE_PATH = CUSTOM_MAIL_CREATE_BODY = ""
    CUSTOM_MAIL_CREATE_METHOD = "POST"
    CUSTOM_MAIL_EMAIL_PATH = "email"
    CUSTOM_MAIL_ID_PATH = CUSTOM_MAIL_TOKEN_PATH = ""
    CUSTOM_MAIL_FETCH_METHOD = "GET"
    CUSTOM_MAIL_FETCH_PATH = CUSTOM_MAIL_LIST_PATH = CUSTOM_MAIL_DETAIL_PATH = ""
    CUSTOM_MAIL_FETCH_AUTH = "key"
    CUSTOM_MAIL_MSG_ID_PATH = "id"
    CUSTOM_MAIL_MSG_PATH = ""

HTTP_TIMEOUT = 30
# 临时邮箱端点在国外，走本机代理反而不稳；这里默认直连（trust_env=False 绕 HTTP(S)_PROXY），
# 与 common/mailbox.py 打 MS 端点同款策略。个别 provider 需代理可在 create/fetch 传 session。
GPTMAIL_PUBLIC_TEST_KEY = "gpt-test"

# 验证码：6-8 位纯数字，前后不粘连其它数字（避开长串里的片段）。
_CODE_RE = re.compile(r"(?<!\d)(\d{6,8})(?!\d)")
# 带分隔符的 code（如 x.ai 的 ABCD-1234 风格）——grok 邮件里出现过
_DASH_CODE_RE = re.compile(r"\b((?=[A-Z0-9-]*[A-Z])[A-Z0-9]{2,4}-[A-Z0-9]{2,4})\b")
_LINK_RE = re.compile(r"https?://[^\s\"'<>)]+")


def _session():
    """直连 session（忽略环境代理），临时邮箱端点直连更稳。"""
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    return s


def _strip_html(text):
    """去 HTML 标签（含 style/script 块），避免命中 inline CSS 的 #202123 等伪码。
    某些 provider 的 html/content 字段可能是 list/dict（多段正文），统一转成字符串再处理。"""
    if not text:
        return ""
    if not isinstance(text, str):
        # list（多段正文）拼接；其它类型直接 str()
        text = " ".join(str(x) for x in text) if isinstance(text, (list, tuple)) else str(text)
    text = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return text


def _rand_local(n=10):
    """随机 localPart（cfmail 需要显式给 name）。"""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _json_path(obj, path):
    """迷你 JSON 路径解析：支持 a.b.c、a.b[0].c、[0].x，空串=根。找不到返回 None。
    给 custom provider 从任意响应结构里抠字段用（零三方依赖）。"""
    if path is None or path == "":
        return obj
    cur = obj
    # 把 [i] 归一成 .i，再按 . 切；数字段当下标，其它当键
    for token in re.split(r"\.", re.sub(r"\[(\d+)\]", r".\1", path)):
        if token == "":
            continue
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(token)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(token)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _fill_tpl(s, ctx):
    """把字符串里的 {key} 用 ctx[key] 替换；缺失的占位符原样保留（不炸）。"""
    if not s:
        return s
    def _sub(m):
        k = m.group(1)
        return str(ctx.get(k)) if ctx.get(k) is not None else m.group(0)
    return re.sub(r"\{(\w+)\}", _sub, s)


def _custom_headers(use_token=False, token=None, api_key=None):
    """按 CUSTOM_MAIL_AUTH_HEADER/PREFIX/API_KEY 组鉴权头；use_token=True 时用建号返回的 token。
    取信 CUSTOM_MAIL_FETCH_AUTH=token 时，用 Authorization: Bearer <token>（除非另配了 AUTH_HEADER）。
    api_key 显式传入时覆盖 CUSTOM_MAIL_API_KEY（多 provider 场景一般不用，留作扩展）。"""
    headers = {}
    key = api_key or CUSTOM_MAIL_API_KEY
    if use_token and token:
        hdr = CUSTOM_MAIL_AUTH_HEADER or "Authorization"
        prefix = CUSTOM_MAIL_AUTH_PREFIX or ("Bearer " if hdr.lower() == "authorization" else "")
        headers[hdr] = f"{prefix}{token}"
    elif CUSTOM_MAIL_AUTH_HEADER and key:
        headers[CUSTOM_MAIL_AUTH_HEADER] = f"{CUSTOM_MAIL_AUTH_PREFIX}{key}"
    return headers


def _extract_codes_and_links(text):
    """从一段文本里抽验证码 + 链接，返回 {"codes": [...], "links": [...]}。
    先剥 HTML 再匹配，数字码 + 带分隔符码都收。"""
    clean = _strip_html(text or "")
    codes = []
    for m in _DASH_CODE_RE.findall(text or ""):
        if m not in codes:
            codes.append(m)
    for m in _CODE_RE.findall(clean):
        if m not in codes:
            codes.append(m)
    links = []
    for l in _LINK_RE.findall(text or ""):
        if l not in links:
            links.append(l)
    return {"codes": codes, "links": links}


def normalize_provider(provider=None, base_url=""):
    """归一化 provider 名：moemail | yyds | gptmail | cfmail | icloud | custom。
    provider 显式给出优先；否则从 base_url 域名特征推断；再兜底 config 默认。"""
    p = (provider or "").strip().lower()
    if p in ("moemail", "yyds", "gptmail", "cfmail", "icloud", "custom"):
        return p
    b = (base_url or "").lower()
    # base_url 命中自定义 API 根地址 → custom
    if CUSTOM_MAIL_BASE_URL and CUSTOM_MAIL_BASE_URL.lower().rstrip("/") in b:
        return "custom"
    if "215.im" in b:
        return "yyds"
    if "chatgpt.org.uk" in b:
        return "gptmail"
    if "moemail" in b:
        return "moemail"
    if "temp-email" in b or "awsl" in b:
        return "cfmail"
    if "no-replyca" in b or "manageh.shop" in b:
        return "icloud"
    dflt = (TEMP_EMAIL_PROVIDER or "gptmail").strip().lower()
    return dflt if dflt in ("moemail", "yyds", "gptmail", "cfmail", "icloud") else "gptmail"


def _norm_base(base_url, default):
    b = (base_url or default or "").strip().rstrip("/")
    if b and not b.startswith("http"):
        b = "https://" + b
    return b


def _norm_yyds_base(base_url=None):
    """Accept the YYDS API root, marketing URL, or a pasted v1 endpoint."""
    base = _norm_base(base_url, YYDS_BASE_URL)
    parsed = urlsplit(base)
    host = (parsed.hostname or "").lower()
    netloc = parsed.netloc
    if host in {"vip.215.im", "www.vip.215.im"}:
        netloc = "maliapi.215.im"

    path = parsed.path.rstrip("/")
    for suffix in ("/v1/accounts", "/v1/domains", "/v1/messages", "/v1"):
        if path.lower().endswith(suffix):
            path = path[:-len(suffix)].rstrip("/")
            break
    return urlunsplit((parsed.scheme or "https", netloc, path, "", "")).rstrip("/")


# ==================================================================== MoeMail
def _moemail_create(name, domain, expiry_ms, api_key, base_url, sess):
    key = (api_key or MOEMAIL_API_KEY or "").strip()
    if not key:
        raise ValueError("MoeMail 需要 API key（MOEMAIL_API_KEY）")
    base = _norm_base(base_url, MOEMAIL_BASE_URL)
    official = {3600000, 86400000, 259200000, 0}
    chosen = int(MOEMAIL_EXPIRY_MS if expiry_ms is None else expiry_ms)
    if chosen not in official:
        chosen = min((3600000, 86400000, 259200000), key=lambda x: abs(x - chosen))
    payload = {"expiryTime": chosen, "domain": domain or MOEMAIL_DOMAIN}
    if name:
        payload["name"] = name
    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    r = sess.post(f"{base}/api/emails/generate", json=payload, headers=headers, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"MoeMail create {r.status_code}: {r.text[:200]}")
    data = r.json()
    eid = data.get("id") or data.get("emailId")
    addr = data.get("email") or data.get("address")
    if not eid or not addr:
        raise RuntimeError(f"MoeMail create 返回异常: {data}")
    return {"id": str(eid), "email": str(addr), "token": "", "provider": "moemail", "raw": data}


def _moemail_fetch(mailbox_id, api_key, base_url, sess):
    key = (api_key or MOEMAIL_API_KEY or "").strip()
    base = _norm_base(base_url, MOEMAIL_BASE_URL)
    headers = {"X-API-Key": key}
    r = sess.get(f"{base}/api/emails/{mailbox_id}", headers=headers, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        return []
    msgs = (r.json() or {}).get("messages") or []
    out = []
    for raw in msgs[:20]:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        mid = item.get("id") or item.get("messageId")
        if mid:
            d = sess.get(f"{base}/api/emails/{mailbox_id}/{mid}", headers=headers, timeout=HTTP_TIMEOUT)
            if d.status_code == 200:
                msg = (d.json() or {}).get("message")
                if isinstance(msg, dict):
                    item.update(msg)
        out.append(item)
    return out


# ==================================================================== YYDS Mail
def _yyds_pick_domain(key, base, sess, exclude=()):
    base = _norm_yyds_base(base)
    try:
        r = sess.get(f"{base}/v1/domains", headers={"X-API-Key": key}, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            return None
        data = r.json()
        body = data.get("data") if isinstance(data, dict) and "data" in data else data
        doms = body.get("domains") if isinstance(body, dict) else body
        if not isinstance(doms, list):
            return None
        ok = []
        for d in doms:
            if not isinstance(d, dict):
                continue
            name = d.get("domain") or d.get("name")
            if not name:
                continue
            dns = d.get("dnsRecords") if isinstance(d.get("dnsRecords"), dict) else {}
            if d.get("public") is False or d.get("isPublic") is False:
                continue
            if d.get("ready") is False or d.get("isVerified") is False:
                continue
            if d.get("isMxValid") is False or dns.get("receivingReady") is False:
                continue
            wildcard = dns.get("wildcardMxValid", d.get("wildcardMxValid"))
            ok.append((name, bool(wildcard)))
        if not ok:
            return None
        excluded = {str(x).lower().strip().rstrip(".") for x in exclude}
        available = [(n, w) for n, w in ok if str(n).lower().strip().rstrip(".") not in excluded]
        if not available:
            return None
        # Accounts use local@domain, so wildcard MX is unnecessary. Prefer the
        # smaller exact-domain pool, which tends to have lower abuse reputation.
        pref = [n for n, wildcard in available if not wildcard] or [n for n, _ in available]
        return random.choice(pref)
    except Exception:
        return None


def _yyds_create(name, domain, expiry_ms, api_key, base_url, sess):
    key = (api_key or YYDS_API_KEY or MOEMAIL_API_KEY or "").strip()
    if not key:
        raise ValueError("YYDS Mail 需要 API key（YYDS_API_KEY，通常 AC- 开头）")
    base = _norm_yyds_base(base_url)
    explicit_domain = (domain or "").strip().lstrip("@").strip(".")
    dom = explicit_domain or _yyds_pick_domain(key, base, sess) or ""
    payload = {"domain": dom}
    local = (name or "").strip().lower()
    if local:
        payload["localPart"] = local
    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    tried_domains = set()
    for _attempt in range(4):
        tried_domains.add(dom.lower().rstrip("."))
        r = sess.post(f"{base}/v1/accounts", json=payload, headers=headers, timeout=HTTP_TIMEOUT)
        if r.status_code < 400:
            break
        body_text = (r.text or "")[:200]
        shared_domain_block = r.status_code == 403 and "shared domain" in body_text.lower()
        if not shared_domain_block or explicit_domain:
            hint = "；YYDS_BASE_URL 应填 https://maliapi.215.im" if r.status_code == 404 else ""
            raise RuntimeError(f"YYDS create {r.status_code} ({base}/v1/accounts): {body_text}{hint}")
        next_dom = _yyds_pick_domain(key, base, sess, exclude=tried_domains)
        if not next_dom:
            raise RuntimeError(
                f"YYDS create 403 ({base}/v1/accounts): {body_text}; no alternate shared domain"
            )
        dom = next_dom
        payload["domain"] = dom
        print(f"  [temp-email] YYDS shared domain rejected, retrying with {dom}")
    else:
        raise RuntimeError(f"YYDS create {r.status_code} ({base}/v1/accounts): {(r.text or '')[:200]}")
    data = r.json()
    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    eid = body.get("id") or body.get("inboxId") or body.get("accountId")
    addr = body.get("address") or body.get("email")
    token = body.get("token") or body.get("tempToken") or ""
    if not addr:
        raise RuntimeError(f"YYDS create 返回异常: {data}")
    return {"id": str(eid or ""), "email": str(addr), "token": str(token), "provider": "yyds", "raw": data}


def _yyds_fetch(mailbox_id, email, token, api_key, base_url, sess):
    key = (api_key or YYDS_API_KEY or MOEMAIL_API_KEY or "").strip()
    base = _norm_yyds_base(base_url)

    # YYDS's public mailbox flow uses the account token with /v1/messages.
    # Keep API-key and legacy inbox routes as fallbacks for older accounts.
    attempts = []
    params = {"limit": 20}
    if email:
        params["address"] = email
    if token:
        attempts.append((f"{base}/v1/messages", {"Authorization": f"Bearer {token}"}, params))
    if key:
        attempts.append((f"{base}/v1/messages", {"X-API-Key": key}, params))
    if mailbox_id and key:
        attempts.append((f"{base}/v1/inboxes/{mailbox_id}/messages",
                         {"X-API-Key": key}, {"limit": 20}))

    r = None
    headers = {}
    for url, candidate_headers, candidate_params in attempts:
        r = sess.get(url, headers=candidate_headers, params=candidate_params, timeout=HTTP_TIMEOUT)
        if r.status_code < 400:
            headers = candidate_headers
            break
    if r is None:
        raise ValueError("YYDS fetch 缺少 mailbox token 或 YYDS_API_KEY")
    if r.status_code >= 400:
        hint = "；请检查 YYDS_BASE_URL，应为 https://maliapi.215.im" if r.status_code == 404 else ""
        raise RuntimeError(f"YYDS fetch {r.status_code}: {r.text[:160]}{hint}")
    try:
        data = r.json() if r.content else {}
    except ValueError as e:
        raise RuntimeError(f"YYDS fetch 返回非 JSON（请检查 YYDS_BASE_URL={base}）") from e
    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    msgs = body.get("messages") or body.get("items") or [] if isinstance(body, dict) else []
    out = []
    for raw in msgs[:20]:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        mid = item.get("id") or item.get("messageId")
        if mid:
            detail_params = {"address": email} if email and "X-API-Key" in headers else {}
            d = sess.get(f"{base}/v1/messages/{mid}", headers=headers,
                         params=detail_params, timeout=HTTP_TIMEOUT)
            if d.status_code == 200 and d.content:
                dd = d.json()
                msg = dd.get("data") if isinstance(dd, dict) and "data" in dd else dd
                if isinstance(msg, dict):
                    item.update(msg)
        out.append(item)
    return out


# ==================================================================== GPTMail
GPTMAIL_PUBLIC_TEST_KEY = "gpt-test"


def _gptmail_pick_domain(key, base, sess):
    try:
        r = sess.get(f"{base}/api/domains/public", headers={"X-API-Key": key}, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            return None
        data = r.json()
        body = data.get("data") if isinstance(data, dict) and "data" in data else data
        doms = body.get("domains") if isinstance(body, dict) else body
        if not isinstance(doms, list):
            return None
        ok = []
        for d in doms:
            if isinstance(d, dict):
                # is_active 可能是 bool 或 0/1 整数：非真值(False/0/"")都跳过
                if "is_active" in d and not d.get("is_active"):
                    continue
                name = d.get("domain_name") or d.get("domain") or d.get("name")
            else:
                name = d
            if name:
                ok.append(name)
        return random.choice(ok) if ok else None
    except Exception:
        return None


def _gptmail_create(name, domain, expiry_ms, api_key, base_url, sess):
    key = (api_key or GPTMAIL_API_KEY or MOEMAIL_API_KEY or "").strip() or GPTMAIL_PUBLIC_TEST_KEY
    base = _norm_base(base_url, GPTMAIL_BASE_URL)
    dom = (domain or "").strip().lstrip("@").strip(".")
    pre = (name or "").strip().lower()
    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    if pre or dom:
        payload = {}
        if pre:
            payload["prefix"] = pre
        if dom:
            payload["domain"] = dom
        r = sess.post(f"{base}/api/generate-email", json=payload, headers=headers, timeout=HTTP_TIMEOUT)
    else:
        r = sess.get(f"{base}/api/generate-email", headers=headers, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        # 非鉴权错误（如域名/限流）：退化为自己拼 prefix@域名（域名从公共目录挑）。
        # 401/403 是 key 问题，拼地址也收不到信，直接抛错让上层回退。
        if r.status_code not in (401, 403):
            dom2 = dom or _gptmail_pick_domain(key, base, sess)
            if dom2:
                local = pre or _rand_local()
                addr = f"{local}@{dom2}"
                return {"id": str(addr), "email": str(addr), "token": "",
                        "provider": "gptmail", "raw": {"composed": True}}
        raise RuntimeError(f"GPTMail create {r.status_code}: {r.text[:200]}")
    data = r.json() if r.content else {}
    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    addr = (body.get("email") or body.get("address")) if isinstance(body, dict) else None
    if not addr:
        raise RuntimeError(f"GPTMail create 返回异常: {data}")
    return {"id": str(addr), "email": str(addr), "token": "", "provider": "gptmail", "raw": data}


def _gptmail_fetch(mailbox_id, email, token, api_key, base_url, sess):
    addr = (email or mailbox_id or "").strip()
    if "@" not in addr:
        return []
    key = (api_key or GPTMAIL_API_KEY or MOEMAIL_API_KEY or "").strip() or GPTMAIL_PUBLIC_TEST_KEY
    base = _norm_base(base_url, GPTMAIL_BASE_URL)
    headers = {"X-API-Key": key}
    r = sess.get(f"{base}/api/emails", headers=headers, params={"email": addr}, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        return []
    data = r.json() if r.content else {}
    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    msgs = (body.get("emails") or body.get("messages") or []) if isinstance(body, dict) else []
    out = []
    for raw in msgs[:20]:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        mid = item.get("id") or item.get("messageId")
        if mid:
            d = sess.get(f"{base}/api/email/{mid}", headers=headers, timeout=HTTP_TIMEOUT)
            if d.status_code == 200 and d.content:
                dd = d.json()
                msg = dd.get("data") if isinstance(dd, dict) and "data" in dd else dd
                if isinstance(msg, dict):
                    item.update(msg.get("email") if isinstance(msg.get("email"), dict) else msg)
        # 字段归一：html_content -> html, content -> text
        if item.get("html_content") and not item.get("html"):
            item["html"] = item["html_content"]
        out.append(item)
    return out


# ==================================================================== iCloud Mail API
def _icloud_error(response, action):
    """Format provider errors without echoing the apikey embedded in the URL."""
    message = ""
    try:
        body = response.json() if response.content else {}
        if isinstance(body, dict):
            message = str(body.get("message") or body.get("error") or "")
    except Exception:
        pass
    message = message[:160] or (response.text or "")[:160]
    return RuntimeError(f"iCloud Mail {action} {response.status_code}: {message}")


def _icloud_base_and_query(base_url):
    """Normalize either the API root or a pasted /api/user/email URL."""
    raw = _norm_base(base_url, ICLOUD_MAIL_API_BASE)
    parsed = urlsplit(raw)
    path = parsed.path.rstrip("/")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for endpoint in ("/api/user/email", "/api/user/mail"):
        if path.lower().endswith(endpoint):
            path = path[: -len(endpoint)].rstrip("/")
            break
    root = urlunsplit((parsed.scheme or "https", parsed.netloc, path, "", "")).rstrip("/")
    return root, query


def _icloud_create(name, domain, expiry_ms, api_key, base_url, sess):
    """Allocate an iCloud address from the provider's GET-only API."""
    base, pasted_query = _icloud_base_and_query(base_url)
    key = (api_key or pasted_query.get("apikey") or ICLOUD_MAIL_API_KEY or "").strip()
    if not key:
        raise ValueError("iCloud Mail 需要 API key（ICLOUD_MAIL_API_KEY）")
    kind = (pasted_query.get("type") or ICLOUD_MAIL_TYPE or "icloud-code").strip().lower()
    if kind not in {"icloud", "icloud-code"}:
        raise ValueError("ICLOUD_MAIL_TYPE 只能是 icloud 或 icloud-code")
    params = {"type": kind, "apikey": key}
    if kind == "icloud-code":
        service = (pasted_query.get("service") or ICLOUD_MAIL_SERVICE or "openai").strip().lower()
        if not service:
            raise ValueError("icloud-code 需要 ICLOUD_MAIL_SERVICE")
        params["service"] = service
    response = sess.get(f"{base}/api/user/email", params=params, timeout=HTTP_TIMEOUT)
    if response.status_code >= 400:
        raise _icloud_error(response, "create")
    data = response.json() if response.content else {}
    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    address = (body.get("email") or body.get("address")) if isinstance(body, dict) else None
    if not address or "@" not in str(address):
        raise RuntimeError(f"iCloud Mail create 返回异常: {str(data)[:220]}")
    return {
        "id": str(address),
        "email": str(address),
        "token": "",
        "provider": "icloud",
        "raw": data,
    }


def _icloud_fetch(mailbox_id, email, token, api_key, base_url, sess):
    """Query the latest message; an empty success response means no mail yet."""
    base, pasted_query = _icloud_base_and_query(base_url)
    key = (api_key or pasted_query.get("apikey") or ICLOUD_MAIL_API_KEY or "").strip()
    address = (email or mailbox_id or "").strip()
    if not key or "@" not in address:
        return []
    response = sess.get(
        f"{base}/api/user/mail",
        params={"email": address, "apikey": key},
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code >= 400:
        raise _icloud_error(response, "fetch")
    data = response.json() if response.content else {}
    if isinstance(data, dict) and data.get("code") == 0 and "data" not in data:
        return []
    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    if not body:
        return []
    if isinstance(body, list):
        return [dict(item) for item in body if isinstance(item, dict)]
    if isinstance(body, dict):
        return [dict(body)]
    return []


# ==================================================================== Cloudflare Temp Email
def _cfmail_pick_domain(base, sess, site_pw):
    try:
        headers = {"x-custom-auth": site_pw} if site_pw else {}
        r = sess.get(f"{base}/open_api/settings", headers=headers, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            return None
        data = r.json()
        body = data.get("data") if isinstance(data, dict) and "data" in data else data
        pool = []
        for field in ("defaultDomains", "domains", "randomSubdomainDomains"):
            vals = body.get(field) if isinstance(body, dict) else None
            if isinstance(vals, list):
                for v in vals:
                    pool.append(v.get("value") if isinstance(v, dict) else v)
        pool = [p for p in pool if p]
        return random.choice(pool) if pool else None
    except Exception:
        return None


def _cfmail_create(name, domain, expiry_ms, api_key, base_url, sess):
    base = _norm_base(base_url, CFMAIL_BASE_URL)
    admin_pw = (api_key or CFMAIL_ADMIN_PASSWORD or "").strip()
    site_pw = (CFMAIL_SITE_PASSWORD or "").strip()
    dom = (domain or "").strip().lstrip("@").strip(".") or _cfmail_pick_domain(base, sess, site_pw) or ""
    local = (name or "").strip().lower() or _rand_local()
    payload = {"name": local, "domain": dom, "enablePrefix": False}
    headers = {"Content-Type": "application/json"}
    if site_pw:
        headers["x-custom-auth"] = site_pw
    if admin_pw:
        headers["x-admin-auth"] = admin_pw
        r = sess.post(f"{base}/admin/new_address", json=payload, headers=headers, timeout=HTTP_TIMEOUT)
    else:
        r = sess.post(f"{base}/api/new_address", json=payload, headers=headers, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"CFMail create {r.status_code}: {r.text[:200]}")
    data = r.json() if r.content else {}
    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    addr = body.get("address") if isinstance(body, dict) else None
    jwt = (body.get("jwt") or body.get("token")) if isinstance(body, dict) else None
    aid = body.get("address_id") if isinstance(body, dict) else None
    if not addr or not jwt:
        raise RuntimeError(f"CFMail create 返回异常: {data}")
    return {"id": str(aid or addr), "email": str(addr), "token": str(jwt), "provider": "cfmail", "raw": data}


def _cfmail_fetch(mailbox_id, email, token, api_key, base_url, sess):
    if not token:
        return []
    base = _norm_base(base_url, CFMAIL_BASE_URL)
    headers = {"Authorization": f"Bearer {token}"}
    r = sess.get(f"{base}/api/parsed_mails", headers=headers,
                 params={"limit": 20, "offset": 0}, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        r = sess.get(f"{base}/api/mails", headers=headers,
                     params={"limit": 20, "offset": 0}, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            return []
    data = r.json() if r.content else {}
    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    results = body.get("results") if isinstance(body, dict) else body
    if not isinstance(results, list):
        return []
    out = []
    for raw in results[:20]:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        # 只有 raw RFC822 时解析出 text/html
        if not (item.get("text") or item.get("html")) and item.get("raw"):
            try:
                msg = email_parser.message_from_string(item["raw"])
                item["subject"] = item.get("subject") or msg.get("subject", "")
                for part in msg.walk():
                    ctype = part.get_content_type()
                    if ctype == "text/plain" and not item.get("text"):
                        item["text"] = part.get_payload(decode=True).decode(errors="ignore")
                    elif ctype == "text/html" and not item.get("html"):
                        item["html"] = part.get_payload(decode=True).decode(errors="ignore")
            except Exception:
                pass
        out.append(item)
    return out


# ==================================================================== 自定义（配置驱动）
def _custom_create(name, domain, expiry_ms, api_key, base_url, sess):
    """配置驱动地接任意 REST 临时邮箱建号 API。端点/方法/body/字段路径全走 CUSTOM_MAIL_* 配置。"""
    base = _norm_base(base_url, CUSTOM_MAIL_BASE_URL)
    if not base:
        raise ValueError("自定义临时邮箱需要 CUSTOM_MAIL_BASE_URL")
    if not CUSTOM_MAIL_CREATE_PATH:
        raise ValueError("自定义临时邮箱需要 CUSTOM_MAIL_CREATE_PATH")
    local = (name or "").strip().lower() or _rand_local()
    dom = (domain or "").strip().lstrip("@").strip(".")
    ctx = {"name": local, "domain": dom, "email": "", "id": "", "token": "", "msg_id": ""}
    url = base + _fill_tpl(CUSTOM_MAIL_CREATE_PATH, ctx)
    headers = _custom_headers(use_token=False, token="", api_key=api_key)
    method = (CUSTOM_MAIL_CREATE_METHOD or "POST").strip().upper()
    if method == "POST":
        headers["Content-Type"] = "application/json"
        body = None
        if CUSTOM_MAIL_CREATE_BODY:
            try:
                body = json.loads(_fill_tpl(CUSTOM_MAIL_CREATE_BODY, ctx))
            except Exception as e:
                raise ValueError(f"CUSTOM_MAIL_CREATE_BODY 不是合法 JSON: {str(e)[:60]}")
        r = sess.post(url, json=body, headers=headers, timeout=HTTP_TIMEOUT)
    else:
        r = sess.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"custom create {r.status_code}: {r.text[:200]}")
    data = r.json() if r.content else {}
    addr = _json_path(data, CUSTOM_MAIL_EMAIL_PATH)
    if not addr:
        raise RuntimeError(f"custom create 未按 CUSTOM_MAIL_EMAIL_PATH='{CUSTOM_MAIL_EMAIL_PATH}' 取到 email: {str(data)[:200]}")
    eid = _json_path(data, CUSTOM_MAIL_ID_PATH) if CUSTOM_MAIL_ID_PATH else addr
    token = _json_path(data, CUSTOM_MAIL_TOKEN_PATH) if CUSTOM_MAIL_TOKEN_PATH else ""
    return {"id": str(eid or addr), "email": str(addr), "token": str(token or ""),
            "provider": "custom", "raw": data}


def _custom_fetch(mailbox_id, email, token, api_key, base_url, sess):
    """配置驱动地拉自定义临时邮箱收件箱。返回 raw dict 列表（取码交给 fetch_messages 统一逻辑）。"""
    base = _norm_base(base_url, CUSTOM_MAIL_BASE_URL)
    if not base or not CUSTOM_MAIL_FETCH_PATH:
        return []
    ctx = {"name": "", "domain": "", "email": email or "", "id": mailbox_id or "",
           "token": token or "", "msg_id": ""}
    url = base + _fill_tpl(CUSTOM_MAIL_FETCH_PATH, ctx)
    use_token = CUSTOM_MAIL_FETCH_AUTH == "token"
    headers = _custom_headers(use_token=use_token, token=token, api_key=api_key)
    method = (CUSTOM_MAIL_FETCH_METHOD or "GET").strip().upper()
    r = sess.post(url, headers=headers, timeout=HTTP_TIMEOUT) if method == "POST" \
        else sess.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        return []
    data = r.json() if r.content else {}
    msgs = _json_path(data, CUSTOM_MAIL_LIST_PATH) if CUSTOM_MAIL_LIST_PATH else data
    if not isinstance(msgs, list):
        return []
    out = []
    for raw in msgs[:20]:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        # 列表无正文且配了 detail 端点：逐封拉详情合并
        if CUSTOM_MAIL_DETAIL_PATH and not (item.get("text") or item.get("html") or item.get("content")):
            mid = _json_path(item, CUSTOM_MAIL_MSG_ID_PATH)
            if mid:
                dctx = dict(ctx, msg_id=str(mid))
                durl = base + _fill_tpl(CUSTOM_MAIL_DETAIL_PATH, dctx)
                try:
                    d = sess.get(durl, headers=headers, timeout=HTTP_TIMEOUT)
                    if d.status_code == 200 and d.content:
                        dd = d.json()
                        msg = _json_path(dd, CUSTOM_MAIL_MSG_PATH) if CUSTOM_MAIL_MSG_PATH else dd
                        if isinstance(msg, dict):
                            item.update(msg)
                except Exception:
                    pass
        out.append(item)
    return out


# ==================================================================== 统一接口
_CREATE = {
    "moemail": _moemail_create,
    "yyds": _yyds_create,
    "gptmail": _gptmail_create,
    "icloud": _icloud_create,
    "cfmail": _cfmail_create,
    "custom": _custom_create,
}
_FETCH = {
    "moemail": _moemail_fetch,
    "yyds": _yyds_fetch,
    "gptmail": _gptmail_fetch,
    "icloud": _icloud_fetch,
    "cfmail": _cfmail_fetch,
    "custom": _custom_fetch,
}


def _provider_list(provider=None):
    """把 provider 规格解析成有序去重的 provider 列表（支持故障转移）。
    provider 可为：None(用 config.TEMP_EMAIL_PROVIDER) / 单名 / 逗号(或空格)分隔多名 / list。
    config.TEMP_EMAIL_PROVIDER 也支持 'yyds,gptmail,cfmail' 这种多 provider 写法。"""
    if provider is None:
        provider = TEMP_EMAIL_PROVIDER
    if isinstance(provider, (list, tuple)):
        raw = provider
    else:
        raw = re.split(r"[,\s]+", str(provider or ""))
    out = []
    for p in raw:
        p = (p or "").strip().lower()
        if not p:
            continue
        norm = normalize_provider(p)
        if norm not in out:
            out.append(norm)
    return out or ["gptmail"]


def create_mailbox(provider=None, name=None, domain=None, expiry_ms=None,
                   api_key=None, base_url=None):
    """创建临时邮箱。返回 {"id", "email", "token", "provider", "raw"}。
    provider 为空时用 config.TEMP_EMAIL_PROVIDER，支持逗号分隔的多 provider 故障转移：
    按序尝试，第一个建号成功的即返回；全失败才抛异常（把各家错误合并抛出）。
    显式传 base_url/api_key 时只按单 provider 走（多 provider 无法共用同一 base_url）。"""
    # 显式 base_url 只对单 provider 有意义，退回单 provider 路径
    if base_url or api_key:
        prov = normalize_provider(provider or TEMP_EMAIL_PROVIDER, base_url)
        fn = _CREATE.get(prov)
        if not fn:
            raise ValueError(f"未知临时邮箱 provider: {prov}")
        return fn(name, domain, expiry_ms, api_key, base_url, _session())
    provs = _provider_list(provider)
    errors = []
    for prov in provs:
        fn = _CREATE.get(prov)
        if not fn:
            errors.append(f"{prov}: 未知 provider")
            continue
        try:
            mb = fn(name, domain, expiry_ms, None, None, _session())
            if len(provs) > 1:
                print(f"  [temp-email] provider={prov} 建号成功（候选 {provs}）")
            return mb
        except Exception as e:
            msg = f"{prov}: {str(e)[:120]}"
            print(f"  [temp-email] provider {prov} 建号失败，尝试下一个 -> {str(e)[:80]}")
            errors.append(msg)
    raise RuntimeError("所有临时邮箱 provider 均建号失败：\n  " + "\n  ".join(errors))


def fetch_messages(mailbox_id, provider, email=None, token=None,
                   api_key=None, base_url=None):
    """拉取邮箱消息，每条附带 extracted={"codes":[...], "links":[...]}。"""
    prov = normalize_provider(provider, base_url)
    fn = _FETCH.get(prov)
    if not fn:
        return []
    sess = _session()
    msgs = fn(mailbox_id, email, token, api_key, base_url, sess)
    for m in msgs:
        text = "\n".join(str(m.get(k) or "") for k in (
            "subject", "content", "text", "textBody", "html", "htmlBody",
            "body", "from_address", "from", "sender", "verificationCode", "code"))
        ex = _extract_codes_and_links(text)
        # YYDS 服务端直接给 verificationCode 字段时优先采信
        vc = m.get("verificationCode")
        if vc and str(vc) not in ex["codes"]:
            ex["codes"].insert(0, str(vc))
        m["extracted"] = ex
    return msgs


def _hit(msg, sender_hint, subject_hint):
    """邮件是否命中发件人/主题提示（宽松：任一命中即算；两个 hint 都空则全放行）。"""
    if not sender_hint and not subject_hint:
        return True
    frm = " ".join(str(msg.get(k) or "") for k in ("from_address", "from", "sender")).lower()
    subj = str(msg.get("subject") or "").lower()
    hit_s = any(s.lower() in frm for s in sender_hint) if sender_hint else False
    hit_j = any(s.lower() in subj for s in subject_hint) if subject_hint else False
    return hit_s or hit_j


def _scan_once(mailbox_id, provider, email, token, api_key, base_url,
               sender_hint, subject_hint, code_regex, exclude_codes=()):
    """同步扫一轮，命中目标邮件就返回 code，否则 None。给 poll 用 executor 调。"""
    try:
        msgs = fetch_messages(mailbox_id, provider, email=email, token=token,
                              api_key=api_key, base_url=base_url)
    except Exception as e:
        print(f"  [temp-email] fetch error: {str(e)[:80]}")
        return None
    pat = re.compile(code_regex) if code_regex else None
    excluded = {str(code) for code in (exclude_codes or ()) if code}
    for m in msgs:
        if not _hit(m, sender_hint, subject_hint):
            continue
        if pat:
            # 自定义正则：在主题+正文里找（剥 HTML 避免命中 hex 色值）
            for text in (str(m.get("subject") or ""),
                         _strip_html(str(m.get("html") or m.get("htmlBody") or "")),
                         str(m.get("text") or m.get("textBody") or m.get("content") or "")):
                for mm in pat.finditer(text):
                    code = next((g for g in mm.groups() if g), mm.group(0))
                    if str(code) not in excluded:
                        return code
        codes = m.get("extracted", {}).get("codes") or []
        code = next((code for code in codes if str(code) not in excluded), None)
        if code:
            return code
    return None


async def poll_verification_code(mailbox_id, provider, email=None, token=None,
                                 api_key=None, base_url=None,
                                 max_wait=120, poll_interval=5,
                                 sender_hint=(), subject_hint=(), code_regex=None,
                                 exclude_codes=()):
    """轮询临时邮箱直到拿到验证码或超时。返回 code 字符串或 None。
    - sender_hint/subject_hint：筛选目标邮件（宽松匹配）。
    - code_regex：给定则用它提码（如 grok 的 XXX-XXX），否则用默认 6-8 位数字。
    - exclude_codes：忽略已经尝试过的旧码，适用于 resend 后旧邮件仍在收件箱的场景。
    HTTP 调用走线程池（requests 同步），不阻塞事件循环。"""
    loop = asyncio.get_event_loop()
    start = time.time()
    while time.time() - start < max_wait:
        code = await loop.run_in_executor(
            None, _scan_once, mailbox_id, provider, email, token, api_key, base_url,
            tuple(sender_hint), tuple(subject_hint), code_regex, tuple(exclude_codes))
        if code:
            print(f"  [temp-email] code found: {code}")
            return code
        elapsed = int(time.time() - start)
        print(f"  [temp-email] waiting for code... ({elapsed}s/{max_wait}s)")
        await asyncio.sleep(poll_interval)
    print("  [temp-email] timeout, no code")
    return None


def poll_verification_code_blocking(mailbox_id, provider, email=None, token=None,
                                    api_key=None, base_url=None,
                                    max_wait=120, poll_interval=5,
                                    sender_hint=(), subject_hint=(), code_regex=None,
                                    exclude_codes=()):
    """Synchronous companion for HTTP-only flows such as Outlook Graph OAuth."""
    start = time.time()
    while time.time() - start < max_wait:
        code = _scan_once(
            mailbox_id, provider, email, token, api_key, base_url,
            tuple(sender_hint), tuple(subject_hint), code_regex,
            tuple(exclude_codes),
        )
        if code:
            print(f"  [temp-email] code found: {code}")
            return code
        elapsed = int(time.time() - start)
        print(f"  [temp-email] waiting for code... ({elapsed}s/{max_wait}s)")
        time.sleep(poll_interval)
    print("  [temp-email] timeout, no code")
    return None


def _provider_config(prov):
    """返回某 provider 的 (base_url, key_present, key_source)，供 doctor 展示。"""
    if prov == "moemail":
        return MOEMAIL_BASE_URL, bool(MOEMAIL_API_KEY), "MOEMAIL_API_KEY"
    if prov == "yyds":
        return YYDS_BASE_URL, bool(YYDS_API_KEY or MOEMAIL_API_KEY), "YYDS_API_KEY"
    if prov == "gptmail":
        return GPTMAIL_BASE_URL, True, "GPTMAIL_API_KEY(或公共 gpt-test)"
    if prov == "icloud":
        # A complete endpoint may carry apikey in its query string.  Return
        # only the normalized root so doctor output never prints that secret.
        base, query = _icloud_base_and_query(ICLOUD_MAIL_API_BASE)
        return base, bool(ICLOUD_MAIL_API_KEY or query.get("apikey")), "ICLOUD_MAIL_API_KEY 或 ICLOUD_MAIL_API_BASE"
    if prov == "cfmail":
        return CFMAIL_BASE_URL, bool(CFMAIL_ADMIN_PASSWORD), "CFMAIL_ADMIN_PASSWORD"
    if prov == "custom":
        # custom 没有"key 必填"概念（鉴权可选）；这里的 has_key 表示"关键配置是否齐"。
        ready = bool(CUSTOM_MAIL_BASE_URL and CUSTOM_MAIL_CREATE_PATH and CUSTOM_MAIL_FETCH_PATH)
        return CUSTOM_MAIL_BASE_URL, ready, "CUSTOM_MAIL_BASE_URL/CREATE_PATH/FETCH_PATH"
    return "", False, ""


def doctor(providers=None):
    """连通性自测：逐个 provider 检查 域名目录可达 + 建号可用。
    不轮询收码（那要真发信）。返回 [(prov, ok, detail)]，并打印人类可读报告。"""
    provs = _provider_list(providers) if providers else ["moemail", "yyds", "gptmail", "cfmail", "icloud", "custom"]
    print("=" * 56)
    print(f"  临时邮箱连通性自测  providers={provs}")
    print("=" * 56)
    results = []
    for prov in provs:
        base, has_key, key_src = _provider_config(prov)
        print(f"\n[{prov}] base={base}")
        print(f"  key: {'已配置' if has_key else '缺失'} ({key_src})")
        # 1) 域名目录可达性（不需要建号，先探端点）
        sess = _session()
        dom = None
        try:
            if prov == "yyds":
                dom = _yyds_pick_domain(YYDS_API_KEY or MOEMAIL_API_KEY, _norm_base(None, YYDS_BASE_URL), sess)
            elif prov == "gptmail":
                dom = _gptmail_pick_domain(GPTMAIL_API_KEY, _norm_base(None, GPTMAIL_BASE_URL), sess)
            elif prov == "cfmail":
                dom = _cfmail_pick_domain(_norm_base(None, CFMAIL_BASE_URL), sess, CFMAIL_SITE_PASSWORD)
            if prov == "custom":
                # custom 无公开域名目录；改为检查关键配置是否填齐
                miss = [n for n, v in (("CUSTOM_MAIL_BASE_URL", CUSTOM_MAIL_BASE_URL),
                                       ("CUSTOM_MAIL_CREATE_PATH", CUSTOM_MAIL_CREATE_PATH),
                                       ("CUSTOM_MAIL_FETCH_PATH", CUSTOM_MAIL_FETCH_PATH)) if not v]
                print(f"  配置: {'齐全' if not miss else '缺 ' + ', '.join(miss)}")
            elif dom:
                print(f"  域名目录: OK (样例域名 {dom})")
            else:
                print(f"  域名目录: 未取到（可能需 key 或该 provider 无公开目录）")
        except Exception as e:
            print(f"  域名目录: 探测异常 {str(e)[:80]}")
        # 2) 建号（真建一个，能建=端点+鉴权都通）
        ok, detail = False, ""
        try:
            mb = create_mailbox(provider=prov)
            ok = True
            detail = mb["email"]
            print(f"  建号: OK -> {mb['email']}")
        except Exception as e:
            detail = str(e)[:120]
            print(f"  建号: FAIL -> {detail}")
        results.append((prov, ok, detail))
    okn = sum(1 for _, o, _ in results if o)
    print("\n" + "=" * 56)
    print(f"  可用 provider: {okn}/{len(results)} -> "
          + ", ".join(f"{p}{'✓' if o else '✗'}" for p, o, _ in results))
    print("=" * 56)
    return results


if __name__ == "__main__":
    # 用法：
    #   python -m common.temp_email doctor [prov[,prov...]]   连通性自测（建号，不收码）
    #   python -m common.temp_email [prov[,prov...]]           建号 + 轮询 60s 收码
    import sys as _sys
    args = _sys.argv[1:]
    if args and args[0] == "doctor":
        doctor(args[1] if len(args) > 1 else None)
    else:
        prov = args[0] if args else None
        mb = create_mailbox(provider=prov)
        print(f"created: {mb}")
        print("polling 60s for any message...")
        code = asyncio.run(poll_verification_code(
            mb["id"], mb["provider"], email=mb["email"], token=mb.get("token"),
            max_wait=60, poll_interval=5))
        print(f"code: {code}")
