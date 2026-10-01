# -*- coding: utf-8 -*-
"""路由判定小样本单测：合成 7 类典型元素，检验 route_elements 的判定方向。

不依赖原图切片，独立可跑：
    python unit_classify.py            # 默认建到 <本目录>/unit-out
"""
import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "unit-out")
PY = sys.executable
ROUTE = os.path.join(HERE, "..", "..", "scripts", "route_elements.py")

BG = (250, 250, 252)
RNG = np.random.default_rng(20260929)


def canvas(w, h):
    a = np.zeros((h, w, 3), np.uint8)
    a[:, :] = BG
    return a


def save(a, name):
    os.makedirs(os.path.join(OUT, "slices", "g"), exist_ok=True)
    p = os.path.join(OUT, "slices", "g", name)
    Image.fromarray(a).save(p)
    return p


# ---- 1. 胶囊 chip：少色、大面积平坦、圆角矩形
def s_chip(w=260, h=56):
    a = canvas(w, h)
    im = Image.fromarray(a)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([2, 2, w - 3, h - 3], radius=h // 2,
                        fill=(235, 243, 252), outline=(120, 160, 205), width=1)
    d.rounded_rectangle([40, 18, 90, 38], radius=10, fill=(70, 120, 175))
    return np.array(im)


# ---- 2. 实拍照片：连续色调 + 多尺度噪点
def s_photo(w=300, h=220):
    yy, xx = np.mgrid[0:h, 0:w]
    ramp = 0.55 + 0.45 * (0.6 * xx / (w - 1) + 0.4 * yy / (h - 1))
    img = np.zeros((h, w, 3), np.float64)
    for c, base in enumerate((146, 108, 82)):
        img[:, :, c] = base * ramp
    coarse = RNG.normal(0, 1, (max(2, h // 14), max(2, w // 14), 3))
    ci = Image.fromarray(np.clip(coarse * 24 + 128, 0, 255).astype(np.uint8))
    ci = ci.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(3))
    img += (np.array(ci).astype(np.float64) - 128) * 1.25
    img += RNG.normal(0, 24, (h, w, 3))
    return np.clip(img, 0, 255).astype(np.uint8)


# ---- 3. 细线：2px 连通线
def s_thinline(w=560, h=6):
    a = canvas(w, h)
    a[2:4, 20:w - 20] = (86, 104, 132)
    return a


# ---- 4. 通栏色带：水平渐变，色数少但边缘柔和
def s_band(w=1440, h=60):
    a = np.zeros((h, w, 3), np.uint8)
    t = np.linspace(0, 1, w)
    fill = np.stack([np.interp(t, [0, .5, 1], [206, 226, 206]),
                     np.interp(t, [0, .5, 1], [220, 236, 220]),
                     np.interp(t, [0, .5, 1], [238, 246, 238])], axis=1)
    a[:, :] = fill[None, :, :]
    v = np.linspace(-18, 14, h)[:, None, None]
    a = np.clip(a.astype(np.float64) + v, 0, 255).astype(np.uint8)
    return a


# ---- 5. 网格阵列：4 列 × 2 行图标，严格等间距
def s_grid(w=300, h=160, cols=4, rows=2):
    a = canvas(w, h)
    im = Image.fromarray(a)
    d = ImageDraw.Draw(im)
    dx, dy = w / cols, h / rows
    for r in range(rows):
        for c in range(cols):
            cx, cy = int(dx * (c + .5)), int(dy * (r + .5))
            d.rounded_rectangle([cx - 17, cy - 17, cx + 17, cy + 17], radius=6,
                                fill=(210, 226, 244), outline=(90, 130, 180), width=2)
            d.rectangle([cx - 7, cy - 3, cx + 7, cy + 3], fill=(50, 90, 140))
    return np.array(im)


# ---- 6. 扁平图标：几何 path，色数少但形状不规则
def s_icon(w=110, h=110):
    a = canvas(w, h)
    im = Image.fromarray(a)
    d = ImageDraw.Draw(im)
    d.polygon([(55, 8), (70, 42), (106, 46), (78, 70), (86, 105),
               (55, 86), (24, 105), (32, 70), (4, 46), (40, 42)],
              fill=(242, 206, 92), outline=(190, 140, 30))
    return np.array(im)


# ---- 7. 渐变插画：色数较多、大片渐变（应落入模糊地带，触发 needs_review）
def s_gradient_art(w=180, h=160):
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.sqrt((xx - w / 2) ** 2 + (yy - h / 2) ** 2)
    r = np.clip(r / (w / 2), 0, 1)
    a = np.zeros((h, w, 3), np.float64)
    a[:, :, 0] = 250 - 120 * r
    a[:, :, 1] = 220 - 90 * r
    a[:, :, 2] = 180 + 60 * r
    return np.clip(a, 0, 255).astype(np.uint8)


CASES = [
    ("01_chip.png", s_chip, "矢量/param", "vector"),
    ("02_photo.png", s_photo, "位图", "raster"),
    ("03_thinline.png", s_thinline, "矢量/trace", "vector"),
    ("04_band.png", s_band, "矢量/param", "vector"),
    ("05_grid.png", s_grid, "矢量/param", "vector"),
    ("06_icon.png", s_icon, "矢量/hand", "vector"),
    ("07_gradart.png", s_gradient_art, "模糊地带(位图+待确认)", "?"),
]


def main():
    os.makedirs(OUT, exist_ok=True)
    items = []
    for name, fn, _expect, _d in CASES:
        a = fn()
        save(a, name)
        h, w = a.shape[:2]
        items.append({"name": name, "group": "g", "box": [0, 0, w, h],
                      "note": name, "pad": 0, "kind": "graphic"})
    man = {"source": "synthetic", "out": os.path.join(OUT, "slices"),
           "items": items, "overview": False, "sheet": False, "zip": False,
           "title": "路由单测"}
    mp = os.path.join(OUT, "manifest.json")
    json.dump(man, open(mp, "w", encoding="utf-8"), ensure_ascii=False)

    # slice_by_plan 只认已存在的源图；这里切片已由 save() 写好，故直接造 manifest
    recs = []
    for it in items:
        p = os.path.join(OUT, "slices", "g", it["name"])
        with Image.open(p) as im:
            sz = list(im.size)
        recs.append({"file": "g/" + it["name"], "box": it["box"], "size": sz,
                     "note": it["note"], "no_index": False})
    json.dump(recs, open(os.path.join(OUT, "slices", "manifest.json"), "w",
                         encoding="utf-8"), ensure_ascii=False, indent=1)

    r = subprocess.run([PY, os.path.abspath(ROUTE), "--manifest",
                        os.path.join(OUT, "slices", "manifest.json"),
                        "--out", os.path.join(OUT, "route.json")],
                       capture_output=True, text=True, encoding="utf-8")
    print(r.stdout or "")
    if r.returncode:
        print(r.stderr)
        return 1

    got = {it["file"].split("/")[-1]: it for it in
           json.load(open(os.path.join(OUT, "route.json"), encoding="utf-8"))["items"]}
    print("=" * 74)
    print("%-16s %-9s %-7s %-9s %s" % ("样本", "期望", "实得", "手法", "判定"))
    print("-" * 74)
    bad = 0
    for name, _fn, expect, d in CASES:
        g = got[name]
        real = g["decision"] + ("/" + g["vector_submode"] if g["vector_submode"] else "")
        if d == "?":
            ok = g["needs_review"]
            mark = "OK" if ok else "!! 未标待确认"
        else:
            ok = (g["decision"] == d)
            mark = "OK" if ok else "!! 方向错"
        if not ok:
            bad += 1
        print("%-16s %-9s %-7s %-9s %s" % (name, expect, g["decision"], real, mark))
    print("-" * 74)
    print("不一致项：%d" % bad)
    for name, _f, _e, _d in CASES:
        g = got[name]
        f = g["features"]
        print("  %-16s 色数=%-5d 主色=%.2f 平坦=%.2f 梯度=%.4f 熵=%.2f 周期=%.2f 阵列=%dx%d"
              % (name, f["n_unique"], f["topk_ratio"], f["flat_run_ratio"],
                 f["grad_energy"], f["entropy"], f["periodicity"],
                 f["grid_cols"], f["grid_rows"]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
