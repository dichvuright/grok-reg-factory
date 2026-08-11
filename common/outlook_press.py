# -*- coding: utf-8 -*-
"""Shared Outlook PerimeterX press-and-hold behavior.

Registration and recovery must use this module together. Keeping target
selection and the physical press sequence in one place prevents the two flows
from slowly diverging as Microsoft's challenge markup changes.
"""

from __future__ import annotations

import asyncio
import random

from common import human_mouse


async def captcha_visible(page):
    """Return whether an interactive Outlook hold challenge is still visible."""
    try:
        for selector in (
            'button:has-text("Press and hold")',
            'button:has-text("Appuyer et maintenir")',
            'button:has-text("按住")',
            'button:has-text("长按")',
            'button:has-text("Halten")',
            '#px-captcha',
        ):
            element = page.locator(selector).first
            if await element.count() > 0:
                box = await element.bounding_box()
                if box and box["width"] > 30:
                    return True

        frames = page.locator(
            'iframe[src*="hsprotect.net"], '
            'iframe[src*="arkose"], '
            'iframe[src*="funcaptcha"]'
        )
        for index in range(await frames.count()):
            box = await frames.nth(index).bounding_box()
            if box and box["width"] > 50 and box["height"] > 30:
                return True
    except Exception:
        pass
    return False


async def find_hold_target(page):
    """Use the target lookup proven by the Outlook registration flow."""
    for frame in page.frames:
        if frame == page.main_frame or "hsprotect.net" not in (frame.url or ""):
            continue
        try:
            button = frame.locator("#px-captcha").first
            if await button.count() > 0:
                box = await button.bounding_box()
                if box and box["width"] > 30 and box["height"] > 8:
                    return box, True
        except Exception:
            pass

    try:
        frames = page.locator('iframe[src*="hsprotect.net"]')
        for index in range(await frames.count()):
            box = await frames.nth(index).bounding_box()
            if box and box["width"] > 50 and box["height"] > 30:
                return box, False
    except Exception:
        pass
    return None, False


async def press_and_hold(page, *, label="", press_number=1):
    """Run one registration-style hold attempt, or return None without a target."""
    target_box, box_is_button = await find_hold_target(page)
    if not target_box:
        return None

    bx = target_box["x"]
    by = target_box["y"]
    bw = target_box["width"]
    bh = target_box["height"]
    if box_is_button:
        cx = bx + bw * random.uniform(0.40, 0.60)
        cy = by + bh * random.uniform(0.40, 0.60)
    else:
        cx = bx + bw * random.uniform(0.42, 0.58)
        cy = by + bh * random.uniform(0.48, 0.62)

    suffix = " [btn]" if box_is_button else " [box]"
    print(f"{label} press #{press_number}: ({cx:.0f},{cy:.0f}){suffix}")

    async def hold_done():
        return not await captcha_visible(page)

    try:
        held, passed = await human_mouse.human_press_and_hold(
            page,
            cx,
            cy,
            is_done=hold_done,
            max_hold=random.uniform(11.0, 15.0),
            min_hold=1.5,
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        print(f"{label} human_press_and_hold err: {message}")
        if "closed" in message.lower() or "targetclosed" in message.lower():
            print(f"{label} page/context 已关闭，跳过重按，交外层判定")
            held, passed = 0.0, False
        else:
            try:
                await page.mouse.down()
                await asyncio.sleep(random.uniform(11.0, 14.0))
                await page.mouse.up()
            except Exception:
                pass
            held, passed = 12.0, False

    print(f"{label} held {held:.1f}s{' (passed)' if passed else ''}")
    return {
        "held": held,
        "passed": passed,
        "box_is_button": box_is_button,
        "x": cx,
        "y": cy,
    }
