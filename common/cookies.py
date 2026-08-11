# -*- coding: utf-8 -*-
"""
common/cookies.py — 通用 cookie 保存（按平台分目录）

保存到:
    cookies/<platform>/full_<profileid>_<ts>.json   完整 Playwright cookie 数组
    cookies/<platform>/accounts.txt                 email|password|key 追加
"""

import json
import os
import sys
from datetime import datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

BASE_COOKIE_DIR = "cookies"


async def save_platform_cookies(context, platform, profile_id, email=None, password=None, key_cookie_names=()):
    """保存某平台账号的完整 cookie。
    key_cookie_names: 用于判定"已登录"的关键 cookie 名列表（任一存在即算成功）。
    返回 (key_value or None, full_json_path or None)。"""
    cookies = await context.cookies()
    pdir = os.path.join(BASE_COOKIE_DIR, platform)
    os.makedirs(pdir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    key_val = None
    found_names = []
    for c in cookies:
        if c.get("name") in key_cookie_names:
            found_names.append(c["name"])
            if key_val is None:
                key_val = c["value"]

    print(f"  [{platform}] {len(cookies)} cookies, key cookies present: {found_names}")

    full_path = os.path.join(pdir, f"full_{profile_id}_{ts}.json")
    with open(full_path, "w", encoding="utf-8") as f:
        json.dump(cookies, f, indent=2, ensure_ascii=False)
    print(f"  [{platform}] full cookies saved: {full_path}")

    if key_val and email:
        acc_path = os.path.join(pdir, "accounts.txt")
        with open(acc_path, "a", encoding="utf-8") as f:
            f.write(f"{email}|{password or ''}|{key_val}\n")
        print(f"  [{platform}] account saved: {acc_path}")

    return key_val, full_path
