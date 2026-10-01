# -*- coding: utf-8 -*-
"""用 Spire.Presentation 真实渲染 PPTX，并与原图逐像素比对（没有 PowerPoint / LibreOffice 时唯一可行的核验）。

用法：
  python render_check.py --pptx deck.pptx --ref 原图.png --canvas 1193x436 --out-dir .

依赖：pip install Spire.Presentation（免费版渲染 1280x720，无页数限制问题）

`render_and_norm()` 是渲染+归一化的**单一真源**，`calibrate_layout.py`（闭环校准）直接
import 它，保证两者量到的偏差同口径。
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image
from spire.presentation import Presentation

sys.stdout.reconfigure(encoding="utf-8")


def render_and_norm(pptx, out_dir=".", canvas=None, ref=None, ref_top=None):
    """渲染 PPTX 第 1 页，缩放到 canvas 尺寸并落盘归一化图。

    返回 dict：raw / norm / CW / CH / full_h / top / scale / slides
      norm 图是**整页**（含上下留白），内容区在其中 y ∈ [top, top+CH)；
      想把渲染图与原图同坐标系比较，直接切 `norm[top:top+CH]`。

    canvas 为 "WxH" 字符串；不给则取 ref 的实际尺寸，再不给就取渲染原图尺寸。
    """
    os.makedirs(out_dir, exist_ok=True)
    raw_png = os.path.join(out_dir, "render-pptx.png")
    pres = Presentation()
    pres.LoadFromFile(pptx)
    n = pres.Slides.Count
    pres.Slides[0].SaveAsImage().Save(raw_png)
    pres.Dispose()

    if canvas:
        CW, CH = (int(v) for v in str(canvas).lower().split("x"))
    elif ref:
        CW, CH = Image.open(ref).size
    else:
        CW, CH = Image.open(raw_png).size

    r = np.array(Image.open(raw_png).convert("RGB")).astype(np.int16)
    sc = r.shape[1] / CW
    h = int(round(r.shape[0] / sc))
    res = np.array(Image.fromarray(r.astype(np.uint8)).resize((CW, h), Image.LANCZOS)).astype(np.int16)
    norm = os.path.join(out_dir, "render-pptx-norm.png")
    Image.fromarray(res.astype(np.uint8)).save(norm)
    top = ref_top if ref_top is not None else int(round((h - CH) / 2))
    return {"raw": raw_png, "norm": norm, "CW": CW, "CH": CH,
            "full_h": h, "top": top, "scale": sc, "slides": n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pptx", required=True)
    ap.add_argument("--ref", required=True, help="原图，用于比对")
    ap.add_argument("--canvas", default=None, help="原图尺寸 WxH，默认自动取原图实际尺寸")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--ref-top", type=int, default=None, help="原图在整页中的上边距，默认自动算")
    ap.add_argument("--block", type=int, default=40)
    args = ap.parse_args()

    info = render_and_norm(args.pptx, args.out_dir, args.canvas, args.ref, args.ref_top)
    print(f"载入成功，{info['slides']} 页")
    print(f"渲染输出 {info['raw']}；原图 {info['CW']}x{info['CH']}")
    print(f"归一化 {info['CW']}x{info['full_h']}（水平系数 {info['scale']:.4f}），"
          f"内容区 y {info['top']}..{info['top'] + info['CH']}")

    CW, CH, top = info["CW"], info["CH"], info["top"]
    ref = Image.open(args.ref).convert("RGB")
    res = np.array(Image.open(info["norm"]).convert("RGB")).astype(np.int16)

    o = np.array(ref).astype(np.int16)
    p = res[top:top + CH]
    diff = np.abs(o - p).max(axis=2)
    print(f"\n与原图逐像素差：mean={diff.mean():.2f} 中位={np.median(diff):.0f} "
          f">30={100.0*(diff>30).mean():.2f}%  >60={100.0*(diff>60).mean():.2f}%")
    rows = sorted(((float(diff[y:y+args.block, x:x+args.block].mean()), x, y)
                   for y in range(0, CH, args.block) for x in range(0, CW, args.block)), reverse=True)
    print("差异最大 8 块：")
    for mn, x, y in rows[:8]:
        kind = "文字区(字形差异)" if y < 120 or (330 < y < 375) else "图形/背景区(查真问题)"
        print(f"   x={x:4d} y={y:3d} mean={mn:6.1f}  {kind}")

    cmp_path = os.path.join(args.out_dir, "compare-render.png")
    cmpi = Image.new("RGB", (CW, CH * 3 + 40), (255, 255, 255))
    cmpi.paste(ref, (0, 0))
    cmpi.paste(Image.fromarray(p.astype(np.uint8)), (0, CH + 20))
    cmpi.paste(Image.fromarray(np.clip(diff * 3, 0, 255).astype(np.uint8)).convert("RGB"), (0, CH * 2 + 40))
    cmpi.save(cmp_path)
    print(f"\n三方对比（上=原图 / 中=PPTX 渲染 / 下=差异×3）：{cmp_path}")
    print("文字行差异只看形态：位置/宽度对上了即合格；图形区出现差异块才是素材没对齐。")


if __name__ == "__main__":
    main()
