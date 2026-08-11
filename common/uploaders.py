# -*- coding: utf-8 -*-
"""
common/uploaders.py — 把本地标准 token 上传到下游管理接口。

移植自 FlowPilot(QLHazyCoder/FlowPilot):
  - upload_cpa          background/cpa-api.js: importCurrentChatGptSession
  - upload_sub2api      background/sub2api-api.js: loginSub2Api/getGroupsByNames/importCurrentChatGptSession
  - upload_webchat2api  flows/grok/background/publisher-webchat2api.js: uploadGrokSsoToWebchat2Api

每个函数返回 (ok: bool, message: str)。仅在被 tools/upload_tokens.py 调用时使用。
"""

import json
import time
from urllib.parse import urlparse, quote

import requests

DEFAULT_TIMEOUT = 30
GROK_IMPORT_TIMEOUT = 180
DEFAULT_CONCURRENCY = 10
DEFAULT_PRIORITY = 1
DEFAULT_RATE_MULTIPLIER = 1


def _origin(url):
    p = urlparse(url if "://" in (url or "") else f"http://{url}")
    if not p.scheme or not p.netloc:
        raise ValueError(f"地址格式无效: {url}")
    return f"{p.scheme}://{p.netloc}"


def _msg_from_payload(payload, status, fallback=""):
    if isinstance(payload, dict):
        for key in ("message", "detail", "error", "reason"):
            v = payload.get(key)
            if isinstance(v, dict):
                v = v.get("message") or v.get("error")
            if v:
                return str(v).strip()
    return fallback or f"HTTP {status}"


# ============================================================ CPA
def upload_cpa(base_url, mgmt_key, auth_json, file_name, timeout=DEFAULT_TIMEOUT):
    """POST {origin}/v0/management/auth-files?name=<file_name>，body=auth_json。"""
    try:
        origin = _origin(base_url)
        if not mgmt_key:
            return False, "缺少 CPA 管理密钥"
        url = f"{origin}/v0/management/auth-files?name={quote(file_name)}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {mgmt_key}",
            "X-Management-Key": mgmt_key,
        }
        resp = requests.post(url, headers=headers, json=auth_json, timeout=timeout)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if not resp.ok:
            return False, _msg_from_payload(payload, resp.status_code, "CPA 导入失败")
        return True, _msg_from_payload(payload, resp.status_code, "CPA 导入成功") if isinstance(payload, dict) and payload else "CPA 导入成功"
    except requests.RequestException as e:
        return False, f"CPA 请求异常: {e}"
    except Exception as e:
        return False, str(e)


# ============================================================ SUB2API
def _sub2api_request(origin, path, token=None, method="GET", body=None,
                     timeout=DEFAULT_TIMEOUT, retries=4, use_env_proxy=True):
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # 出口节点(机房代理)偶发 TLS 抖动(SSLEOFError)/连接重置，单发就失败会白白中断整轮
    # OAuth(已开浏览器)。对连接类错误小退避重试几次，业务错误(4xx/code!=0)不重试。
    last_exc = None
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        try:
            if use_env_proxy:
                resp = requests.request(
                    method,
                    f"{origin}{path}",
                    headers=headers,
                    data=None if body is None else json.dumps(body),
                    timeout=timeout,
                )
            else:
                session = requests.Session()
                session.trust_env = False
                try:
                    resp = session.request(
                        method,
                        f"{origin}{path}",
                        headers=headers,
                        data=None if body is None else json.dumps(body),
                        timeout=timeout,
                    )
                finally:
                    session.close()
            break
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt < attempts - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    # SUB2API 约定 {code:0, data}
    if isinstance(payload, dict) and "code" in payload:
        if int(payload.get("code")) == 0:
            return payload.get("data")
        raise RuntimeError(_msg_from_payload(payload, resp.status_code, f"SUB2API 失败: {path}"))
    if not resp.ok:
        raise RuntimeError(_msg_from_payload(payload, resp.status_code, f"SUB2API 失败: {path}"))
    return payload


def _sub2api_login(origin, email, password, timeout=DEFAULT_TIMEOUT):
    if not email or not password:
        raise ValueError("缺少 SUB2API 登录邮箱/密码")
    login = _sub2api_request(
        origin,
        "/api/v1/auth/login",
        method="POST",
        body={"email": email, "password": password},
        timeout=timeout,
    )
    token = ""
    if isinstance(login, dict):
        token = str(login.get("access_token") or login.get("accessToken") or "").strip()
    if not token:
        raise RuntimeError("SUB2API 登录返回缺少 access_token")
    return token


def _sub2api_group_id(origin, token, group, platform, timeout=DEFAULT_TIMEOUT):
    target = str(group or platform).strip().lower()
    groups = _sub2api_request(
        origin, "/api/v1/admin/groups/all", token=token, timeout=timeout
    )
    for item in (groups or []):
        name = str(item.get("name") or "").strip().lower()
        item_platform = str(item.get("platform") or "").strip().lower()
        # 老版本 OpenAI 分组没有 platform 字段；Grok 必须显式为 grok，避免串渠道。
        platform_matches = item_platform == platform or (platform == "openai" and not item_platform)
        if name == target and platform_matches:
            return int(item["id"])
    raise RuntimeError(f"SUB2API 未找到 {platform} 分组: {group or platform}")


def upload_sub2api(base_url, email, password, group, content,
                   expires_at=None, priority=DEFAULT_PRIORITY, timeout=DEFAULT_TIMEOUT):
    """登录 -> 找分组 -> import/codex-session。group 是分组名(字符串)。"""
    try:
        origin = _origin(base_url)
        token = _sub2api_login(origin, email, password, timeout=timeout)
        group_id = _sub2api_group_id(
            origin, token, group or "codex", "openai", timeout=timeout
        )

        payload = {
            "content": content,
            "group_ids": [int(group_id)],
            "priority": int(priority),
            "auto_pause_on_expired": True,
            "update_existing": True,
        }
        if expires_at:
            payload["expires_at"] = int(expires_at)

        result = _sub2api_request(origin, "/api/v1/admin/accounts/import/codex-session",
                                  token=token, method="POST", body=payload, timeout=timeout)
        result = result if isinstance(result, dict) else {}
        created = int(result.get("created") or 0)
        updated = int(result.get("updated") or 0)
        failed = int(result.get("failed") or 0)
        if failed > 0 or (created <= 0 and updated <= 0):
            return False, f"SUB2API 导入未成功(新建{created}/更新{updated}/失败{failed})"
        return True, f"SUB2API 导入完成(新建{created}/更新{updated})"
    except requests.RequestException as e:
        return False, f"SUB2API 请求异常: {e}"
    except Exception as e:
        return False, str(e)


def _create_sub2api_grok_oauth(origin, token, group_id, credentials, account_email,
                               concurrency, priority, timeout, proxy_id=None):
    name = str(credentials.get("email") or account_email or "Grok OAuth Account").strip()
    existing = _sub2api_request(
        origin,
        f"/api/v1/admin/accounts?page=1&page_size=100&platform=grok&search={quote(name)}",
        token=token,
        timeout=timeout,
        retries=1,
        use_env_proxy=False,
    )
    items = existing.get("items", []) if isinstance(existing, dict) else []
    payload = {
        "name": name,
        "type": "oauth",
        "credentials": credentials,
        "concurrency": max(1, int(concurrency)),
        "priority": int(priority),
        "rate_multiplier": DEFAULT_RATE_MULTIPLIER,
        "group_ids": [int(group_id)],
        "auto_pause_on_expired": True,
    }
    if proxy_id:
        payload["proxy_id"] = int(proxy_id)

    for item in items:
        if str(item.get("name") or "").strip().lower() != name.lower():
            continue
        account_id = item.get("id")
        if not account_id:
            continue
        update_payload = dict(payload)
        update_payload["status"] = "active"
        _sub2api_request(
            origin,
            f"/api/v1/admin/accounts/{int(account_id)}",
            token=token,
            method="PUT",
            body=update_payload,
            timeout=timeout,
            retries=1,
            use_env_proxy=False,
        )
        return True, f"SUB2API Grok 账号凭据已修复({name}, id={account_id})"

    payload.update({
        "notes": "local Grok SSO conversion",
        "platform": "grok",
        "extra": {"email": name} if "@" in name else {},
    })
    account = _sub2api_request(
        origin,
        "/api/v1/admin/accounts",
        token=token,
        method="POST",
        body=payload,
        timeout=timeout,
        retries=1,
        use_env_proxy=False,
    )
    account = account if isinstance(account, dict) else {}
    if not account.get("id"):
        return False, "SUB2API Grok 本机 OAuth 建号未返回账号 ID"
    return True, f"SUB2API Grok 导入完成({name}, id={account['id']}, 本机 OAuth)"


def upload_sub2api_grok(base_url, email, password, group, sso, account_email="",
                        concurrency=DEFAULT_CONCURRENCY, priority=DEFAULT_PRIORITY,
                        proxy_id=None, local_proxy="", timeout=GROK_IMPORT_TIMEOUT,
                        oauth_credentials=None):
    """把 Grok Web SSO 转为 SUB2API Grok OAuth 账号并绑定 Grok 分组。"""
    try:
        origin = _origin(base_url)
        sso = str(sso or "").strip()
        if not sso:
            return False, "缺少 grok sso"

        has_browser_oauth = (
            isinstance(oauth_credentials, dict)
            and bool(oauth_credentials.get("refresh_token"))
        )
        if local_proxy and not has_browser_oauth:
            from common.grok_oauth import inspect_grok_account_state

            state = inspect_grok_account_state(sso, local_proxy)
            if state.get("denied"):
                details = state.get("bot_flag_details") or "policy=deny,event=$registration"
                return False, (
                    "Grok OAuth 已跳过: xAI 注册风控拒绝 "
                    f"(botFlagSource={state.get('bot_flag_source')}, {details})"
                )

        token = _sub2api_login(origin, email, password, timeout=timeout)
        group_id = _sub2api_group_id(
            origin, token, group or "grok", "grok", timeout=timeout
        )

        # Credentials approved in the still-live registration browser avoid a
        # second HTTP-only consent attempt that xAI may reject for a new SSO.
        if has_browser_oauth:
            return _create_sub2api_grok_oauth(
                origin,
                token,
                group_id,
                oauth_credentials,
                oauth_credentials.get("email") or account_email,
                concurrency,
                priority,
                timeout,
                proxy_id=proxy_id,
            )

        # sub2api 的 SSO 转换实现随版本变化。优先在注册所用出口本地换取
        # refreshable OAuth，避免旧版服务端虽然建号成功、实际调用却持续 401。
        local_error = None
        if local_proxy:
            from common.grok_oauth import convert_grok_sso_local

            local_credentials = None
            local_email = ""
            for attempt in range(2):
                try:
                    local_credentials, local_email = convert_grok_sso_local(
                        sso, local_proxy, account_email=account_email
                    )
                    break
                except Exception as e:
                    local_error = e
                    if attempt == 0:
                        time.sleep(2)
            if local_credentials is not None:
                return _create_sub2api_grok_oauth(
                    origin,
                    token,
                    group_id,
                    local_credentials,
                    local_email or account_email,
                    concurrency,
                    priority,
                    timeout,
                    proxy_id=proxy_id,
                )

        body = {
            "sso_tokens": [sso],
            "group_ids": [group_id],
            "concurrency": max(1, int(concurrency)),
            "priority": int(priority),
            "rate_multiplier": DEFAULT_RATE_MULTIPLIER,
        }
        if account_email:
            body["name"] = str(account_email).strip()
        if proxy_id:
            body["proxy_id"] = int(proxy_id)
        result = _sub2api_request(
            origin,
            "/api/v1/admin/grok/sso-to-oauth",
            token=token,
            method="POST",
            body=body,
            timeout=timeout,
            # 建号请求超时后状态未知，自动重放可能生成重复账号。
            retries=1,
        )
        result = result if isinstance(result, dict) else {}
        created = result.get("created") if isinstance(result.get("created"), list) else []
        failed = result.get("failed") if isinstance(result.get("failed"), list) else []
        if len(created) == 1 and not failed:
            item = created[0] if isinstance(created[0], dict) else {}
            imported_email = item.get("email") or account_email or "unknown"
            return True, f"SUB2API Grok 导入完成({imported_email})"
        errors = []
        for item in failed:
            if isinstance(item, dict) and item.get("error"):
                errors.append(str(item["error"]))
        detail = "; ".join(errors) or f"新建{len(created)}/失败{len(failed)}"
        if local_error:
            return False, f"本机 OAuth 失败: {local_error}; SUB2API 远端导入失败: {detail}"
        return False, f"SUB2API Grok 导入未成功: {detail}"
    except requests.RequestException as e:
        return False, f"SUB2API 请求异常: {e}"
    except Exception as e:
        return False, str(e)


# ============================================================ webchat2api (Grok SSO)
def upload_webchat2api(base_url, admin_key, sso, timeout=DEFAULT_TIMEOUT):
    """POST {origin}/api/remote-account/inject，注入 grok sso。"""
    try:
        origin = _origin(base_url)
        if not admin_key:
            return False, "缺少 webchat2api 管理密钥"
        if not sso:
            return False, "缺少 grok sso"
        url = f"{origin}/api/remote-account/inject"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {admin_key}",
        }
        body = {
            "accounts": [{"token": sso, "provider": "grok", "type": "sso"}],
            "strategy": "merge",
            "source_id": "flowpilot-grok-sso",
            "source_name": "FlowPilot Grok SSO",
            "provider": "grok",
        }
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if not resp.ok:
            return False, _msg_from_payload(payload, resp.status_code, "webchat2api 上传失败")
        if isinstance(payload, dict) and "code" in payload and int(payload.get("code")) != 0:
            return False, _msg_from_payload(payload, resp.status_code, f"code={payload.get('code')}")
        return True, _msg_from_payload(payload, resp.status_code, "上传成功") if isinstance(payload, dict) and payload else "上传成功"
    except requests.RequestException as e:
        return False, f"webchat2api 请求异常: {e}"
    except Exception as e:
        return False, str(e)
