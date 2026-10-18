"""
Animated finale for the Fireball satellite: ten ducks, all on fire, race across
the screen. One by one a flame goes out and that duck drops; the last three
still burning cross the finish line.

This is drawing only. Who loses, in what order, and who survives are decided
beforehand (and saved) by fireball.py; this module just turns that into a GIF.
Everything is drawn with Pillow shapes, so there are no image or font files to
ship.
"""
from __future__ import annotations

import io
import math
import random
import zlib

from PIL import Image, ImageDraw, ImageFont

W = 720
LANE_H = 40
TOP = 50
BOTTOM = 14
LABEL_W = 150
START_X = LABEL_W + 26
FINISH_X = W - 56
FRAME_MS = 80
RACE_FRAMES = 78            # the race itself (~6 s)
HOLD_MS = 4500              # last frame stays up this long

BG = (24, 24, 30)
LANE_A = (34, 34, 42)
LANE_B = (29, 29, 36)
WHITE = (240, 240, 245)
GREY = (120, 120, 130)
GOLD = (255, 205, 60)
RED = (255, 90, 60)
YELLOW = (255, 214, 60)
ORANGE = (255, 150, 30)
BEAK = (255, 140, 20)
SMOKE = (150, 150, 160)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                      # very old Pillow: fixed-size bitmap font
        return ImageFont.load_default()


def _flame(d: ImageDraw.ImageDraw, cx: float, cy: float, rng: random.Random, scale: float = 1.0):
    """A flickering cluster of flame tongues streaming back (left) and up from a duck."""
    layers = [((255, 60, 20), 1.0), ((255, 140, 20), 0.74), ((255, 225, 80), 0.46)]
    for color, k in layers:
        for tongue in range(4):
            ang = math.radians(rng.uniform(150, 215))          # backwards and up
            length = rng.uniform(15, 30) * k * scale
            width = rng.uniform(5, 8) * k * scale
            bx = cx + rng.uniform(-6, 6) * scale
            by = cy + rng.uniform(-5, 5) * scale
            tipx, tipy = bx + math.cos(ang) * length, by - math.sin(ang) * length * 0.9 - 5 * scale
            px, py = -math.sin(ang), -math.cos(ang)
            d.polygon([(bx + px * width, by + py * width), (tipx, tipy), (bx - px * width, by - py * width)], fill=color)
        d.ellipse([cx - 9 * k * scale, cy - 11 * k * scale, cx + 9 * k * scale, cy + 5 * k * scale], fill=color)


def _duck(d: ImageDraw.ImageDraw, x: float, y: float, step: int, dead: bool = False):
    """A little side-on duck facing right. (x, y) is the middle of its body."""
    if dead:
        d.ellipse([x - 14, y - 4, x + 14, y + 10], fill=(170, 170, 120))                 # flipped over
        d.ellipse([x + 9, y + 2, x + 21, y + 14], fill=(170, 170, 120))
        d.line([x + 13, y + 5, x + 17, y + 9], fill=(60, 60, 60), width=2)                # X eye
        d.line([x + 17, y + 5, x + 13, y + 9], fill=(60, 60, 60), width=2)
        d.line([x - 8, y - 4, x - 10, y - 12], fill=BEAK, width=2)                         # feet up
        d.line([x - 1, y - 4, x - 2, y - 12], fill=BEAK, width=2)
        return
    leg = 4 if step % 2 == 0 else -4
    d.line([x - 4, y + 9, x - 4 + leg, y + 16], fill=BEAK, width=3)                        # running legs
    d.line([x + 4, y + 9, x + 4 - leg, y + 16], fill=BEAK, width=3)
    d.ellipse([x - 15, y - 10, x + 13, y + 11], fill=YELLOW)                              # body
    d.ellipse([x - 12, y - 4, x + 2, y + 7], fill=(235, 190, 40))                         # wing
    d.ellipse([x + 7, y - 19, x + 22, y - 4], fill=YELLOW)                                # head
    d.polygon([(x + 21, y - 14), (x + 30, y - 11), (x + 21, y - 8)], fill=BEAK)           # beak
    d.ellipse([x + 14, y - 15, x + 18, y - 11], fill=(20, 20, 20))                        # eye


def _tomb(d: ImageDraw.ImageDraw, x: float, y: float):
    d.rounded_rectangle([x - 11, y - 14, x + 11, y + 14], radius=8, fill=(110, 110, 120))
    d.line([x, y - 8, x, y + 4], fill=(60, 60, 68), width=2)
    d.line([x - 5, y - 4, x + 5, y - 4], fill=(60, 60, 68), width=2)


def _trim(text: str, n: int = 15) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def render_finale_gif(labels: list[str], elim_order: list[int], winners: list[int], seed: str = "") -> tuple[bytes, float]:
    """
    labels      display name for each of the ten ducks (index = lane)
    elim_order  duck indexes in the order their flames go out
    winners     the duck indexes that survive

    Returns (gif_bytes, seconds_to_play_before_the_ending_is_revealed).
    """
    n = len(labels)
    rng = random.Random(f"fireball-{seed}")
    H = TOP + n * LANE_H + BOTTOM
    f_big, f_mid, f_small = _font(22), _font(15), _font(13)

    # --- plan the race -----------------------------------------------------
    # Each death happens at a point in the race; survivors cross the line late.
    k = len(elim_order)
    death_t: dict[int, float] = {}
    for i, duck in enumerate(elim_order):
        base = 0.13 + (0.62 * i / max(1, k - 1))
        death_t[duck] = min(0.80, base + rng.uniform(-0.015, 0.015))
    finish_t = {w: rng.uniform(0.86, 1.0) for w in winners}
    pace = {i: rng.uniform(0.9, 1.12) for i in range(n)}
    wob = {i: rng.uniform(0, 6.28) for i in range(n)}

    def progress(i: int, t: float) -> float:
        """0..1 along the track for duck i at race time t (0..1)."""
        if i in finish_t:
            p = min(1.0, (t / finish_t[i]) ** (0.82 * pace[i]))
            return p
        td = death_t[i]
        reach = 0.58 + 0.30 * td                                  # how far a duck gets scales with how long it lasts
        p = reach * min(1.0, (min(t, td) / td) ** (0.85 * pace[i]))
        return p

    SMOKE_T = 0.045
    frames: list[Image.Image] = []
    durations: list[int] = []
    f_big_h = 24

    for fr in range(RACE_FRAMES + 1):
        t = fr / RACE_FRAMES
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.text((14, 11), "THE DUCKS ARE ON FIRE", font=f_big, fill=(255, 150, 40))

        burning = sum(1 for i in range(n) if i in finish_t or t < death_t[i] + SMOKE_T)
        d.text((W - 170, 15), f"{burning} still burning", font=f_mid, fill=(255, 200, 120))

        # current death banner
        for duck in elim_order:
            if death_t[duck] - 0.01 <= t <= death_t[duck] + 0.075:
                d.text((330, 15), f"{_trim(labels[duck], 18)}'s flame goes out!", font=f_small, fill=RED)

        for i in range(n):
            y0 = TOP + i * LANE_H
            d.rectangle([0, y0, W, y0 + LANE_H], fill=LANE_A if i % 2 == 0 else LANE_B)
            cy = y0 + LANE_H / 2 + 1
            dead = i not in finish_t and t >= death_t[i] + SMOKE_T
            dying = i not in finish_t and death_t[i] <= t < death_t[i] + SMOKE_T
            name_fill = GREY if dead else WHITE
            d.text((10, cy - 9), _trim(labels[i]), font=f_mid, fill=name_fill)
            if dead:
                d.line([10, cy, 10 + min(135, 8.2 * len(_trim(labels[i]))), cy], fill=GREY, width=1)

            x = START_X + progress(i, t) * (FINISH_X - START_X - 34)
            bob = math.sin(fr * 0.9 + wob[i]) * 2.2 if not dead else 0
            frng = random.Random(fr * 97 + i * 13 + zlib.crc32(seed.encode()) % 1000)

            if dead:
                _tomb(d, x, cy)
                _duck(d, x + 22, cy + 3, fr, dead=True)
            elif dying:
                u = (t - death_t[i]) / SMOKE_T                  # 0..1: flame shrinking into smoke
                _duck(d, x, cy + bob, fr)
                _flame(d, x - 4, cy - 2 + bob, frng, scale=max(0.15, 1.3 * (1 - u)))
                for puff in range(4):
                    px = x - 8 + frng.uniform(-10, 14)
                    py = cy - 14 - u * 16 - puff * 3 + bob
                    r = 3 + puff + u * 3
                    d.ellipse([px - r, py - r, px + r, py + r], fill=SMOKE)
            else:
                crossed = i in finish_t and t >= finish_t[i]
                _flame(d, x - 4, cy - 2 + bob, frng, scale=1.3 if not crossed else 1.45)
                _duck(d, x, cy + bob, fr)

        # finish line
        for r in range(0, n * LANE_H, 10):
            for c in range(2):
                col = WHITE if ((r // 10) + c) % 2 == 0 else (30, 30, 36)
                d.rectangle([FINISH_X + c * 10, TOP + r, FINISH_X + c * 10 + 9, TOP + r + 9], fill=col)
        d.text((FINISH_X - 6, 28), "FINISH", font=f_small, fill=GOLD)

        frames.append(img)
        durations.append(FRAME_MS)

    # winners' frame, held
    last = frames[-1].copy()
    d = ImageDraw.Draw(last)
    d.rectangle([0, 0, W, TOP - 2], fill=BG)
    word = {1: "ONE", 2: "TWO", 3: "THREE"}.get(len(winners), str(len(winners)))
    d.text((14, 11), f"LAST {word} STANDING", font=f_big, fill=GOLD)
    d.text((330, 15), " · ".join(_trim(labels[w], 11) for w in winners), font=f_small, fill=GOLD)
    for w in winners:
        y0 = TOP + w * LANE_H
        d.rectangle([2, y0 + 1, W - 3, y0 + LANE_H - 1], outline=GOLD, width=2)
    frames.append(last)
    durations.append(HOLD_MS)

    paletted = [f.quantize(colors=56, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in frames]
    buf = io.BytesIO()
    paletted[0].save(buf, format="GIF", save_all=True, append_images=paletted[1:], duration=durations,
                     loop=1, optimize=False, disposal=1)
    seconds = (RACE_FRAMES * FRAME_MS) / 1000 + 1.5
    return buf.getvalue(), seconds


if __name__ == "__main__":      # quick manual preview: python fireanim.py
    names = [f"Player {i + 1}" for i in range(10)]
    data, secs = render_finale_gif(names, [4, 1, 8, 0, 6, 2, 9], [3, 5, 7], "demo")
    with open("preview.gif", "wb") as fh:
        fh.write(data)
    print(len(data) / 1e6, "MB", secs, "s")
