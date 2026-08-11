# -*- coding: utf-8 -*-
"""本地 token 上传幂等标记。"""

import os

try:
    from config import TOKEN_OUTPUT_DIR
except Exception:
    TOKEN_OUTPUT_DIR = "tokens"


def uploaded_set(platform, target):
    path = os.path.join(TOKEN_OUTPUT_DIR, platform, f"uploaded_{target}.txt")
    if not os.path.isfile(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def mark_uploaded(platform, target, key):
    key = str(key or "").strip()
    if not key:
        return
    pdir = os.path.join(TOKEN_OUTPUT_DIR, platform)
    os.makedirs(pdir, exist_ok=True)
    path = os.path.join(pdir, f"uploaded_{target}.txt")
    existing = uploaded_set(platform, target)
    if key in existing:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{key}\n")
