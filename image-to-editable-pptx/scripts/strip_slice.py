# -*- coding: utf-8 -*-
"""抹掉图形切片内部的文字，保留描边与底色，产出可配合原生文本框使用的干净图形。

用法：
  python strip_slice.py --src chip.png --out chip_notext.png [--core 125] [--pad 3]

原理：先用「文字芯」(lum < core) 定位文字块的完整范围，再在该范围内逐行用
该行左右空白（跳过最外 4 px 的圆角描边）的中位色填满整块。
逐像素阈值会漏掉笔画稀疏的行；块级填充不会。
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")


def strip_text(region, pad=3, core=125, edge=4):
    """region: HxWx3 float。返回 (处理后图, 填充像素数, 文字块 bbox 或 None)。"""
    lum = region.sum(axis=2) / 3.0
    h, w = lum.shape
    out = region.copy()
    core_mask = lum < core
    if not core_mask.any():
        return out, 0, None
    ys, xs = np.where(core_mask)
    Y1, Y2 = max(0, int(ys.min()) - 3), min(h - 1, int(ys.max()) + 3)
    X1, X2 = max(0, int(xs.min()) - pad), min(w - 1, int(xs.max()) + pad)
    filled = 0
    for y in range(Y1, Y2 + 1):
        left = region[y, edge:X1]
        right = region[y, X2 + 1:w - edge]
        parts = [p for p in (left, right) if len(p)]
        if not parts:
            continue
        fill = np.median(np.vstack(parts), axis=0)
        out[y, X1:X2 + 1] = fill
        filled += 1
    return out, filled * (X2 - X1 + 1), (X1, Y1, X2, Y2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--core", type=int, default=125,
                    help="文字芯亮度阈值。要高于文字实色、低于描边/底色（半透明描边常在 145 左右）")
    ap.add_argument("--pad", type=int, default=3, help="文字块外扩像素，覆盖抗锯齿边缘")
    ap.add_argument("--check-only", action="store_true", help="只报告，不写文件")
    args = ap.parse_args()

    src = np.array(Image.open(args.src).convert("RGB")).astype(np.float64)
    out, n, bbox = strip_text(src, pad=args.pad, core=args.core)
    lm = out.sum(axis=2) / 3.0
    res = int((lm < args.core).sum())
    print(f"{os.path.basename(args.src)} {src.shape[1]}x{src.shape[0]} "
          f"文字块={bbox} 填充={n}px 残留文字芯={res} 内区最暗={lm.min():.1f}")
    if res:
        print("  ⚠️ 仍有文字芯残留：core 调高，或用 --check-only 放大观察后手工处理")
    if not args.check_only:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).save(args.out)
        print(f"  → {args.out}")


if __name__ == "__main__":
    main()
