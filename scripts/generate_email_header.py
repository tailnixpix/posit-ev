"""
scripts/generate_email_header.py

Generates web/static/email_header.png — the static header image used in
the daily newsletter email. Run this once to regenerate after brand changes.

Output: 600 x 190 px PNG
  - Dark indigo gradient background (#1E1A47 → #534AB7)
  - Subtle rising line-chart in the background
  - Arc symbol (Posit+EV logo mark)
  - "Posit+EV" wordmark (white with purple +)
"""

import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ── Output path ──────────────────────────────────────────────────────────────
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT  = os.path.join(REPO, "web", "static", "email_header.png")

W, H = 600, 190
SCALE = 2          # render 2× then downscale for crisp edges on retina

# ── Colours ──────────────────────────────────────────────────────────────────
C_DARK   = (30, 26, 71)       # #1E1A47
C_MID    = (52, 40, 138)      # #34288A
C_LIGHT  = (83, 74, 183)      # #534AB7
C_WHITE  = (255, 255, 255)
C_PURPLE = (155, 142, 255)    # #9B8EFF
C_ARC    = (123, 110, 232)    # #7B6EE8


def lerp_color(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def hex_to_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def build_gradient_array(w, h, c0, c1, c2):
    """Diagonal gradient: top-left→bottom-right, two-stop."""
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            t = (x / w * 0.5 + y / h * 0.5)   # diagonal blend 0→1
            if t < 0.55:
                t2 = t / 0.55
                c = lerp_color(c0, c1, t2)
            else:
                t2 = (t - 0.55) / 0.45
                c = lerp_color(c1, c2, t2)
            arr[y, x] = c
    return arr


# ── Chart line points (normalised to W×H) ───────────────────────────────────
_RAW_PTS = [
    (0, 168), (40, 155), (80, 162), (120, 142), (160, 148),
    (200, 124), (250, 130), (295, 108), (335, 114),
    (375, 90),  (415, 96),  (455, 72),  (500, 78),
    (540, 54),  (580, 38),  (600, 28),
]
_PEAK_PTS = [(120, 142), (295, 108), (455, 72), (580, 38)]


def scale_pts(pts, s):
    return [(x * s, y * s) for x, y in pts]


def draw_chart(draw, pts, peak_pts, alpha_line=70, alpha_fill=28, s=1):
    # Area fill under the curve (closed polygon to bottom)
    fill_pts = pts + [(W * s, H * s), (0, H * s)]
    fill_col = (83, 74, 183, alpha_fill)
    draw.polygon(fill_pts, fill=fill_col)

    # Line
    line_col = (155, 142, 255, alpha_line)
    draw.line(pts, fill=line_col, width=max(1, round(1.8 * s)), joint="curve")

    # Dots
    for cx, cy in peak_pts:
        r = 2.5 * s
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=(155, 142, 255, 100),
        )


def draw_arc_symbol(draw, cx, cy, s=1):
    """Draw the Posit+EV arc symbol centred at (cx, cy)."""
    R = 52 * s      # arc radius to top dot
    sw = max(2, round(7 * s))

    # Right-half shaded fill (bezier approximation as polygon)
    # Approximate the cubic bezier M 0,-R C 28,-R 57,0 68,38 L 0,38 Z
    # using a fan of triangles / a polygon
    half_fill = []
    # parametric approximation of the right half of the arc
    for i in range(20):
        t = i / 19
        # cubic bezier: P0=(0,-R) P1=(28*s,-R) P2=(57*s,0) P3=(68*s,38*s)
        bx = (1-t)**3 * 0 + 3*(1-t)**2*t * 28*s + 3*(1-t)*t**2 * 57*s + t**3 * 68*s
        by = (1-t)**3 * (-R) + 3*(1-t)**2*t * (-R) + 3*(1-t)*t**2 * 0 + t**3 * 38*s
        half_fill.append((cx + bx, cy + by))
    half_fill.append((cx, cy + 38 * s))  # close at centre bottom
    draw.polygon(half_fill, fill=(83, 74, 183, 46))

    # Main arc (left half): M -68,38 C -43,-52 43,-52 68,38
    arc_pts = []
    for i in range(40):
        t = i / 39
        # cubic bezier: P0=(-68*s, 38*s)  P1=(-43*s,-52*s)  P2=(43*s,-52*s)  P3=(68*s,38*s)
        bx = (1-t)**3*(-68*s) + 3*(1-t)**2*t*(-43*s) + 3*(1-t)*t**2*(43*s) + t**3*(68*s)
        by = (1-t)**3*(38*s)  + 3*(1-t)**2*t*(-52*s) + 3*(1-t)*t**2*(-52*s) + t**3*(38*s)
        arc_pts.append((cx + bx, cy + by))
    draw.line(arc_pts, fill=(*C_ARC, 255), width=sw, joint="curve")

    # Glow ring behind top dot
    gr = 15 * s
    draw.ellipse(
        [cx - gr, cy - R - gr, cx + gr, cy - R + gr],
        fill=(83, 74, 183, 56),
    )
    # Top dot
    dr = 9 * s
    draw.ellipse(
        [cx - dr, cy - R - dr, cx + dr, cy - R + dr],
        fill=(*hex_to_rgb("8B7EF8"), 255),
    )


def find_font(size):
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Black.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Geneva.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def main():
    s = SCALE
    iw, ih = W * s, H * s

    # ── 1. Gradient background ──────────────────────────────────────────────
    arr = build_gradient_array(iw, ih, C_DARK, C_MID, C_LIGHT)
    img = Image.fromarray(arr, "RGB").convert("RGBA")

    # ── 2. Overlay layer (chart + arc on transparent canvas) ───────────────
    overlay = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    pts      = scale_pts(_RAW_PTS,  s)
    peak_pts = scale_pts(_PEAK_PTS, s)
    draw_chart(draw, pts, peak_pts, s=s)
    draw_arc_symbol(draw, W // 2 * s, 92 * s, s=s)

    img = Image.alpha_composite(img, overlay)

    # ── 3. Text layer ───────────────────────────────────────────────────────
    txt_layer = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
    td = ImageDraw.Draw(txt_layer)

    # Wordmark "Posit+EV" — two passes to colour "+" differently
    wm_size = 44 * s
    font_bold = find_font(wm_size)
    font_sm   = find_font(18 * s)

    # Measure each segment
    dummy = Image.new("RGBA", (1, 1))
    dm = ImageDraw.Draw(dummy)

    def text_width(text, font):
        bb = dm.textbbox((0, 0), text, font=font)
        return bb[2] - bb[0]

    w_posit = text_width("Posit", font_bold)
    w_plus  = text_width("+",     font_bold)
    w_ev    = text_width("EV",    font_bold)
    total_w = w_posit + w_plus + w_ev

    cx = iw // 2
    wm_y = 154 * s   # baseline y

    x_posit = cx - total_w // 2
    x_plus  = x_posit + w_posit
    x_ev    = x_plus  + w_plus

    td.text((x_posit, wm_y), "Posit", font=font_bold, fill=(*C_WHITE, 255),
            anchor="ls")
    td.text((x_plus,  wm_y), "+",     font=font_bold, fill=(*C_PURPLE, 255),
            anchor="ls")
    td.text((x_ev,    wm_y), "EV",    font=font_bold, fill=(*C_WHITE, 255),
            anchor="ls")

    img = Image.alpha_composite(img, txt_layer)

    # ── 4. Downscale 2× → 1× for antialiasing ──────────────────────────────
    final = img.resize((W, H), Image.LANCZOS).convert("RGB")
    final.save(OUT, "PNG", optimize=True)
    print(f"✓ Saved {OUT}  ({W}×{H}px)")


if __name__ == "__main__":
    main()
