# -*- coding: utf-8 -*-
"""
common/oauth_codex.py — 走 Codex CLI OAuth 给 SUB2API 创建带 refresh_token 的 openai 账号。

为什么:网页 /api/auth/session 的 accessToken 没有 refresh_token，SUB2API 当 oauth 账号
无法续期 → 401。正确做法是走 OAuth 授权码换取正式凭据(含 refresh_token)。

SUB2API 包办 PKCE/换码，三步:
  1. POST /api/v1/admin/openai/generate-auth-url {redirect_uri} -> {auth_url, session_id}
     auth_url 走 auth.openai.com/oauth/authorize?client_id=app_EMoamEEZ73f0CkXaXp7hrann
     &scope=openid profile email offline_access&code_challenge=...(S256)&state=...
  2. [浏览器] 在已登录该账号的窗口打开 auth_url → 同意 → 跳
     http://localhost:1455/auth/callback?code=...&state=...(浏览器拦截此 URL 拿 code/state)
  3. POST /api/v1/admin/openai/exchange-code {session_id, code, state} -> 凭据(含 refresh_token)
     再 POST /api/v1/admin/accounts 建 type=oauth 账号。
"""

import asyncio
import re
import sys
import time
from urllib.parse import urlparse, parse_qs

from common.uploaders import _origin, _sub2api_request, DEFAULT_TIMEOUT

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_CONCURRENCY = 10
DEFAULT_PRIORITY = 1
DEFAULT_RATE_MULTIPLIER = 1
# 授权页可能出现的"同意/继续"按钮文案(多语言：英/简中/繁中/日/马来)
# 注：代理 IP 地区决定 OpenAI 界面语言(JP 节点→日文、HK/TW→繁中、MY→马来)，必须覆盖全，
# 否则 data-dd-action-name/submit 选择器漏掉时，纯靠文案匹配会因缺语种而点不到、卡死。
CONSENT_LABELS = [
    "Authorize", "Allow", "Continue", "Approve", "Yes", "Accept",
    "Continue with ChatGPT", "Log in with ChatGPT", "Authorize access",
    # 简中
    "同意", "授权", "允许", "继续", "确认", "登录", "继续使用 ChatGPT",
    # 繁中
    "繼續", "授權", "允許", "確認", "登入", "繼續使用 ChatGPT",
    # 日文
    "続行", "許可", "許可する", "承認", "承認する", "認証", "同意する",
    "続ける", "次へ", "はい", "ChatGPT で続行",
    # 马来
    "Teruskan", "Benarkan", "Sahkan", "Setuju",
]


# ============================================================ SUB2API 调用
def sub2api_login(origin, email, password, timeout=DEFAULT_TIMEOUT):
    d = _sub2api_request(origin, "/api/v1/auth/login", method="POST",
                         body={"email": email, "password": password}, timeout=timeout)
    token = ""
    if isinstance(d, dict):
        token = str(d.get("access_token") or d.get("accessToken") or "").strip()
    if not token:
        raise RuntimeError("SUB2API 登录失败，无 access_token")
    return token


def find_group_id(origin, token, group_name, timeout=DEFAULT_TIMEOUT):
    target = str(group_name or "codex").strip().lower()
    groups = _sub2api_request(origin, "/api/v1/admin/groups/all", token=token, timeout=timeout) or []
    for g in groups:
        name = str(g.get("name") or "").strip().lower()
        platform = g.get("platform")
        if name == target and (not platform or platform == "openai"):
            return g.get("id")
    raise RuntimeError(f"SUB2API 未找到 openai 分组: {group_name}")


def generate_auth_url(origin, token, redirect_uri=REDIRECT_URI, timeout=DEFAULT_TIMEOUT):
    body = {"redirect_uri": redirect_uri}
    d = _sub2api_request(origin, "/api/v1/admin/openai/generate-auth-url",
                         token=token, method="POST", body=body, timeout=timeout)
    auth_url = str((d or {}).get("auth_url") or (d or {}).get("authUrl") or "").strip()
    session_id = str((d or {}).get("session_id") or (d or {}).get("sessionId") or "").strip()
    state = str((d or {}).get("state") or "").strip() or _state_from_url(auth_url)
    if not auth_url or not session_id:
        raise RuntimeError("SUB2API 未返回完整 auth_url / session_id")
    return auth_url, session_id, state


def _state_from_url(url):
    try:
        return parse_qs(urlparse(url).query).get("state", [""])[0]
    except Exception:
        return ""


def exchange_code(origin, token, session_id, code, state, timeout=60):
    body = {"session_id": session_id, "code": code, "state": state}
    return _sub2api_request(origin, "/api/v1/admin/openai/exchange-code",
                            token=token, method="POST", body=body, timeout=timeout)


def build_oauth_credentials(exchange_data):
    """对齐 sub2api-api.js buildOpenAiCredentials。无 access_token 抛错。"""
    cred = {}
    for k in ("access_token", "refresh_token", "id_token", "expires_at", "email",
              "chatgpt_account_id", "chatgpt_user_id", "organization_id", "plan_type", "client_id"):
        v = (exchange_data or {}).get(k)
        if v not in (None, "", []):
            cred[k] = v
    if not cred.get("access_token"):
        raise RuntimeError("exchange-code 未返回 access_token")
    return cred


def create_oauth_account(origin, token, credentials, group_ids, name="",
                         priority=DEFAULT_PRIORITY, timeout=60):
    payload = {
        "name": name or credentials.get("email") or "codex-oauth",
        "notes": "",
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "concurrency": DEFAULT_CONCURRENCY,
        "priority": int(priority),
        "rate_multiplier": DEFAULT_RATE_MULTIPLIER,
        "group_ids": [int(g) for g in group_ids if g],
        "auto_pause_on_expired": True,
    }
    extra = {}
    for k in ("email", "plan_type"):
        if credentials.get(k):
            extra[k] = credentials[k]
    if extra:
        payload["extra"] = extra
    return _sub2api_request(origin, "/api/v1/admin/accounts",
                            token=token, method="POST", body=payload, timeout=timeout)


# ============================================================ 浏览器驱动授权
async def _has_phone_error(page):
    """add-phone 页是否出现"号码不可用/无效"类报错。"""
    try:
        txt = (await page.inner_text("body")).lower()
    except Exception:
        return False
    for kw in ["can't be used", "cannot be used", "not valid", "invalid", "unable to",
               "try another", "different phone", "not supported", "already", "too many"]:
        if kw in txt:
            return True
    return False


def _is_phone_flow_url(url):
    """Return whether *url* is any step of the phone verification flow."""
    path = urlparse(str(url or "")).path.lower()
    return "/add-phone" in path or "/phone" in path


def _is_authorization_advanced_url(url):
    """Return whether OAuth has advanced beyond phone verification."""
    value = str(url or "").lower()
    return any(
        marker in value
        for marker in (
            "/consent",
            "sign-in-with-chatgpt",
            "localhost:1455",
            "127.0.0.1:1455",
        )
    )


async def _select_sms_if_present(page):
    """add-phone「コードの受け取り方法」可能有 WhatsApp / SMS 切换，默认常选中 WhatsApp。
    接码平台(sms-man/firefox)发的是 **SMS**，必须切到 SMS 否则码发去 WhatsApp、脚本永远收不到。
    尽力点选 SMS（按文案/role 多策略），点不到返回 False（不报错）。"""
    labels = ["SMS", "Text message", "Text", "テキスト", "ショートメッセージ", "短信", "簡訊", "簡訊/短信"]
    for lbl in labels:
        for getter in (
            lambda l=lbl: page.get_by_role("radio", name=l, exact=False),
            lambda l=lbl: page.get_by_role("button", name=l, exact=False),
            lambda l=lbl: page.get_by_role("tab", name=l, exact=False),
            lambda l=lbl: page.locator(f'button:has-text("{l}")'),
            lambda l=lbl: page.locator(f'label:has-text("{l}")'),
            lambda l=lbl: page.get_by_text(l, exact=True),
        ):
            try:
                loc = getter()
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=3000)
                    await asyncio.sleep(0.6)
                    print(f"  [add-phone] 已选 SMS（匹配 '{lbl}'）")
                    return True
            except Exception:
                pass
    return False


async def _fill_phone_continue(page, country_code, national):
    """填手机号(优先整条 E.164，react-aria 多数会自动识别国家)，选 SMS 发码方式，再点 Continue。"""
    full = ("+" + country_code + national) if country_code else ("+" + national)
    tel = page.locator("#tel")
    await tel.wait_for(state="visible", timeout=15000)
    await tel.click()
    try:
        await tel.fill("")
    except Exception:
        pass
    await tel.type(full, delay=25)
    await asyncio.sleep(1.0)
    # 关键：接码平台发的是 SMS，必须把发码方式从默认 WhatsApp 切到 SMS（同页 toggle）
    await _select_sms_if_present(page)
    btn = page.locator('button[data-dd-action-name="Continue"], button[type="submit"]')
    await btn.first.click(timeout=6000)
    await asyncio.sleep(2.0)
    # 提交后若弹出独立的发码方式选择页，再切一次 SMS
    await _select_sms_if_present(page)


async def _select_whatsapp_if_present(page):
    """add-phone 填号后可能出现"短信/WhatsApp"发码方式选择。尽力点选 WhatsApp（按文案/role
    多策略匹配），点不到就返回 False（不报错，留给调用方决定）。"""
    labels = ["WhatsApp", "Whatsapp", "whatsapp", "通过 WhatsApp", "用 WhatsApp"]
    for lbl in labels:
        for getter in (
            lambda l=lbl: page.get_by_role("radio", name=l, exact=False),
            lambda l=lbl: page.get_by_role("button", name=l, exact=False),
            lambda l=lbl: page.get_by_text(l, exact=False),
            lambda l=lbl: page.locator(f'label:has-text("{l}")'),
        ):
            try:
                loc = getter()
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=3000)
                    await asyncio.sleep(0.8)
                    print(f"  [add-phone] 已选 WhatsApp（匹配 '{lbl}'）")
                    return True
            except Exception:
                pass
    return False


async def _semi_fill_phone(page, full_e164):
    """半自动：填整条 E.164 号 → 选 WhatsApp（若有选项）→ 点发送/Continue。
    成功推进返回 True。任何关键步骤异常返回 False，并由调用方 dump 页面+转手动。"""
    tel = page.locator("#tel")
    await tel.wait_for(state="visible", timeout=15000)
    await tel.click()
    try:
        await tel.fill("")
    except Exception:
        pass
    await tel.type(full_e164, delay=25)
    await asyncio.sleep(0.8)
    # 先尝试在填号页就地选 WhatsApp（有些版本是同页 radio）
    await _select_whatsapp_if_present(page)
    # 点 Continue/发送
    btn = page.locator('button[data-dd-action-name="Continue"], button[type="submit"]')
    if await btn.count() == 0 or not await btn.first.is_visible():
        return False
    await btn.first.click(timeout=6000)
    await asyncio.sleep(2.0)
    # 提交后可能弹出发码方式选择页，再试一次 WhatsApp
    await _select_whatsapp_if_present(page)
    return True


async def _enter_otp(page, code):
    """用真实键盘事件填入 OTP，并确认可见输入框已经接收验证码。"""
    from common.browser import react_fill

    code = str(code or "").strip()
    selector = (
        'input[autocomplete="one-time-code"]:visible, '
        'input[name*="code" i]:visible, '
        'input[inputmode="numeric"]:visible, '
        'input[type="tel"]:visible'
    )
    inp = page.locator(selector)
    await inp.first.wait_for(state="visible", timeout=20000)
    cnt = await inp.count()

    committed = False
    if cnt > 1 and cnt >= len(code):
        # 分段 OTP 通常会自动跳到下一格，从第一格一次键入最接近人工操作。
        try:
            await inp.first.click(timeout=4000)
            await page.keyboard.type(code, delay=70)
            await asyncio.sleep(0.5)
            values = [await inp.nth(i).input_value() for i in range(len(code))]
            committed = "".join(values) == code
        except Exception:
            committed = not _is_phone_flow_url(page.url)

        # 某些页面不自动跳格，逐格发送真实键盘事件兜底。
        if not committed and _is_phone_flow_url(page.url):
            values = []
            for i, ch in enumerate(code):
                field = inp.nth(i)
                try:
                    await field.click(timeout=2500)
                    await field.press("Control+A", timeout=1500)
                    await field.press("Delete", timeout=1500)
                    await page.keyboard.type(ch, delay=70)
                    values.append(await field.input_value())
                except Exception:
                    values.append("")
            committed = "".join(values) == code
    else:
        committed = await react_fill(
            page, selector, code, tries=3, delay=70, verbose=False, settle=0.5
        )

    if not committed and not _is_phone_flow_url(page.url):
        committed = True
    if not committed:
        return False

    await asyncio.sleep(0.5)
    if not _is_phone_flow_url(page.url):
        return True

    try:
        button = page.locator(
            'button[type="submit"]:visible, '
            'button[data-dd-action-name="Continue"]:visible'
        )
        if await button.count() and await button.first.is_visible():
            await button.first.click(timeout=4000)
        else:
            await inp.first.press("Enter", timeout=2000)
    except Exception:
        try:
            await inp.first.press("Enter", timeout=2000)
        except Exception:
            pass
    return True


async def _wait_for_phone_flow_exit(page, timeout=20):
    """Wait until phone verification reaches consent or the OAuth callback."""
    deadline = time.time() + timeout
    while time.time() <= deadline:
        if _is_authorization_advanced_url(page.url):
            return True
        if not _is_phone_flow_url(page.url):
            return False
        if await _has_phone_error(page):
            return False
        await asyncio.sleep(0.5)
    return False


async def _goto_add_phone(page, auth_url, account_email, timeout=45):
    """(重新)走到 add-phone 输手机号页:导航 auth_url → 选账号 → 落到 add-phone 且 #tel 可见。
    用于换号前把页面退回干净的输手机号状态。"""
    # 若 OTP 页有"换个号码/返回"入口，先点(更轻);点不到就重新导航
    for lbl in ["Use a different phone number", "Change phone number", "Edit", "Back",
                "换个号码", "更改手机号", "返回", "重新输入"]:
        try:
            loc = page.get_by_role("link", name=lbl, exact=False)
            if await loc.count() == 0:
                loc = page.get_by_role("button", name=lbl, exact=False)
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click(timeout=2500)
                await asyncio.sleep(1.5)
                break
        except Exception:
            pass
    try:
        await page.goto(auth_url, timeout=30000, wait_until="domcontentloaded")
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = page.url
        if _is_phone_flow_url(url):
            try:
                await page.locator("#tel").wait_for(state="visible", timeout=4000)
                return True
            except Exception:
                pass
        if "choose-an-account" in url or "/account" in url:
            await _click_account(page, account_email)
            await asyncio.sleep(2)
            continue
        if "auth.openai.com" in url and any(
            part in url for part in ("/log-in", "/email-verification")
        ):
            print("  [add-phone] 换号授权要求重新登录，自动完成邮箱验证...")
            ok, message = await _complete_auth_email_login(page, account_email)
            if not ok:
                print(f"  [add-phone] 重新登录失败: {message}")
                return False
            deadline = time.time() + timeout
            await asyncio.sleep(1.5)
            continue
        # 其它中间页(同意等)，点推进按钮
        for lbl in CONSENT_LABELS:
            try:
                b = page.get_by_role("button", name=lbl, exact=False)
                if await b.count() > 0 and await b.first.is_visible():
                    await b.first.click(timeout=2000)
                    break
            except Exception:
                pass
        await asyncio.sleep(1.5)
    return False


async def handle_add_phone(page, auth_url="", account_email="", attempts=None, sms_timeout=None, sms_provider="auto"):
    """auth.openai.com/add-phone:接码平台租号→填→收码→提交，被拒/收不到码就**回退页面**换号重试。
    成功(离开 add-phone)返回 True。

    接码 provider 顺序：sms-man.com 优先(配 SMSMAN_TOKEN 即启用，SMS 直收，匹配本函数
    自动填号→输码路径) → firefox.fun → hero-sms。未配 sms-man 时仅走后两者，OpenAI 对普通
    虚拟号风控严，命中率低，可改用 --codex-manual-phone 手动填号收码。
    换号次数/单号等码超时可经环境变量调：CODEX_ADDPHONE_ATTEMPTS(默认2)、CODEX_SMS_TIMEOUT(默认150)。
    OpenAI 对虚拟号拒收率高，但接码花钱，默认只换 2 次(够碰运气、不烧号)。
    """
    import os as _os
    if attempts is None:
        attempts = int(_os.environ.get("CODEX_ADDPHONE_ATTEMPTS", "2") or "2")
    if sms_timeout is None:
        sms_timeout = int(_os.environ.get("CODEX_SMS_TIMEOUT", "150") or "150")
    from common import sms
    from config import (SMS_PROJECT_ID_OPENAI, HERO_SMS_SERVICE_OPENAI,
                        SMS_MAXPRICE_OPENAI, SMS_COUNTRY_BLACKLIST_OPENAI,
                        SMSMAN_APP_ID_OPENAI, SMSMAN_COUNTRY_ID_OPENAI, SMSMAN_MAXPRICE_OPENAI)
    print(f"  [add-phone] 接码模式：最多换号 {attempts} 次，单号等码 {sms_timeout}s")
    for i in range(attempts):
        pkey = None
        try:
            # 换号前必须把页面退回"输手机号"页(否则 #tel 找不到)
            need_reset = i > 0
            if not need_reset:
                try:
                    await page.locator("#tel").wait_for(state="visible", timeout=5000)
                except Exception:
                    need_reset = True
            if need_reset:
                if not auth_url:
                    print("  [add-phone] 缺 auth_url 无法回退，终止")
                    break
                print("  [add-phone] 回退到输手机号页...")
                if not await _goto_add_phone(page, auth_url, account_email):
                    if not _is_phone_flow_url(page.url):
                        if _is_authorization_advanced_url(page.url):
                            print("  [add-phone] 已离开 add-phone，继续处理授权回调")
                            return True
                        print(f"  [add-phone] 换号未回到手机号页: {page.url[:80]}")
                        continue
                    print("  [add-phone] 回退后仍找不到 #tel，跳过本次")
                    continue

            # 任意国家(库存动态，指定具体国家常无货) + 拉黑垃圾号段 + 给够价格上限。
            # max_retries 经 SMS_GETPHONE_RETRIES 可调：OpenAI WhatsApp 项目(1096/1008)库存
            # 常成分钟级干涸，默认 4 次(~32s)轮询太短，调大让本步骤耐心等补货。
            import os as _os
            _retries = int(_os.environ.get("SMS_GETPHONE_RETRIES", "4") or "4")
            phone, cc, pkey = sms.get_phone(SMS_PROJECT_ID_OPENAI, HERO_SMS_SERVICE_OPENAI,
                                            country_prefer=[""], country_blacklist=SMS_COUNTRY_BLACKLIST_OPENAI,
                                            max_retries=_retries, max_price=SMS_MAXPRICE_OPENAI,
                                            smsman_app=SMSMAN_APP_ID_OPENAI,
                                            smsman_country=SMSMAN_COUNTRY_ID_OPENAI,
                                            smsman_maxprice=SMSMAN_MAXPRICE_OPENAI,
                                            provider=sms_provider)
            print(f"  [add-phone] 尝试 {i+1}/{attempts}: +{cc}{phone}")
            await _fill_phone_continue(page, cc, phone)
            await asyncio.sleep(4)
            if _is_phone_flow_url(page.url) and await _has_phone_error(page):
                print("  [add-phone] 号码被拒，换号重试")
                sms.release(pkey)
                continue
            code = sms.get_code(pkey, max_wait=sms_timeout)
            if not code:
                print("  [add-phone] 未收到验证码，换号重试")
                sms.release(pkey)
                continue
            if not await _enter_otp(page, code):
                print("  [add-phone] 验证码未写入输入框，换号重试")
                sms.release(pkey)
                continue
            if await _wait_for_phone_flow_exit(page):
                print("  [add-phone] 手机验证通过")
                return True
            print(f"  [add-phone] 验证码提交后仍在手机验证页: {page.url[:80]}")
            sms.release(pkey)
        except Exception as e:
            print(f"  [add-phone] err: {str(e)[:80]}")
            if pkey:
                try:
                    sms.release(pkey)
                except Exception:
                    pass
    return False


async def _click_account(page, account_email=""):
    """choose-an-account 账号选择页:优先点中目标邮箱的账号，否则点第一个账号按钮。"""
    # 1) 含邮箱文本的按钮
    if account_email:
        try:
            loc = page.locator("button", has_text=account_email)
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click(timeout=2500)
                return True
        except Exception:
            pass
    # 2) 含 "Select account" 无障碍文案的账号按钮(取第一个)
    for sel in ['button:has-text("Select account")', 'button:has(span:has-text("@"))']:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click(timeout=2500)
                return True
        except Exception:
            pass
    return False


async def drive_authorize(page, auth_url, timeout=120, debug_dump=None, account_email="", manual_phone=False, semi_phone="", allow_phone=True, sms_provider="auto"):
    """在已登录该账号的页面打开 auth_url，处理账号选择/同意页，捕获 localhost:1455 回调。
    manual_phone=True 时遇到 add-phone 不自动接码，由用户在浏览器手动填号收码，脚本轮询等待。
    semi_phone 非空时(半自动)：脚本自动填该号+选 WhatsApp+发送一次，然后转手动等用户输码。
    allow_phone=False 时遇到 add-phone **立即返回**(不接码不花钱)，msg="ADDPHONE_REQUIRED"，
    供上层"先试 N 次免手机直连、实在每次都弹才在最后一次接码"的策略复用。
    返回 (code, state, msg)。失败 code/state 为 None。"""
    captured = {}
    manual_hint_shown = False
    semi_sent = False
    auth_login_attempted = False

    async def _handle(route):
        captured["url"] = route.request.url
        try:
            await route.fulfill(status=200, content_type="text/html", body="<html>captured</html>")
        except Exception:
            try:
                await route.abort()
            except Exception:
                pass

    for pat in ("http://localhost:1455/**", "http://127.0.0.1:1455/**"):
        await page.context.route(pat, _handle)

    try:
        try:
            await page.goto(auth_url, timeout=45000, wait_until="domcontentloaded")
        except Exception:
            pass  # 可能被重定向到 localhost 打断，正常

        deadline = time.time() + timeout
        consent_url_seen = [0]  # 连续在 consent 页未推进的轮数，多了就 re-goto 破 churn
        stuck_rounds = 0        # 连续"没点到任何按钮"的轮数(任意页面)，用于心跳日志 + 兜底破 churn
        last_hb_url = ""        # 上次心跳打印的 URL，变了就立刻再打一条
        round_i = 0
        while time.time() < deadline:
            round_i += 1
            if captured.get("url"):
                break
            # BiDi keeps the failed localhost callback in the address bar even
            # when no Playwright route handler is available.
            try:
                current_url = page.url
                parsed_current = urlparse(current_url)
                if (
                    parsed_current.hostname in {"localhost", "127.0.0.1"}
                    and parsed_current.port == 1455
                ):
                    captured["url"] = current_url
                    break
            except Exception:
                pass
            # A valid chatgpt.com session does not always carry into the newer
            # auth.openai.com OAuth session. Complete the email login in-place;
            # iCloud accounts can fetch the fresh verification code directly.
            try:
                on_auth_login = (
                    "auth.openai.com" in page.url
                    and any(part in page.url for part in ("/log-in", "/email-verification"))
                )
                if on_auth_login and not auth_login_attempted:
                    auth_login_attempted = True
                    ok, login_msg = await _complete_auth_email_login(
                        page, account_email
                    )
                    if not ok:
                        return None, None, login_msg
                    await asyncio.sleep(1.5)
                    continue
            except Exception as error:
                return None, None, f"OAuth 邮箱登录异常: {str(error)[:100]}"
            # 账号选择页:先选账号
            try:
                if "choose-an-account" in page.url or "/account" in page.url:
                    if await _click_account(page, account_email):
                        await asyncio.sleep(2.0)
                        continue
            except Exception:
                pass
            # add-phone 页:manual_phone=True 时不接码，由用户在浏览器手动填号+输码(如 WhatsApp 码)，
            # 脚本只轮询等待离开 add-phone 页；否则走接码自动过。
            try:
                if _is_phone_flow_url(page.url):
                    if not allow_phone:
                        # 本次只赌"免手机直连"，弹了手机就立刻退出(不接码不花钱)，交给上层换会话重试
                        print("  [add-phone] 本次免手机策略：检测到要手机验证，跳过本次(不接码)")
                        return None, None, "ADDPHONE_REQUIRED"
                    if semi_phone:
                        # 半自动：第一次到 add-phone 页就填号+选 WhatsApp+发送，之后转轮询等用户输码。
                        if not semi_sent:
                            print(f"  [add-phone] 半自动:填号 {semi_phone} + 选 WhatsApp + 发送...")
                            try:
                                ok = await _semi_fill_phone(page, semi_phone)
                            except Exception as e:
                                ok = False
                                print(f"  [add-phone] 半自动填号异常: {str(e)[:80]}")
                            semi_sent = True
                            if ok:
                                print("  [add-phone] 已发送。请在浏览器里输入收到的 WhatsApp 验证码；脚本轮询等待离开本页。")
                            else:
                                print("  [add-phone] 自动填号/选WhatsApp未完全成功，请在浏览器里手动完成（填号/选WhatsApp/输码）。")
                        await asyncio.sleep(2.0)
                        continue
                    if manual_phone:
                        if not manual_hint_shown:
                            print("  [add-phone] 手动模式:请在浏览器里自行填写手机号并输入收到的验证码(如 WhatsApp 码)。")
                            print(f"             脚本会轮询等待,直到离开 add-phone 页(上限 {timeout}s)。")
                            manual_hint_shown = True
                        await asyncio.sleep(2.0)
                        continue
                    ok = await handle_add_phone(
                        page,
                        auth_url=auth_url,
                        account_email=account_email,
                        sms_provider=sms_provider,
                    )
                    if not ok:
                        return None, None, "add-phone 手机验证失败(接码换号都没过)"
                    # add-phone 自动接码可能耗时数分钟，把原 deadline 吃光。过了之后给
                    # 后续「同意页→捕获 localhost:1455 回调」一段独立的新预算，否则刚过手机
                    # 验证就因 deadline 已到而退出、卡在 /codex/consent 拿不到回调。
                    deadline = max(deadline, time.time() + 90)
                    print(f"  [add-phone] 通过，续期授权捕获窗口至 +{int(deadline - time.time())}s")
                    await asyncio.sleep(2.0)
                    continue
            except Exception as e:
                return None, None, f"add-phone 处理异常: {str(e)[:80]}"
            # 同意页(/codex/consent)：就一个 续行/Continue 提交按钮。优先用精确 selector
            # (role+name 在 churn 态常 DOMException)，点不到累计；连续多轮没推进就 re-goto 破 churn。
            clicked = False
            for sel in ('button[data-dd-action-name="Continue"]', 'button[type="submit"]'):
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        await loc.first.click(timeout=2500)
                        await asyncio.sleep(1.5)
                        clicked = True
                        break
                except Exception:
                    pass
            if not clicked:
                for lbl in CONSENT_LABELS:
                    try:
                        loc = page.get_by_role("button", name=lbl, exact=False)
                        if await loc.count() > 0 and await loc.first.is_visible():
                            await loc.first.click(timeout=2500)
                            await asyncio.sleep(1.5)
                            clicked = True
                            break
                    except Exception:
                        pass
            if not clicked:
                for lbl in CONSENT_LABELS:
                    try:
                        loc = page.get_by_role("link", name=lbl, exact=False)
                        if await loc.count() > 0 and await loc.first.is_visible():
                            await loc.first.click(timeout=2500)
                            await asyncio.sleep(1.5)
                            clicked = True
                            break
                    except Exception:
                        pass
            # churn 破解：在 consent 页连续 4 轮没点动按钮，重新 goto auth_url 拿干净页面
            try:
                cur_url = page.url
            except Exception:
                cur_url = ""
            try:
                on_consent = "consent" in cur_url or "sign-in-with-chatgpt" in cur_url
            except Exception:
                on_consent = True
            if on_consent and not clicked:
                consent_url_seen[0] += 1
                if consent_url_seen[0] % 4 == 0:
                    print(f"  [consent] 连续 {consent_url_seen[0]} 轮未推进，re-goto auth_url 破 churn...")
                    try:
                        await page.goto(auth_url, timeout=30000, wait_until="domcontentloaded")
                    except Exception:
                        pass
            else:
                consent_url_seen[0] = 0
            # 心跳 + 兜底破 churn：循环平时静默，卡在"识别不了的页面/按钮"时既不点也不 re-goto，
            # 会一路空转到硬超时还没任何输出。这里把卡死状态打出来(URL 变化或每~12s 一条)，
            # 并对任意页面连续 8 轮没推进也 re-goto，救回卡在非 consent 未知页的情况。
            if clicked:
                stuck_rounds = 0
            else:
                stuck_rounds += 1
                left = int(deadline - time.time())
                if cur_url != last_hb_url or stuck_rounds % 8 == 0:
                    print(f"  [authz] 等待中(轮{round_i}/卡{stuck_rounds}, 剩{left}s): {cur_url[:90]}")
                    last_hb_url = cur_url
                # 卡住第 4 轮:dump 当前页可见按钮 + 截图,看清到底是什么页面/按钮没点到
                # (chatgpt.com 促销弹窗遮挡 consent、未知语种按钮等都能在此现形)。
                if stuck_rounds == 4:
                    try:
                        btns = []
                        nb = await page.locator("button").count()
                        for bi in range(min(nb, 12)):
                            try:
                                t = (await page.locator("button").nth(bi).inner_text()).strip()[:30]
                                if t:
                                    btns.append(t)
                            except Exception:
                                pass
                        print(f"  [authz] 卡住页按钮: {btns}")
                        import os as _os2
                        _os2.makedirs("screenshots", exist_ok=True)
                        shot = f"screenshots/codex_authz_stuck_{int(time.time())}.png"
                        await page.screenshot(path=shot)
                        print(f"  [authz] 截图: {shot}")
                    except Exception as e:
                        print(f"  [authz] dump 失败: {str(e)[:60]}")
                if stuck_rounds and stuck_rounds % 8 == 0 and not on_consent:
                    print(f"  [authz] 连续 {stuck_rounds} 轮停在非 consent 页未推进，re-goto auth_url 破 churn...")
                    try:
                        await page.goto(auth_url, timeout=30000, wait_until="domcontentloaded")
                    except Exception:
                        pass
            await asyncio.sleep(1.0)

        url = captured.get("url")
        if not url:
            if debug_dump:
                try:
                    html = await page.content()
                    with open(debug_dump, "w", encoding="utf-8") as f:
                        f.write(f"<!-- url: {page.url} -->\n" + html)
                except Exception:
                    pass
            return None, None, f"未捕获 localhost:1455 回调(当前页 {page.url[:80]})"

        q = parse_qs(urlparse(url).query)
        code = q.get("code", [None])[0]
        state = q.get("state", [None])[0]
        err = q.get("error", [None])[0]
        if err:
            return None, None, f"授权返回 error={err}"
        if not code:
            return None, None, "回调缺少 code"
        return code, state, "ok"
    finally:
        for pat in ("http://localhost:1455/**", "http://127.0.0.1:1455/**"):
            try:
                await page.context.unroute(pat, _handle)
            except Exception:
                pass


def _sanitize_cookies(cookies):
    """清洗 cookie 给 add_cookies 用(只留必要字段，sameSite 规范化)。"""
    out = []
    for c in cookies:
        nc = {k: c[k] for k in ("name", "value", "domain", "path", "httpOnly", "secure") if k in c}
        if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0:
            nc["expires"] = c["expires"]
        ss = c.get("sameSite")
        nc["sameSite"] = ss if ss in ("Strict", "Lax", "None") else "Lax"
        out.append(nc)
    return out


def make_reset_page(
    p,
    cookies,
    account_email="",
    name_prefix="codex_retry",
    before_open=None,
    browser_options=None,
):
    """造一个 reset_page(old_page)->new_page 回调：关旧窗口→开新窗口→灌 cookie→开 chatgpt.com
    确认登录态。给 authorize_with_retry 用，使每次尝试都是 OpenAI 眼里的全新会话。
    p: playwright 实例；cookies: 注册窗口导出的 cookie 列表(list[dict])。
    第一次调用 old_page 是注册窗口——也关掉它(由调用方在 finally 兜底删 profile)。"""
    from common.browser import open_and_connect, teardown
    state = {"bb": None, "pid": None, "before_open_done": False}
    clean = _sanitize_cookies(cookies)

    async def reset_page(old_page):
        # 关掉上一个 retry 窗口(注册原窗口由调用方 finally 删，这里只删自己开的)
        if state["bb"] and state["pid"]:
            try:
                await teardown(state["bb"], state["pid"], delete=True)
            except Exception:
                pass
            state["bb"] = state["pid"] = None
        if before_open is not None and not state["before_open_done"]:
            await before_open()
            state["before_open_done"] = True
        bb, pid, browser, ctx, page = await open_and_connect(
            name=f"{name_prefix}_{time.strftime('%H%M%S')}",
            p=p,
            browser_options=browser_options,
        )
        state["bb"], state["pid"] = bb, pid
        await ctx.clear_cookies()
        await ctx.add_cookies(clean)
        await page.goto("https://chatgpt.com/", timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        # 确认登录态(撞导航重试几次)
        for _ in range(5):
            try:
                sess = await page.evaluate(
                    "() => fetch('/api/auth/session',{credentials:'include'}).then(r=>r.ok?r.json():null).catch(()=>null)")
                if sess and sess.get("accessToken"):
                    break
            except Exception:
                pass
            await asyncio.sleep(2)
        return page

    async def cleanup():
        """收尾删最后一个 retry 窗口(调用方 finally await 调)。"""
        if state["bb"] and state["pid"]:
            try:
                await teardown(state["bb"], state["pid"], delete=True)
            except Exception:
                pass
            state["bb"] = state["pid"] = None

    reset_page.cleanup = cleanup
    reset_page.state = state
    return reset_page


async def _icloud_existing_codes(email):
    """Read current codes so the OAuth login cannot reuse the signup code."""
    if not email or email.rsplit("@", 1)[-1].lower() not in {
        "icloud.com", "me.com", "mac.com"
    }:
        return set()
    from common.temp_email import fetch_messages

    loop = asyncio.get_running_loop()
    messages = await loop.run_in_executor(
        None,
        lambda: fetch_messages(email, "icloud", email=email),
    )
    codes = set()
    for message in messages or []:
        for code in (message.get("extracted") or {}).get("codes") or []:
            if str(code).isdigit() and len(str(code)) == 6:
                codes.add(str(code))
        text = " ".join(
            str(message.get(key) or "")
            for key in ("subject", "text", "textBody", "content", "html", "htmlBody")
        )
        codes.update(re.findall(r"\b\d{6}\b", text))
    return codes


async def _complete_auth_email_login(page, account_email):
    """Finish the Codex OAuth email login when the ChatGPT cookie is insufficient."""
    if not account_email:
        return False, "OAuth 要求重新登录，但无法确定账号邮箱"

    from common.browser import react_fill

    known_codes = await _icloud_existing_codes(account_email)
    email_input = page.locator('input[type="email"], input[name="email"]').first
    if await email_input.count() > 0:
        if not await react_fill(
            page,
            'input[type="email"], input[name="email"]',
            account_email,
            tries=2,
            verbose=False,
        ):
            return False, "OAuth 登录页邮箱输入未被页面接受"
        submit = page.locator('button[type="submit"]').first
        if await submit.count() == 0:
            return False, "OAuth 登录页没有 Continue 按钮"
        await submit.click()

    code_input = page.locator(
        'input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
    ).first
    for _ in range(30):
        if await code_input.count() > 0:
            break
        if "auth.openai.com" not in page.url or not any(
            part in page.url for part in ("/log-in", "/email-verification")
        ):
            return True, "ok"
        await asyncio.sleep(0.5)
    if await code_input.count() == 0:
        return False, f"OAuth 邮箱登录未进入验证码页(当前页 {page.url[:80]})"

    domain = account_email.rsplit("@", 1)[-1].lower()
    if domain not in {"icloud.com", "me.com", "mac.com"}:
        return False, "OAuth 要求邮箱验证码；当前仅 iCloud 可在此步骤自动取码"

    from common.temp_email import poll_verification_code

    code = await poll_verification_code(
        account_email,
        "icloud",
        email=account_email,
        max_wait=120,
        poll_interval=4,
        code_regex=r"\b(\d{6})\b",
        exclude_codes=tuple(known_codes),
    )
    if not code:
        return False, "OAuth iCloud 验证码超时"
    if not await react_fill(
        page,
        'input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]',
        str(code),
        tries=2,
        verbose=False,
    ):
        return False, "OAuth 验证码输入未被页面接受"
    submit = page.locator('button[type="submit"]').first
    if await submit.count() == 0:
        return False, "OAuth 验证码页没有提交按钮"
    await submit.click()

    for _ in range(40):
        if "email-verification" not in page.url and "/log-in" not in page.url:
            return True, "ok"
        try:
            body = (await page.locator("body").inner_text()).lower()
            if "route error" in body and "text/html" in body:
                retry = page.get_by_role("button", name="Retry", exact=False)
                if await retry.count() > 0:
                    await retry.first.click()
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False, f"OAuth 邮箱验证码未完成(当前页 {page.url[:80]})"


async def authorize_with_retry(page, gen_auth_url, account_email="", phone_skip_attempts=3,
                               skip_timeout=120, phone_timeout=600, debug_dump=None,
                               manual_phone=False, semi_phone="", reset_page=None,
                               sms_provider="auto"):
    """Codex 授权重试编排：**先赌 N 次"免手机直连"，每次失败重新生成授权链接(新会话=重新摇风控骰子)，
    实在每次都要手机，最后一次才真接码/手动填号。**

    gen_auth_url: 无参可调用，每次返回 (auth_url, session_id, state)，内部重新 POST generate-auth-url。
    phone_skip_attempts: 免手机尝试次数(默认 3)。这 N 次遇 add-phone 立即跳过(不接码不花钱)。
    第 N+1 次(最后一次)放开手机：manual_phone/semi_phone 决定接码方式，默认自动接码。
    reset_page: 可选 async 可调用 reset_page(old_page)->new_page。**每次尝试前关旧窗口、开新窗口、
      重登(cookie)**，让每次都是 OpenAI 眼里全新会话——这才能真正重摇"要不要手机"的风控
      (复用同窗口重发 auth_url 不改变其决定)。返回 None 视为重置失败，沿用旧 page。
    返回 (code, session_id, state, msg)；失败 code 为 None。code 与返回的 session_id/state 必配套。"""
    last_msg = ""
    # phone_skip_attempts 次免手机直连 + 1 次接码兜底；skip<=0 时直接一次性接码(不赌免手机)
    total = phone_skip_attempts + 1 if phone_skip_attempts > 0 else 1
    for attempt in range(total):
        is_phone_attempt = attempt >= phone_skip_attempts
        # 每次尝试前关窗口重开+重登，确保是全新会话(否则同窗口重试不改变风控决定)
        if reset_page is not None:
            try:
                new_page = await reset_page(page)
                if new_page is not None:
                    page = new_page
                else:
                    print(f"  [codex] 第 {attempt+1} 次窗口重置失败，沿用旧窗口")
            except Exception as e:
                print(f"  [codex] 窗口重置异常(忽略): {str(e)[:80]}")
        # 生成授权链接(SUB2API generate-auth-url)。tiantianai.co 经代理偶发抖动，
        # 单次失败不该搞死整轮——重试几次(退避)，全失败才放弃本次尝试。
        auth_url = session_id = state = None
        for _g in range(4):
            try:
                auth_url, session_id, state = gen_auth_url()
                break
            except Exception as e:
                last_msg = f"生成授权链接失败: {str(e)[:80]}"
                print(f"  [codex] gen_auth_url 失败({_g+1}/4): {str(e)[:70]}")
                await asyncio.sleep(3 + _g * 2)
        if not auth_url:
            # 本次尝试拿不到链接：不是最后一次就继续下一次(可能节点恢复)，最后一次才真退
            if attempt < total - 1:
                print("  [codex] 本次生成链接失败，下一次重试...")
                continue
            return None, None, None, last_msg or "生成授权链接失败"
        if is_phone_attempt:
            mode = "手动填号" if manual_phone else ("半自动" if semi_phone else "自动接码")
            print(f"  [codex] 授权尝试 {attempt+1}/{total}（最后一次，放开手机验证：{mode}，上限 {phone_timeout}s）...")
            _budget = phone_timeout
            _coro = drive_authorize(
                page, auth_url, timeout=phone_timeout, debug_dump=debug_dump,
                account_email=account_email, manual_phone=manual_phone, semi_phone=semi_phone,
                allow_phone=True, sms_provider=sms_provider)
        else:
            print(f"  [codex] 授权尝试 {attempt+1}/{total}（免手机直连，弹手机就换会话重试，上限 {skip_timeout}s）...")
            _budget = skip_timeout
            _coro = drive_authorize(
                page, auth_url, timeout=skip_timeout, debug_dump=None,
                account_email=account_email, allow_phone=False,
                sms_provider=sms_provider)
        # 硬上限：drive_authorize 内部循环若被卡死的 await 阻塞会无视自身 deadline，
        # 这里用 wait_for 兜底(+60s 缓冲)强制超时，避免整轮永久冻结。
        try:
            code, cb_state, msg = await asyncio.wait_for(_coro, timeout=_budget + 60)
        except asyncio.TimeoutError:
            code, cb_state, msg = None, None, f"drive_authorize 硬超时({_budget+60}s)"
            print(f"  [codex] 第 {attempt+1} 次硬超时，强制中断重试...")
        last_msg = msg
        if code:
            return code, session_id, cb_state or state, "ok"
        if msg == "ADDPHONE_REQUIRED":
            print(f"  [codex] 第 {attempt+1} 次要手机验证，换新会话重试...")
            await asyncio.sleep(1.5)
            continue
        # 非"要手机"的其它失败(没捕获回调/error 等)：免手机阶段也继续重试，最后一次失败才退
        if is_phone_attempt:
            return None, None, None, msg
        print(f"  [codex] 第 {attempt+1} 次未成({str(msg)[:50]})，重试...")
        await asyncio.sleep(1.5)
    return None, None, None, last_msg or "授权重试用尽"
