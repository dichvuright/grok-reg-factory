# -*- coding: utf-8 -*-
"""Grok Web SSO -> xAI OAuth conversion through the local proxy."""

import base64
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

from curl_cffi import requests as curl_requests

XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_TOKEN_ENDPOINT = f"{XAI_OAUTH_ISSUER}/oauth2/token"
XAI_CLI_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
GROK_HOME_URL = "https://grok.com/"
XAI_CLI_VERSION = "0.2.93"
XAI_TOKEN_USER_AGENT = (
    f"grok-pager/{XAI_CLI_VERSION} grok-shell/{XAI_CLI_VERSION} "
    "(linux; x86_64)"
)
XAI_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
XAI_CLI_HEADERS = {
    "User-Agent": XAI_TOKEN_USER_AGENT,
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-authenticateresponse": "authenticate-response",
    "x-grok-client-identifier": "grok-pager",
    "x-grok-client-version": XAI_CLI_VERSION,
}
_SSO_COOKIE_DOMAINS = (
    ".x.ai",
    "accounts.x.ai",
    "auth.x.ai",
    ".grok.com",
    "grok.com",
)


def _jwt_claims(token):
    try:
        segment = str(token or "").split(".")[1]
        segment += "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(segment.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _sso_principal_id(sso):
    claims = _jwt_claims(sso)
    for key in ("sub", "principal_id", "user_id", "uid", "id"):
        value = str(claims.get(key) or "").strip()
        if value:
            return value
    return ""


def _trusted_xai_url(raw):
    try:
        parsed = urlparse(str(raw or "").strip())
    except Exception:
        return False
    host = str(parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "x.ai"
        or host.endswith(".x.ai")
        or host == "grok.com"
        or host.endswith(".grok.com")
    )


def _browser_device_verification_url(url):
    """Use Grok's live UI route; accounts.x.ai's legacy route now renders 404."""
    raw = str(url or "").strip()
    prefix = "https://accounts.x.ai/oauth2/device"
    if raw.lower().startswith(prefix):
        return "https://grok.com/oauth2/device" + raw[len(prefix):]
    return raw


def _device_authorized(url="", body=""):
    normalized_url = str(url or "").lower().rstrip("/")
    if "/oauth2/device/done" in normalized_url or normalized_url.endswith("/device/done"):
        return True
    normalized_body = str(body or "").lower()
    markers = (
        "device authorized",
        "you have authorized",
        "device is authorized",
        "authorization complete",
        "设备已授权",
        "已授权此设备",
    )
    return any(marker in normalized_body for marker in markers)


def _response_error(response):
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        detail = payload.get("error_description") or payload.get("error") or payload.get("message")
        if detail:
            return str(detail).strip()[:240]
    return str(getattr(response, "text", "") or "").replace("\n", " ").strip()[:240]


def _request(session, method, url, **kwargs):
    kwargs.setdefault("timeout", 45)
    kwargs.setdefault("allow_redirects", True)
    response = session.request(method, url, **kwargs)
    if response.status_code >= 400:
        detail = _response_error(response)
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"xAI OAuth HTTP {response.status_code}{suffix}")
    return response


def _new_sso_session(sso, proxy):
    session = curl_requests.Session(impersonate="chrome131", http_version="v2")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    session.headers.update({
        "User-Agent": XAI_BROWSER_USER_AGENT,
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    for domain in _SSO_COOKIE_DOMAINS:
        session.cookies.set("sso", sso, domain=domain, path="/")
        session.cookies.set("sso-rw", sso, domain=domain, path="/")
    return session


def _parse_grok_account_state(page_html):
    """Parse the registration risk decision embedded in grok.com's RSC data."""
    normalized = str(page_html or "").replace('\\"', '"')
    source_match = re.search(r'botFlagSource"\s*:\s*(null|-?\d+)', normalized)
    details_match = re.search(
        r'botFlagDetails"\s*:\s*(?:null|"([^"]*)")', normalized
    )

    source = None
    if source_match and source_match.group(1) != "null":
        try:
            source = int(source_match.group(1))
        except (TypeError, ValueError):
            pass
    details = details_match.group(1) if details_match and details_match.group(1) else ""
    fields = {}
    for item in details.split(","):
        key, separator, value = item.partition("=")
        if separator and key.strip():
            fields[key.strip().lower()] = value.strip()
    try:
        risk = float(fields["risk"]) if fields.get("risk") else None
    except (TypeError, ValueError):
        risk = None
    policy = fields.get("policy", "").lower()
    event = fields.get("event", "")
    return {
        "found": bool(source_match or details_match),
        "bot_flag_source": source,
        "bot_flag_details": details,
        "policy": policy,
        "risk": risk,
        "event": event,
        "denied": policy == "deny" and event == "$registration",
    }


def inspect_grok_account_state(sso, proxy="", timeout=20):
    """Read the current Grok risk state; diagnostics failures do not block OAuth."""
    result = _parse_grok_account_state("")
    result.update({"status_code": 0, "url": "", "error": ""})
    sso = str(sso or "").strip()
    if not sso:
        result["error"] = "sso 为空"
        return result

    session = None
    try:
        session = _new_sso_session(sso, proxy)
        response = session.get(
            GROK_HOME_URL,
            headers={
                "User-Agent": XAI_BROWSER_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=timeout,
            allow_redirects=True,
        )
        result["status_code"] = int(getattr(response, "status_code", 0) or 0)
        result["url"] = str(getattr(response, "url", "") or "")
        if result["status_code"] != 200:
            result["error"] = f"grok.com HTTP {result['status_code']}"
            return result
        result.update(_parse_grok_account_state(getattr(response, "text", "") or ""))
        if not result["found"]:
            result["error"] = "grok.com 未发现 botFlag 字段"
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result
    finally:
        if session is not None:
            session.close()


def _new_device_session(proxy):
    """Create a browser-like session for the unauthenticated Device Code request."""
    session = curl_requests.Session(impersonate="chrome131", http_version="v2")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    session.headers.update({
        "User-Agent": XAI_BROWSER_USER_AGENT,
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    })
    return session


def start_grok_device_flow(proxy):
    """Request a Device Code without binding the request to an SSO cookie."""
    session = _new_device_session(proxy)
    try:
        response = _request(
            session,
            "POST",
            f"{XAI_OAUTH_ISSUER}/oauth2/device/code",
            data={"client_id": XAI_CLIENT_ID, "scope": XAI_SCOPE},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": XAI_BROWSER_USER_AGENT,
            },
        )
        payload = response.json()
        payload = payload if isinstance(payload, dict) else {}
        device_code = str(payload.get("device_code") or "").strip()
        user_code = str(payload.get("user_code") or "").strip()
        verification_url = str(
            payload.get("verification_uri_complete")
            or payload.get("verification_url_complete")
            or ""
        ).strip()
        if not verification_url:
            verification_uri = str(
                payload.get("verification_uri") or payload.get("verification_url") or ""
            ).strip()
            if verification_uri:
                separator = "&" if "?" in verification_uri else "?"
                verification_url = f"{verification_uri}{separator}user_code={quote(user_code)}"
        verification_url = _browser_device_verification_url(verification_url)
        if not device_code or not user_code or not _trusted_xai_url(verification_url):
            raise RuntimeError("xAI Device Flow returned incomplete data")
        return {
            "device_code": device_code,
            "user_code": user_code,
            "verification_url": verification_url,
            "interval": payload.get("interval") or 2,
        }
    finally:
        session.close()


def finish_grok_device_flow(proxy, device_code, interval=2, timeout=90, account_email=""):
    """Poll a browser-approved Device Code and return refreshable credentials."""
    session = _new_device_session(proxy)
    try:
        token = _poll_device_token(session, device_code, interval, timeout)
        if not token.get("refresh_token"):
            raise RuntimeError("xAI OAuth did not return a refresh token")
        return _build_credentials(token, account_email=account_email)
    finally:
        session.close()


def _poll_device_token(session, device_code, interval, timeout):
    interval = max(1, min(5, int(interval or 2)))
    started_at = time.time()
    deadline = started_at + max(15, int(timeout))
    invalid_grant_grace = started_at + 8
    last_error = ""

    while time.time() < deadline:
        response = session.post(
            XAI_TOKEN_ENDPOINT,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": XAI_CLIENT_ID,
                "device_code": device_code,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": XAI_TOKEN_USER_AGENT,
                "X-Grok-Client-Version": XAI_CLI_VERSION,
            },
            timeout=45,
        )
        try:
            payload = response.json()
        except Exception:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        if response.status_code < 300 and payload.get("access_token"):
            return payload

        error = str(payload.get("error") or "").strip()
        description = str(payload.get("error_description") or "").strip()
        last_error = description or error or f"HTTP {response.status_code}"
        if error == "slow_down":
            interval = min(interval + 5, 30)
        elif error == "authorization_pending":
            pass
        elif error == "invalid_grant" and time.time() < invalid_grant_grace:
            pass
        else:
            raise RuntimeError(f"xAI OAuth 换取 token 失败: {last_error}")
        time.sleep(interval)

    raise RuntimeError(f"xAI Device Flow 等待 token 超时: {last_error or 'no token'}")


def _build_credentials(token, account_email=""):
    access_claims = _jwt_claims(token.get("access_token"))
    id_claims = _jwt_claims(token.get("id_token"))
    email = str(
        id_claims.get("email")
        or access_claims.get("email")
        or account_email
        or ""
    ).strip()
    subject = str(access_claims.get("sub") or id_claims.get("sub") or "").strip()
    team_id = str(access_claims.get("team_id") or id_claims.get("team_id") or "").strip()
    expires_at = access_claims.get("exp")
    try:
        expires_at = int(expires_at)
    except (TypeError, ValueError):
        expires_at = int(time.time()) + int(token.get("expires_in") or 21600)
    expires_iso = datetime.fromtimestamp(
        expires_at, tz=timezone.utc
    ).isoformat().replace("+00:00", "Z")

    credentials = {
        "access_token": token["access_token"],
        "refresh_token": token["refresh_token"],
        "token_type": token.get("token_type") or "Bearer",
        "client_id": XAI_CLIENT_ID,
        "scope": token.get("scope") or XAI_SCOPE,
        "expires_in": int(token.get("expires_in") or 21600),
        "expires_at": expires_iso,
        "token_endpoint": XAI_TOKEN_ENDPOINT,
        "base_url": XAI_CLI_BASE_URL,
        "headers": dict(XAI_CLI_HEADERS),
    }
    if email:
        credentials["email"] = email
    if subject:
        credentials["sub"] = subject
    if team_id:
        credentials["team_id"] = team_id
    if token.get("id_token"):
        credentials["id_token"] = token["id_token"]
    return credentials, email


def convert_grok_sso_local(sso, proxy, account_email="", timeout=90):
    """Convert a Grok Web SSO cookie into refreshable sub2api credentials."""
    sso = str(sso or "").strip()
    proxy = str(proxy or "").strip()
    if not sso:
        raise ValueError("缺少 grok sso")
    if not proxy:
        raise ValueError("缺少本机 Grok OAuth 代理")

    session = _new_sso_session(sso, proxy)
    try:
        response = _request(session, "GET", "https://accounts.x.ai/")
        final_url = str(response.url or "")
        if response.status_code == 401 or "sign-in" in final_url or "sign-up" in final_url:
            raise RuntimeError("Grok Web SSO 已失效")

        response = _request(
            session,
            "POST",
            f"{XAI_OAUTH_ISSUER}/oauth2/device/code",
            data={"client_id": XAI_CLIENT_ID, "scope": XAI_SCOPE},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": XAI_BROWSER_USER_AGENT,
            },
        )
        device = response.json()
        device_code = str(device.get("device_code") or "").strip()
        user_code = str(device.get("user_code") or "").strip()
        verify_url = str(
            device.get("verification_uri_complete")
            or device.get("verification_url_complete")
            or ""
        ).strip()
        if not verify_url:
            verification_uri = str(
                device.get("verification_uri") or device.get("verification_url") or ""
            ).strip()
            if verification_uri:
                separator = "&" if "?" in verification_uri else "?"
                verify_url = f"{verification_uri}{separator}user_code={quote(user_code)}"
        if not device_code or not user_code or not _trusted_xai_url(verify_url):
            raise RuntimeError("xAI Device Flow 返回不完整或验证地址不受信任")

        _request(session, "GET", verify_url)
        response = _request(
            session,
            "POST",
            f"{XAI_OAUTH_ISSUER}/oauth2/device/verify",
            data={"user_code": user_code},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Origin": "https://accounts.x.ai",
                "Referer": verify_url,
                "User-Agent": XAI_BROWSER_USER_AGENT,
            },
        )
        authorize_url = str(response.url or "")
        if "sign-in" in authorize_url or "sign-up" in authorize_url:
            raise RuntimeError("xAI Device Flow 会话已失效")

        if not _device_authorized(authorize_url):
            response = _request(
                session,
                "POST",
                f"{XAI_OAUTH_ISSUER}/oauth2/device/approve",
                data={
                    "user_code": user_code,
                    "action": "allow",
                    "principal_type": "User",
                    "principal_id": _sso_principal_id(sso),
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                    "Origin": "https://accounts.x.ai",
                    "Referer": authorize_url or verify_url,
                    "User-Agent": XAI_BROWSER_USER_AGENT,
                },
            )
            if not _device_authorized(response.url, response.text):
                raise RuntimeError(
                    f"xAI Device Flow 授权未完成: {str(response.url or '')[:160]}"
                )

        token = _poll_device_token(
            session,
            device_code,
            device.get("interval") or 2,
            timeout,
        )
        if not token.get("refresh_token"):
            raise RuntimeError("xAI OAuth 未返回 refresh_token")
        return _build_credentials(token, account_email=account_email)
    finally:
        session.close()
