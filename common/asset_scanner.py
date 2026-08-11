"""Online status scanner for local mailbox and platform asset pools."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from common import asset_store


PLATFORMS = ("outlook", "chatgpt", "claude", "grok", "kiro")
STATUSES = (
    "normal",
    "unlock",
    "banned",
    "expired",
    "restricted",
    "invalid",
    "unknown",
    "error",
)
PLUS_TRIAL_STATUSES = ("eligible", "ineligible", "active", "unknown", "disabled")

_BANNED_MARKERS = (
    "account_deactivated",
    "account deactivated",
    "account_disabled",
    "account disabled",
    "account suspended",
    "your account has been suspended",
    "your account has been banned",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _scan_path() -> Path:
    return asset_store._data_root() / "runtime" / "state" / "asset_pool_scan.json"


def _stable_id(platform: str, email: str, source: str) -> str:
    identity = email.strip().lower() or source.strip().lower()
    return hashlib.sha256(f"{platform}|{identity}".encode("utf-8")).hexdigest()[:20]


def _read_cache() -> dict:
    try:
        value = json.loads(_scan_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_cache(report: dict) -> None:
    path = _scan_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _history_outcomes() -> dict[str, dict]:
    """Return the newest known Outlook unlock outcome for each email."""
    root = asset_store._data_root()
    mappings = {
        "unlocked_": ("normal", "历史解锁成功"),
        "unlocked_clean_": ("normal", "历史解锁成功"),
        "needs_phone_": ("unlock", "需要手机验证解锁"),
        "locked_for_unlock_": ("unlock", "等待解锁"),
        "abuse_locked_": ("banned", "Abuse 锁定"),
        "dead_account_": ("banned", "账号不可用"),
        "failed_": ("unknown", "历史检测失败"),
    }
    result: dict[str, dict] = {}
    paths = []
    for directory in (root / "unlock_results", root / "check_results"):
        if directory.is_dir():
            paths.extend(path for path in directory.glob("*.txt") if path.is_file())
    for path in sorted(paths, key=lambda item: item.stat().st_mtime):
        mapping = next((value for prefix, value in mappings.items() if path.name.startswith(prefix)), None)
        if not mapping:
            continue
        checked_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            email = raw.strip().split("----", 1)[0].strip().lower()
            if "@" in email:
                result[email] = {
                    "status": mapping[0],
                    "detail": mapping[1],
                    "evidence": f"history:{path.name}",
                    "checked_at": checked_at,
                }
    return result


def _claude_token_records() -> list[dict]:
    directory = asset_store._token_root() / "claude"
    records = []
    paths = directory.glob("*.sessionKey.json") if directory.is_dir() else ()
    for path in paths:
        try:
            data = asset_store._read_json(path)
        except Exception:
            continue
        if isinstance(data, dict):
            records.append({"path": path, "data": data})
    return sorted(records, key=lambda item: (item["path"].stat().st_mtime, str(item["path"]).lower()))


def _merge_platform_records(platform: str) -> list[dict]:
    merged: dict[str, dict] = {}

    def obtain(email: str, source: str) -> dict:
        email = str(email or "").strip()
        key = email.lower() or f"source:{source.lower()}"
        if key not in merged:
            merged[key] = {
                "platform": platform,
                "kind": "platform",
                "email": email,
                "sources": set(),
                "_cookies": [],
                "_token": {},
            }
        elif email and not merged[key]["email"]:
            merged[key]["email"] = email
        merged[key]["sources"].add(source)
        return merged[key]

    token_records = _claude_token_records() if platform == "claude" else asset_store._token_records(platform)
    for record in token_records:
        data = record["data"]
        source = record["path"].name
        email = asset_store._email_from_session(data, record["path"].stem.split(".")[0])
        obtain(email, source)["_token"] = data

    for record in asset_store._cookie_records(platform):
        source = record["path"].name
        target = obtain(record.get("email", ""), source)
        target["_cookies"] = record["cookies"]

    records = []
    for record in merged.values():
        source = ", ".join(sorted(record.pop("sources")))
        record["source"] = source
        record["id"] = _stable_id(platform, record["email"], source)
        records.append(record)
    return sorted(records, key=lambda item: (item["email"].lower(), item["source"].lower()))


def _inventory_records() -> list[dict]:
    records = []
    history = _history_outcomes()
    seen_mailboxes = set()
    for index, mailbox in enumerate(asset_store._mailboxes()):
        email = mailbox.get("email", "").strip()
        identity = email.lower()
        if identity in seen_mailboxes:
            continue
        seen_mailboxes.add(identity)
        source = f"emails.txt:{index + 1}"
        records.append({
            "id": _stable_id("outlook", email, source),
            "platform": "outlook",
            "kind": "mailbox",
            "email": email,
            "source": source,
            "_mailbox": mailbox,
            "_history": history.get(identity),
        })
    for platform in ("chatgpt", "claude", "grok"):
        records.extend(_merge_platform_records(platform))
    # Kiro is an account bundle rather than a browser/cookie pool. Include it
    # in inventory when present without changing the legacy scan pool contract.
    if asset_store._token_records("kiro"):
        records.extend(_merge_platform_records("kiro"))
    return records


def _public_record(record: dict) -> dict:
    return {key: value for key, value in record.items() if not key.startswith("_")}


def _status_summary(items: list[dict]) -> dict:
    statuses = {status: 0 for status in STATUSES}
    plus_trial = {status: 0 for status in PLUS_TRIAL_STATUSES}
    platforms = {}
    for item in items:
        status = item.get("status", "unknown")
        if status not in statuses:
            status = "unknown"
        statuses[status] += 1
        platform = item.get("platform", "unknown")
        entry = platforms.setdefault(platform, {"total": 0, **{name: 0 for name in STATUSES}})
        entry["total"] += 1
        entry[status] += 1
        if platform == "chatgpt":
            trial_status = str(item.get("plus_trial") or "unknown")
            plus_trial[trial_status if trial_status in plus_trial else "unknown"] += 1
    return {
        "total": len(items),
        "statuses": statuses,
        "platforms": platforms,
        "plus_trial": plus_trial,
    }


def get_report() -> dict:
    cache = _read_cache()
    cached_items = {
        str(item.get("id")): item
        for item in cache.get("items", [])
        if isinstance(item, dict) and item.get("id")
    }
    items = []
    for record in _inventory_records():
        public = _public_record(record)
        cached = cached_items.get(public["id"], {})
        for key in (
            "status", "detail", "evidence", "checked_at", "latency_ms",
            "plus_trial", "plus_trial_detail", "plus_trial_evidence",
        ):
            if key in cached:
                public[key] = cached[key]
        public.setdefault("status", "unknown")
        public.setdefault("detail", "尚未扫描")
        public.setdefault("evidence", "none")
        public.setdefault("checked_at", "")
        if public.get("platform") == "chatgpt":
            public.setdefault("plus_trial", "unknown")
            public.setdefault("plus_trial_detail", "尚未检测 Plus 试用资格")
            public.setdefault("plus_trial_evidence", "none")
        items.append(public)
    return {
        "schema_version": 2,
        "last_scan_at": cache.get("finished_at", ""),
        "items": items,
        "summary": _status_summary(items),
    }


def update_cached_outlook_statuses(outcomes: dict[str, dict]) -> int:
    """Update cached Outlook outcomes after unlock or RT recovery without storing secrets."""
    normalized = {
        str(email or "").strip().lower(): outcome
        for email, outcome in (outcomes or {}).items()
        if str(email or "").strip() and isinstance(outcome, dict)
    }
    if not normalized:
        return 0
    cache = _read_cache()
    items = cache.get("items")
    if not isinstance(items, list):
        return 0
    updated = 0
    for item in items:
        if not isinstance(item, dict) or item.get("platform") != "outlook":
            continue
        outcome = normalized.get(str(item.get("email") or "").strip().lower())
        if not outcome:
            continue
        status = str(outcome.get("status") or "unknown").strip().lower()
        item.update({
            "status": status if status in STATUSES else "unknown",
            "detail": str(outcome.get("detail") or "恢复任务已更新账号状态"),
            "evidence": str(outcome.get("evidence") or "recovery:updated"),
            "checked_at": _now_iso(),
            "latency_ms": 0,
        })
        updated += 1
    if updated:
        cache["summary"] = _status_summary(items)
        cache["finished_at"] = _now_iso()
        _write_cache(cache)
    return updated


def _web_session(platform: str = "") -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    try:
        from common import proxy_switch

        target_env = proxy_switch.platform_environment(os.environ, platform) if platform else os.environ
        proxy = proxy_switch.effective_proxy_url(target_env)
    except Exception:
        proxy = ""
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return session


def _response_status(response, service: str) -> dict | None:
    text = (response.text or "")[:3000].lower()
    if response.status_code == 401:
        return {"status": "expired", "detail": f"{service} 登录凭据已过期", "evidence": f"{service}:401"}
    if response.status_code == 403:
        if any(marker in text for marker in _BANNED_MARKERS):
            return {"status": "banned", "detail": f"{service} 明确返回账号停用", "evidence": f"{service}:403"}
        return {"status": "restricted", "detail": f"{service} HTTP 403，可能为账号风控或出口限制", "evidence": f"{service}:403"}
    if response.status_code == 429:
        return {"status": "restricted", "detail": f"{service} 请求限流", "evidence": f"{service}:429"}
    if response.status_code >= 500:
        return {"status": "error", "detail": f"{service} 服务异常 HTTP {response.status_code}", "evidence": f"{service}:{response.status_code}"}
    return None


def _platform_preflight(platform: str, timeout: int) -> dict | None:
    """Short-circuit a whole pool when its service is unreachable from this exit."""
    try:
        if platform == "outlook":
            with _web_session("outlook") as session:
                response = session.post(
                    "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                    data={
                        "client_id": "9e5f94bc-e8a4-4e73-b8be-63364c29d753",
                        "grant_type": "refresh_token",
                        "refresh_token": "preflight",
                        "scope": "https://graph.microsoft.com/Mail.Read",
                    },
                    timeout=timeout,
                )
        else:
            urls = {
                "chatgpt": "https://chatgpt.com/api/auth/session",
                "claude": "https://claude.ai/api/account",
                "grok": "https://accounts.x.ai/",
                "kiro": "https://oidc.us-east-1.amazonaws.com/",
            }
            with _web_session(platform) as session:
                response = session.get(urls[platform], timeout=timeout, allow_redirects=True)
        if response.status_code >= 500:
            return {
                "status": "error",
                "detail": f"{platform} 服务预检 HTTP {response.status_code}",
                "evidence": f"preflight:{response.status_code}",
            }
        return None
    except requests.Timeout:
        return {"status": "error", "detail": f"{platform} 服务预检超时", "evidence": "preflight:timeout"}
    except requests.RequestException as exc:
        return {
            "status": "error",
            "detail": f"{platform} 服务预检失败：{type(exc).__name__}",
            "evidence": "preflight:network_error",
        }


def _scan_outlook(record: dict, timeout: int) -> dict:
    mailbox = record.get("_mailbox") or {}
    refresh_token = str(mailbox.get("refresh_token") or "").strip()
    client_id = str(mailbox.get("client_id") or "").strip() or "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
    history = record.get("_history")
    if not refresh_token:
        if history and history.get("status") in {"unlock", "banned", "normal"}:
            return dict(history)
        return {"status": "unknown", "detail": "缺少 Graph refresh token，无法在线确认", "evidence": "local:missing_refresh_token"}

    with _web_session("outlook") as session:
        response = session.post(
            "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": "https://graph.microsoft.com/Mail.Read",
            },
            timeout=timeout,
        )
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if response.status_code == 200 and payload.get("access_token"):
            graph = session.get(
                "https://graph.microsoft.com/v1.0/me/mailFolders/inbox?$select=id",
                headers={"Authorization": f"Bearer {payload['access_token']}"},
                timeout=timeout,
            )
            if graph.status_code == 200:
                return {"status": "normal", "detail": "Graph 邮箱访问正常", "evidence": "microsoft_graph:200"}
            if graph.status_code in {401, 403}:
                return {"status": "restricted", "detail": f"Graph 邮箱访问 HTTP {graph.status_code}", "evidence": f"microsoft_graph:{graph.status_code}"}
            return {"status": "error", "detail": f"Graph 检测 HTTP {graph.status_code}", "evidence": f"microsoft_graph:{graph.status_code}"}

    description = str(payload.get("error_description") or payload.get("error") or "").lower()
    error_codes = {str(value) for value in payload.get("error_codes", [])}
    if "service abuse mode" in description:
        return {
            "status": "banned",
            "detail": "Microsoft 账号处于服务滥用限制",
            "evidence": "microsoft_oauth:service_abuse",
        }
    if "different tenant" in description:
        return {
            "status": "expired",
            "detail": "Graph refresh token 与账号租户不匹配",
            "evidence": "microsoft_oauth:tenant_mismatch",
        }
    if "50057" in error_codes or "user account is disabled" in description:
        return {"status": "banned", "detail": "Microsoft 账号已禁用", "evidence": "microsoft_oauth:AADSTS50057"}
    if "50053" in error_codes or "account is locked" in description:
        return {"status": "unlock", "detail": "Microsoft 账号已锁定，需要解锁", "evidence": "microsoft_oauth:AADSTS50053"}
    if error_codes.intersection({"50055", "50076", "50079"}):
        return {"status": "unlock", "detail": "Microsoft 要求补充验证", "evidence": f"microsoft_oauth:AADSTS{sorted(error_codes)[0]}"}
    if history and history.get("status") in {"unlock", "banned"}:
        return dict(history)
    if response.status_code == 400 and str(payload.get("error") or "") == "invalid_grant":
        return {"status": "expired", "detail": "Graph refresh token 已失效或撤销", "evidence": "microsoft_oauth:invalid_grant"}
    if response.status_code == 429:
        return {"status": "restricted", "detail": "Microsoft 请求限流", "evidence": "microsoft_oauth:429"}
    if response.status_code >= 500:
        return {"status": "error", "detail": f"Microsoft 服务异常 HTTP {response.status_code}", "evidence": f"microsoft_oauth:{response.status_code}"}
    return {"status": "unknown", "detail": f"Microsoft OAuth HTTP {response.status_code}", "evidence": f"microsoft_oauth:{response.status_code}"}


def _scan_chatgpt(record: dict, timeout: int) -> dict:
    cookies = record.get("_cookies") or []
    if cookies:
        with _web_session("chatgpt") as session:
            response = session.get(
                "https://chatgpt.com/api/auth/session",
                headers={"Cookie": asset_store._cookie_header(cookies), "Cache-Control": "no-cache"},
                timeout=timeout,
            )
        classified = _response_status(response, "chatgpt_session")
        if classified:
            return classified
        if response.status_code == 200:
            try:
                payload = response.json()
            except Exception:
                payload = {}
            if payload.get("accessToken"):
                return {
                    "status": "normal",
                    "detail": "ChatGPT 登录会话正常",
                    "evidence": "chatgpt_session:200",
                    "_access_token": str(payload["accessToken"]),
                }
            return {"status": "expired", "detail": "ChatGPT 会话未返回 accessToken", "evidence": "chatgpt_session:empty"}
        return {"status": "unknown", "detail": f"ChatGPT HTTP {response.status_code}", "evidence": f"chatgpt_session:{response.status_code}"}

    token = record.get("_token") or {}
    access_token = str(token.get("accessToken") or token.get("access_token") or "").strip()
    if not access_token:
        return {"status": "invalid", "detail": "缺少 ChatGPT session Cookie 或 accessToken", "evidence": "local:missing_credential"}
    with _web_session("chatgpt") as session:
        response = session.get(
            "https://chatgpt.com/backend-api/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        )
    classified = _response_status(response, "chatgpt_token")
    if classified:
        return classified
    if response.status_code == 200:
        return {
            "status": "normal",
            "detail": "ChatGPT accessToken 正常",
            "evidence": "chatgpt_token:200",
            "_access_token": access_token,
        }
    return {"status": "unknown", "detail": f"ChatGPT token HTTP {response.status_code}", "evidence": f"chatgpt_token:{response.status_code}"}


def _chatgpt_plan_type(record: dict) -> str:
    token = record.get("_token") if isinstance(record.get("_token"), dict) else {}
    account = token.get("account") if isinstance(token.get("account"), dict) else {}
    return str(account.get("planType") or token.get("planType") or "").strip().lower()


def _scan_chatgpt_plus_trial(record: dict, access_token: str, timeout: int) -> dict:
    enabled = str(os.environ.get("ASSET_SCAN_CHATGPT_PLUS_TRIAL", "true")).strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return {
            "plus_trial": "disabled",
            "plus_trial_detail": "Plus 试用资格检测已关闭",
            "plus_trial_evidence": "config:disabled",
        }

    plan_type = _chatgpt_plan_type(record)
    if plan_type and plan_type not in {"free", "unknown"}:
        return {
            "plus_trial": "active",
            "plus_trial_detail": f"账号已有 {plan_type} 套餐",
            "plus_trial_evidence": f"session:plan:{plan_type}",
        }
    token = str(access_token or "").strip()
    if not token:
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": "缺少 accessToken，未检测 Plus 试用资格",
            "plus_trial_evidence": "local:missing_access_token",
        }

    campaign = str(
        os.environ.get("ASSET_SCAN_CHATGPT_PLUS_CAMPAIGN", "plus-1-month-free")
    ).strip() or "plus-1-month-free"
    identity = str(record.get("email") or hashlib.sha256(token.encode("utf-8")).hexdigest())
    device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"reg-factory-plus-trial:{identity.lower()}"))
    try:
        with _web_session("chatgpt") as session:
            response = session.get(
                "https://chatgpt.com/backend-api/promo_campaign/check_coupon",
                params={"coupon": campaign, "is_coupon_from_query_param": "true"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://chatgpt.com",
                    "Referer": "https://chatgpt.com/",
                    "oai-device-id": device_id,
                    "x-openai-target-path": "/backend-api/promo_campaign/check_coupon",
                    "x-openai-target-route": "/backend-api/promo_campaign/check_coupon",
                },
                timeout=timeout,
            )
        try:
            payload = response.json() if response.status_code < 500 else {}
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        state = str(payload.get("state") or "").strip().lower()
        redemption = payload.get("redemption") if isinstance(payload.get("redemption"), dict) else {}
        redeemed_by_user = redemption.get("redeemed_by_user") is True
        evidence = f"promo_campaign:{response.status_code}:{state or 'none'}"
        if response.status_code == 200 and state == "eligible" and not redeemed_by_user:
            return {
                "plus_trial": "eligible",
                "plus_trial_detail": "命中 Plus 免费试用资格",
                "plus_trial_evidence": evidence,
            }
        if response.status_code == 200 and (state in {"ineligible", "redeemed", "expired"} or redeemed_by_user):
            return {
                "plus_trial": "ineligible",
                "plus_trial_detail": "当前没有可用的 Plus 免费试用资格",
                "plus_trial_evidence": evidence,
            }
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": f"Plus 资格接口未返回明确结果（HTTP {response.status_code}）",
            "plus_trial_evidence": evidence,
        }
    except requests.Timeout:
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": "Plus 试用资格检测超时",
            "plus_trial_evidence": "promo_campaign:timeout",
        }
    except requests.RequestException as exc:
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": f"Plus 试用资格网络检测失败：{type(exc).__name__}",
            "plus_trial_evidence": "promo_campaign:network_error",
        }
    except Exception as exc:
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": f"Plus 试用资格检测异常：{type(exc).__name__}",
            "plus_trial_evidence": "promo_campaign:error",
        }


def _scan_claude(record: dict, timeout: int) -> dict:
    cookies = record.get("_cookies") or []
    token = record.get("_token") or {}
    session_key = str(token.get("sessionKey") or "").strip()
    if not session_key:
        key_cookie = next((item for item in cookies if item.get("name") == "sessionKey" and item.get("value")), None)
        session_key = str((key_cookie or {}).get("value") or "").strip()
    if not session_key:
        return {"status": "invalid", "detail": "缺少 Claude sessionKey", "evidence": "local:missing_session_key"}
    with _web_session("claude") as session:
        response = session.get(
            "https://claude.ai/api/account",
            headers={"Cookie": f"sessionKey={session_key}"},
            timeout=timeout,
        )
    if "/login" in response.url or "/logout" in response.url:
        return {"status": "expired", "detail": "Claude 已跳转登录页", "evidence": "claude_account:login_redirect"}
    classified = _response_status(response, "claude_account")
    if classified:
        return classified
    if response.status_code == 200:
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict) and (
            "memberships" in payload or payload.get("uuid") or payload.get("email")
        ):
            return {"status": "normal", "detail": "Claude 登录会话正常", "evidence": "claude_account:200"}
        text = (response.text or "").lower()
        if "just a moment" in text or "cf-chl" in text or "cloudflare" in text:
            return {"status": "restricted", "detail": "Claude 返回 Cloudflare 验证页", "evidence": "claude_account:challenge"}
        return {"status": "unknown", "detail": "Claude 未返回有效账号数据", "evidence": "claude_account:empty"}
    return {"status": "unknown", "detail": f"Claude HTTP {response.status_code}", "evidence": f"claude_account:{response.status_code}"}


def _scan_grok(record: dict, timeout: int) -> dict:
    cookies = record.get("_cookies") or []
    token = record.get("_token") or {}
    sso = str(token.get("sso") or "").strip()
    if not sso:
        key_cookie = next(
            (item for item in cookies if item.get("name") in {"sso", "sso-rw"} and item.get("value")),
            None,
        )
        sso = str((key_cookie or {}).get("value") or "").strip()
    if not sso:
        return {"status": "invalid", "detail": "缺少 Grok SSO", "evidence": "local:missing_sso"}
    with _web_session("grok") as session:
        session.cookies.set("sso", sso, domain=".x.ai", path="/")
        session.cookies.set("sso-rw", sso, domain=".x.ai", path="/")
        response = session.get("https://accounts.x.ai/", timeout=timeout, allow_redirects=True)
    if "sign-in" in response.url or "sign-up" in response.url:
        return {"status": "expired", "detail": "Grok SSO 已跳转登录页", "evidence": "xai_account:login_redirect"}
    classified = _response_status(response, "xai_account")
    if classified:
        return classified
    if 200 <= response.status_code < 400:
        return {"status": "normal", "detail": "Grok SSO 登录正常", "evidence": f"xai_account:{response.status_code}"}
    return {"status": "unknown", "detail": f"Grok HTTP {response.status_code}", "evidence": f"xai_account:{response.status_code}"}


def _scan_kiro(record: dict, timeout: int) -> dict:
    token = record.get("_token") or {}
    refresh = str(token.get("refreshToken") or token.get("refresh_token") or "").strip()
    client_id = str(token.get("clientId") or token.get("client_id") or "").strip()
    client_secret = str(token.get("clientSecret") or token.get("client_secret") or "").strip()
    if not refresh or not client_id or not client_secret:
        return {"status": "invalid", "detail": "缺少 Kiro Builder ID 长期凭据", "evidence": "local:missing_credential"}
    with _web_session("kiro") as session:
        response = session.post(
            "https://oidc.us-east-1.amazonaws.com/token",
            json={"clientId": client_id, "clientSecret": client_secret, "refreshToken": refresh, "grantType": "refresh_token"},
            timeout=timeout,
        )
        classified = _response_status(response, "kiro_token")
        if classified:
            return classified
        try:
            payload = response.json()
        except Exception:
            payload = {}
        access = str(payload.get("accessToken") or "").strip()
        if response.status_code != 200 or not access:
            return {"status": "expired", "detail": "Kiro refresh token 未返回访问令牌", "evidence": "kiro_token:empty"}
        usage = session.get(
            "https://q.us-east-1.amazonaws.com/getUsageLimits?origin=AI_EDITOR&resourceType=AGENTIC_REQUEST&isEmailRequired=true",
            headers={"Authorization": f"Bearer {access}"}, timeout=timeout,
        )
        classified = _response_status(usage, "kiro_usage")
        if classified:
            return classified
        if usage.status_code == 200:
            return {"status": "normal", "detail": "Kiro Builder ID 凭据正常", "evidence": "kiro_usage:200"}
        return {"status": "unknown", "detail": f"Kiro usage HTTP {usage.status_code}", "evidence": f"kiro_usage:{usage.status_code}"}


_SCANNERS = {
    "outlook": _scan_outlook,
    "chatgpt": _scan_chatgpt,
    "claude": _scan_claude,
    "grok": _scan_grok,
    "kiro": _scan_kiro,
}


def _scan_record(record: dict, timeout: int) -> dict:
    started = time.monotonic()
    public = _public_record(record)
    try:
        outcome = _SCANNERS[record["platform"]](record, timeout)
    except requests.Timeout:
        outcome = {"status": "error", "detail": "检测请求超时", "evidence": "network:timeout"}
    except requests.RequestException as exc:
        outcome = {"status": "error", "detail": f"网络检测失败：{type(exc).__name__}", "evidence": "network:error"}
    except Exception as exc:
        outcome = {"status": "error", "detail": f"检测异常：{str(exc)[:120]}", "evidence": "scanner:error"}
    if outcome.get("status") not in STATUSES:
        outcome["status"] = "unknown"
    access_token = str(outcome.pop("_access_token", "") or "")
    if record.get("platform") == "chatgpt" and "plus_trial" not in outcome:
        if outcome.get("status") == "normal":
            outcome.update(_scan_chatgpt_plus_trial(record, access_token, timeout))
        else:
            outcome.update({
                "plus_trial": "unknown",
                "plus_trial_detail": "账号状态异常，未检测 Plus 试用资格",
                "plus_trial_evidence": "health:not_normal",
            })
    public.update(outcome)
    public["checked_at"] = outcome.get("checked_at") or _now_iso()
    public["latency_ms"] = round((time.monotonic() - started) * 1000)
    return public


def _record_with_outcome(record: dict, outcome: dict) -> dict:
    public = _public_record(record)
    public.update(outcome)
    public["checked_at"] = _now_iso()
    public["latency_ms"] = 0
    return public


def scan_pool(
    platforms: list[str] | tuple[str, ...] | None = None,
    concurrency: int = 4,
    timeout: int = 15,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    requested = {str(item).strip().lower() for item in (platforms or PLATFORMS)}
    invalid = requested.difference(PLATFORMS)
    if invalid:
        raise ValueError(f"不支持的平台：{', '.join(sorted(invalid))}")
    concurrency = min(12, max(1, int(concurrency)))
    timeout = min(60, max(5, int(timeout)))
    started_at = _now_iso()
    records = _inventory_records()
    selected = [record for record in records if record["platform"] in requested]
    previous = {
        str(item.get("id")): item
        for item in _read_cache().get("items", [])
        if isinstance(item, dict) and item.get("id")
    }
    scanned = {}
    total = len(selected)
    if progress:
        progress({"completed": 0, "total": total, "current": ""})
    records_by_platform = {
        platform: [record for record in selected if record["platform"] == platform]
        for platform in requested
    }
    preflight_failures = {}
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(records_by_platform)))) as executor:
        future_map = {
            executor.submit(_platform_preflight, platform, timeout): platform
            for platform, platform_records in records_by_platform.items()
            if platform_records
        }
        for future in as_completed(future_map):
            outcome = future.result()
            if outcome:
                preflight_failures[future_map[future]] = outcome

    completed = 0
    pending = []
    for record in selected:
        outcome = preflight_failures.get(record["platform"])
        if outcome:
            result = _record_with_outcome(record, outcome)
            scanned[result["id"]] = result
            completed += 1
            if progress:
                progress({
                    "completed": completed,
                    "total": total,
                    "current": result.get("email") or result.get("source") or "",
                })
        else:
            pending.append(record)
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="asset-scan") as executor:
        future_map = {executor.submit(_scan_record, record, timeout): record for record in pending}
        for future in as_completed(future_map):
            result = future.result()
            scanned[result["id"]] = result
            completed += 1
            if progress:
                progress({
                    "completed": completed,
                    "total": total,
                    "current": result.get("email") or result.get("source") or "",
                })

    items = []
    for record in records:
        public = _public_record(record)
        result = scanned.get(public["id"]) or previous.get(public["id"])
        if result:
            public.update({
                key: result[key]
                for key in (
                    "status", "detail", "evidence", "checked_at", "latency_ms",
                    "plus_trial", "plus_trial_detail", "plus_trial_evidence",
                )
                if key in result
            })
        else:
            public.update({"status": "unknown", "detail": "尚未扫描", "evidence": "none", "checked_at": ""})
        if public.get("platform") == "chatgpt":
            public.setdefault("plus_trial", "unknown")
            public.setdefault("plus_trial_detail", "尚未检测 Plus 试用资格")
            public.setdefault("plus_trial_evidence", "none")
        items.append(public)
    finished_at = _now_iso()
    report = {
        "schema_version": 2,
        "started_at": started_at,
        "finished_at": finished_at,
        "platforms_scanned": sorted(requested),
        "items": items,
        "summary": _status_summary(items),
    }
    _write_cache(report)
    return report
