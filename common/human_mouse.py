# -*- coding: utf-8 -*-
"""Human-like mouse movement + press-and-hold for CDP/Playwright pages.

Ported in spirit from LoseNine/ruyipage's human_move (WindMouse) — the one
piece of that project that IS portable to our BitBrowser + Playwright (CDP)
stack. ruyipage's headline "no automation detection points" comes from a
custom Firefox kernel we can't reuse; but its *movement* algorithms (WindMouse
trajectory + human tremor) are exactly what our Outlook PerimeterX press-and-hold
was missing.

Why the old code failed PerimeterX behavioral analysis:
  * standalone hold drift was a pure sine wave  -> perfectly periodic, obvious bot
  * register.py hold jitter was uniform +-2px    -> white noise, no momentum
Neither looks like a real hand. Real tremor is *autocorrelated* (momentum +
mean-reversion) — modelled here with an Ornstein-Uhlenbeck process. The
approach path uses WindMouse (gravity toward target + random wind, speed-clamped,
with natural overshoot and variable velocity).

Pure stdlib (math/random/asyncio) — no new dependencies. The `page` argument is
any object exposing async `mouse.move(x, y)`, `mouse.down()`, `mouse.up()`
(Playwright async Page / CDP-backed page).

Tunables via env (all optional):
  HUMAN_MOUSE_TREMOR_PX   tremor amplitude clamp in px (default 1.6)
  HUMAN_MOUSE_DEBUG       "1" to print motion stats
"""
from __future__ import annotations

import asyncio
import math
import os
import random

# WindMouse constants (classic values; tuned for screen-scale motion).
_WM_GRAVITY = 9.0        # 向目标的恒定牵引
_WM_WIND = 3.0           # 随机风力上限（制造抖动/过冲）
_WM_MAX_STEP = 14.0      # 单步最大位移（速度钳制）
_WM_TARGET_AREA = 12.0   # 进入该半径后风力衰减、开始收敛落点


def _debug(msg):
    if (os.environ.get("HUMAN_MOUSE_DEBUG", "") or "").strip() in {"1", "true", "yes", "on"}:
        print(f"  [human_mouse] {msg}", flush=True)


def _tremor_px():
    try:
        return max(0.3, float(os.environ.get("HUMAN_MOUSE_TREMOR_PX", "1.6")))
    except Exception:
        return 1.6
def windmouse_path(x0, y0, x1, y1, gravity=_WM_GRAVITY, wind=_WM_WIND,
                   max_step=_WM_MAX_STEP, target_area=_WM_TARGET_AREA):
    """WindMouse: return a list of (x, y) points from (x0,y0) to (x1,y1).

    Motion = constant gravity toward the target + a random-walk "wind" force,
    with per-step displacement clamped to max_step (this is what produces the
    characteristic variable velocity: fast in the middle, slow near the ends,
    with slight overshoot). Near the target the wind decays so the path
    converges instead of orbiting. Points are integer-free (float) so the
    caller can add sub-pixel jitter; the final point is exactly (x1, y1).
    """
    points = []
    cx, cy = float(x0), float(y0)
    vx, vy = 0.0, 0.0  # velocity
    wx, wy = 0.0, 0.0  # wind
    sqrt3 = math.sqrt(3.0)
    sqrt5 = math.sqrt(5.0)

    dist = math.hypot(x1 - cx, y1 - cy)
    # 空移动直接返回终点
    if dist < 1.0:
        return [(float(x1), float(y1))]

    guard = 0
    while dist >= 1.0 and guard < 10000:
        guard += 1
        w = min(wind, dist)
        if dist >= target_area:
            # 远离目标：风力随机游走（有动量）
            wx = wx / sqrt3 + (2.0 * random.random() - 1.0) * w / sqrt5
            wy = wy / sqrt3 + (2.0 * random.random() - 1.0) * w / sqrt5
        else:
            # 逼近目标：风力衰减，步长收缩，防止在落点附近打转
            wx /= sqrt3
            wy /= sqrt3
            if max_step < 3:
                max_step = random.random() * 3.0 + 3.0
            else:
                max_step /= sqrt5

        # 速度 = 风力 + 指向目标的重力
        vx += wx + gravity * (x1 - cx) / dist
        vy += wy + gravity * (y1 - cy) / dist

        # 速度钳制（把过快的一步缩回 max_step，方向保留）
        v_mag = math.hypot(vx, vy)
        if v_mag > max_step:
            v_clip = max_step / 2.0 + random.random() * max_step / 2.0
            vx = (vx / v_mag) * v_clip
            vy = (vy / v_mag) * v_clip

        cx += vx
        cy += vy
        dist = math.hypot(x1 - cx, y1 - cy)
        points.append((cx, cy))

    # 确保精确命中终点
    points.append((float(x1), float(y1)))
    return points


async def human_move_to(page, x, y, start=None):
    """沿 WindMouse 轨迹把鼠标移到 (x, y)，每步 sleep 变速（两端慢、中段快）。"""
    if start is None:
        # 未知当前位置：从一个偏离目标的随机点起步（真人不会瞬移到按钮上）
        sx = x + random.uniform(-260, 260)
        sy = y + random.uniform(-180, 180)
        await page.mouse.move(sx, sy)
        await asyncio.sleep(random.uniform(0.04, 0.12))
    else:
        sx, sy = start
    path = windmouse_path(sx, sy, x, y)
    n = len(path)
    for i, (px, py) in enumerate(path):
        # 亚像素抖动，避免整数网格化的机器人痕迹
        await page.mouse.move(px + random.uniform(-0.6, 0.6),
                              py + random.uniform(-0.6, 0.6))
        # 加减速：路径首尾放慢，中段加快（用位置比例做钟形延时）
        frac = i / max(1, n - 1)
        bell = math.sin(math.pi * frac)          # 0->1->0
        delay = random.uniform(0.004, 0.010) + (1.0 - bell) * random.uniform(0.004, 0.018)
        await asyncio.sleep(delay)
    _debug(f"move_to ({x:.0f},{y:.0f}) via {n} pts")
    return path
def tremor_offsets(n, dt=0.05, theta=6.0, sigma=None, clamp=None, seed=None):
    """生成 n 个自相关的按住微抖动 (dx, dy)，用 Ornstein-Uhlenbeck 过程。

    OU: dv = -theta*v*dt + sigma*sqrt(dt)*N(0,1)，位置对速度积分。这产生的是
    带【动量 + 回中】的抖动——像真人按住时手的生理性震颤，而不是正弦(周期性)
    或均匀随机(白噪声，无动量)。theta 越大回中越快(抖得越"紧")，sigma 控幅度。
    结果 clamp 在 +-clamp px 内（默认取 HUMAN_MOUSE_TREMOR_PX）。
    """
    rng = random.Random(seed) if seed is not None else random
    if clamp is None:
        clamp = _tremor_px()
    if sigma is None:
        sigma = clamp * 9.0        # 经验：使稳态幅度落在 ~clamp 内
    out = []
    x = y = 0.0
    vx = vy = 0.0
    sdt = math.sqrt(dt)
    for _ in range(max(1, n)):
        vx += -theta * vx * dt + sigma * sdt * rng.gauss(0.0, 1.0)
        vy += -theta * vy * dt + sigma * sdt * rng.gauss(0.0, 1.0)
        x += vx * dt
        y += vy * dt
        # 软钳制：超出范围就回拉（保留连续性，不硬截断成方波）
        if x > clamp or x < -clamp:
            x = max(-clamp, min(clamp, x)); vx *= -0.4
        if y > clamp or y < -clamp:
            y = max(-clamp, min(clamp, y)); vy *= -0.4
        out.append((x, y))
    return out


async def human_press_and_hold(page, cx, cy, is_done=None, max_hold=14.0,
                               min_hold=1.5, check_interval=0.5, start=None):
    """拟人「按住验证」完整序列，返回 (held_seconds, passed_bool)。

    流程：WindMouse 逼近 (cx,cy) -> 落点前微停 -> mouse.down -> 按住期间走
    OU 抖动(有动量的生理震颤) -> 每 check_interval 秒轮询 is_done() -> 进度满
    后加一个真人反应延迟再 mouse.up。max_hold 兜底防卡死；min_hold 防刚加载的
    空隙误判通过就松手。

    is_done: async callable -> bool，返回 True 表示验证已过(captcha 消失)。传
    你自己的 _captcha_visible 取反即可。为 None 时按满 max_hold 松手。
    """
    # 1) 逼近（WindMouse）
    await human_move_to(page, cx, cy, start=start)
    # 2) 落点前的自然停顿（真人手到按钮上会顿一下再按）
    await asyncio.sleep(random.uniform(0.12, 0.35))
    # 3) 按下
    await page.mouse.down()

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    # 预生成一段抖动序列（按 check 的内层 tick 走，走完循环续生成）
    tick = random.uniform(0.03, 0.07)
    passed = False
    last_check = 0.0
    tre = tremor_offsets(int(max_hold / tick) + 32, dt=tick)
    ti = 0

    while True:
        elapsed = loop.time() - t0
        if elapsed >= max_hold:
            break
        if ti >= len(tre):
            tre = tremor_offsets(64, dt=tick)
            ti = 0
        dx, dy = tre[ti]; ti += 1
        await page.mouse.move(cx + dx, cy + dy)

        # 进度满(captcha 消失)后加真人反应延迟再松手；至少按住 min_hold
        if is_done is not None and elapsed > min_hold and (elapsed - last_check) > check_interval:
            last_check = elapsed
            try:
                if await is_done():
                    passed = True
                    # 真人看到"通过"到松手有 ~120-360ms 反应时间
                    await asyncio.sleep(random.uniform(0.12, 0.36))
                    break
            except Exception:
                pass
        await asyncio.sleep(random.uniform(tick * 0.6, tick * 1.4))

    # 4) 松手前的极短停顿 + 松手
    await asyncio.sleep(random.uniform(0.03, 0.12))
    await page.mouse.up()
    held = loop.time() - t0
    _debug(f"hold {held:.1f}s passed={passed}")
    return held, passed


# ---------------------------------------------------------------------------
# Self-test: 无法打真 PerimeterX，这里验证运动的统计特征像人不像机器人。
# 运行:  python -m common.human_mouse   或   python common/human_mouse.py
# ---------------------------------------------------------------------------
def _selftest():
    import statistics

    # 1) WindMouse 路径：连续(无跳变) + 精确命中终点 + 速度非均匀
    path = windmouse_path(100, 120, 900, 640)
    assert len(path) >= 5, "path too short"
    assert abs(path[-1][0] - 900) < 0.001 and abs(path[-1][1] - 640) < 0.001, "endpoint miss"
    steps = [math.hypot(path[i+1][0]-path[i][0], path[i+1][1]-path[i][1])
             for i in range(len(path)-1)]
    max_jump = max(steps)
    assert max_jump <= _WM_MAX_STEP + 1e-6, f"jump {max_jump} exceeds max_step"
    cv = statistics.pstdev(steps) / (statistics.fmean(steps) or 1e-9)
    assert cv > 0.15, f"velocity too uniform (cv={cv:.3f}) — looks robotic"

    # 2) OU 抖动：自相关 > 阈值(证明有动量，非白噪声) + 幅度受钳
    tre = tremor_offsets(4000, dt=0.05, clamp=1.6, seed=42)
    xs = [p[0] for p in tre]
    assert max(abs(v) for v in xs) <= 1.6 + 1e-6, "tremor exceeds clamp"
    mean = statistics.fmean(xs)
    var = statistics.pvariance(xs) or 1e-9
    lag1 = sum((xs[i]-mean)*(xs[i+1]-mean) for i in range(len(xs)-1)) / (len(xs)-1) / var
    assert lag1 > 0.5, f"tremor autocorrelation {lag1:.3f} too low — looks like white noise"

    # 对照：白噪声的 lag-1 自相关应接近 0（证明我们的检验有意义）
    wn = [random.uniform(-1.6, 1.6) for _ in range(4000)]
    wmean = statistics.fmean(wn); wvar = statistics.pvariance(wn) or 1e-9
    wlag1 = sum((wn[i]-wmean)*(wn[i+1]-wmean) for i in range(len(wn)-1)) / (len(wn)-1) / wvar
    assert abs(wlag1) < 0.15, "sanity: white-noise autocorr should be ~0"

    print(f"[human_mouse selftest] OK  path_pts={len(path)} vel_cv={cv:.2f} "
          f"max_step={max_jump:.1f}  tremor_lag1={lag1:.2f} (white_noise_lag1={wlag1:.2f})")


if __name__ == "__main__":
    _selftest()

