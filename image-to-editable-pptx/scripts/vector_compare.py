#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「原图切片（位图） ↔ 矢量重绘（SVG）」并排对比图。

矢量化重绘后必做的一件事：让用户一眼看出重绘得像不像。渲染自检（无失败）只说明
SVG 能画出来，不说明画得对 —— 本项目先后出过「无线信号画成蝴蝶」「显示器底座多色斑」
「胶囊文字顶满边框」三处渲染成功但视觉不对的问题，全靠这张对比图抓出来。

用法：
    python vector_compare.py pairs.json [--out 对比图.png]

pairs.json:
{
  "render_dir": "slices/_probe/render",        // 矢量渲染出的 PNG 目录
  "out": "slices/_sheet-vector-compare.png",
  "title": "原图切片（位图） → 矢量重绘（SVG）",
  "subtitle": "源图低清位图；右侧为按原图形态重新绘制的矢量版本，透明底、可无损缩放",
  "cols_width": 1180,
  "items": [
    {"label": "云平台", "orig": "slices/01-graphics/01_cloud-platform.png",
     "vec": "01_cloud-platform.svg", "max_side": 190},
    {"label": "车站阵列 7×5", "vec": "18_station-grid.svg", "max_side": 250,
     "orig_crop": {"src": "原图.png", "box": [806,147,1193,348]}},
    {"label": "箭头 A1", "orig": "slices/01-graphics/05_arrow-a1.png",
     "vec": "14_arrow-a1.svg", "h": 34}
  ]
}

尺寸控制三选一（缺省 max_side=190）：max_side 最长边 / h 固定高 / w 固定宽。
路径相对 pairs.json 所在目录解析。
"""
import argparse
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding="utf-8")

BG = (247, 249, 252, 255)
CARD = (255, 255, 255, 255)
LINE = (223, 231, 241, 255)
INK = (44, 62, 88, 255)
MUTED = (132, 148, 168, 255)
ACCENT = (47, 125, 196, 255)

FONTS = [("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc"),
         ("/System/Library/Fonts/PingFang.ttc",) * 2,
         ("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
          "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc")]


def font(sz, bold=False):
    for pair in FONTS:
        try:
            return ImageFont.truetype(pair[0] if bold else pair[-1], sz)
        except Exception:
            continue
    return ImageFont.load_default()


def load_scaled(path, item):
    im = Image.open(path).convert("RGBA")
    if "max_side" in item:
        k = item["max_side"] / max(im.size)
    elif "h" in item:
        k = item["h"] / im.height
    elif "w" in item:
        k = item["w"] / im.width
    else:
        k = 190 / max(im.size)
    return im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))),
                     Image.LANCZOS)


def main():
    ap = argparse.ArgumentParser(description="原图切片 ↔ 矢量重绘 对比图")
    ap.add_argument("pairs")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    base = os.path.dirname(os.path.abspath(args.pairs))
    cfg = json.load(open(args.pairs, encoding="utf-8"))
    rdir = os.path.join(base, cfg.get("render_dir", "."))

    def R(p):
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    units = []
    missing = []
    for it in cfg["items"]:
        vec_png = os.path.join(rdir, os.path.basename(it["vec"]).replace(".svg", ".png"))
        if not os.path.exists(vec_png):
            missing.append(it["vec"])
            continue
        vec = load_scaled(vec_png, it)
        orig = None
        if it.get("orig_crop"):
            src = R(it["orig_crop"]["src"])
            box = it["orig_crop"]["box"]
            tmp = Image.open(src).convert("RGBA").crop(tuple(box))
            import tempfile
            t = os.path.join(tempfile.gettempdir(), "_vc_crop.png")
            tmp.save(t)
            orig = load_scaled(t, it)
        elif it.get("orig"):
            p = R(it["orig"])
            if os.path.exists(p):
                orig = load_scaled(p, it)
        uw = max(orig.width if orig else 0, vec.width)
        uh = max(orig.height if orig else 0, vec.height, 66)
        units.append(dict(label=it["label"], orig=orig, vec=vec, w=uw, h=uh))

    if not units:
        sys.exit("没有任何可用项（检查 render_dir 与 vec 文件名）")
    if missing:
        print(f"  ! 缺渲染 PNG，跳过 {len(missing)} 项: {missing}")

    PAD, GAP, MARGIN, LABEL_H = 24, 26, 40, 46
    CONTENT = cfg.get("cols_width", 1180) - MARGIN * 2
    rows, cur, curw = [], [], 0
    for u in units:
        cw = u["w"] * 2 + GAP
        if cur and curw + cw + PAD * 2 > CONTENT:
            rows.append(cur)
            cur, curw = [], 0
        cur.append(u)
        curw += cw + PAD * 2
    if cur:
        rows.append(cur)

    head = 118
    total_h = head + sum(max(u["h"] for u in r) + LABEL_H + PAD * 2 for r in rows) + MARGIN
    total_w = cfg.get("cols_width", 1180)
    canvas = Image.new("RGBA", (total_w, total_h), BG)
    d = ImageDraw.Draw(canvas)
    d.text((MARGIN, 30), cfg.get("title", "原图切片（位图） → 矢量重绘（SVG）"),
           font=font(25, True), fill=INK)
    d.text((MARGIN, 66), cfg.get("subtitle", ""), font=font(15), fill=MUTED)
    d.line([(MARGIN, head - 18), (total_w - MARGIN, head - 18)], fill=LINE, width=1)

    y = head
    for r in rows:
        rh = max(u["h"] for u in r)
        x = MARGIN
        for u in r:
            cw = u["w"] * 2 + GAP
            d.rounded_rectangle([x, y, x + cw, y + rh + LABEL_H + 16],
                                radius=10, fill=CARD, outline=LINE, width=1)
            oy = y + 14 + (rh - u["h"]) // 2
            ix = x + (cw - (u["w"] * 2 + GAP)) // 2
            if u["orig"] is not None:
                canvas.alpha_composite(
                    u["orig"], (ix + (u["w"] - u["orig"].width) // 2,
                                oy + (u["h"] - u["orig"].height) // 2))
            canvas.alpha_composite(
                u["vec"], (ix + u["w"] + GAP + (u["w"] - u["vec"].width) // 2,
                           oy + (u["h"] - u["vec"].height) // 2))
            d.text((x + cw / 2, y + rh + 24), u["label"], font=font(15, True),
                   fill=INK, anchor="mm")
            x += cw + PAD * 2
        y += rh + LABEL_H + PAD * 2

    out = args.out or R(cfg.get("out", "vector-compare.png"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    canvas.convert("RGB").save(out)
    print(f"对比图 -> {out}  {canvas.size}  （{len(units)} 组）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
