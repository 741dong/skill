# -*- coding: utf-8 -*-
"""把原图的内容区抹除，生成平滑背景底图（bg.png）与整页延展版（bg-full.png）。

用法：
  python make_background.py --src 原图.png --out assets --slide 960x540 [--clean-below 428]

原理：背景容差掩码 → 按连通域面积去噪 → 膨胀 → 最近邻填充 → 高斯外推。
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

sys.stdout.reconfigure(encoding="utf-8")


def parse_size(s):
    w, h = s.lower().split("x")
    return int(w), int(h)


def fix_border(a, thr=30.0):
    """修掉原图最外圈的异常边（常见：导出残留的 1 px 黑边/白边）。
    若某条外边与相邻内圈的均值差超过 thr，就用内圈整条替换——否则它会被当成“内容”留进底图，
    并在整页延展时把边界色带一路带下去。"""
    fixed = []
    for outer, inner, name in [((slice(None), -1), (slice(None), -2), "右"),
                               ((slice(None), 0), (slice(None), 1), "左"),
                               ((0, slice(None)), (1, slice(None)), "上"),
                               ((-1, slice(None)), (-2, slice(None)), "下")]:
        o, i = a[outer], a[inner]
        d = float(np.abs(o - i).mean())
        if d > thr:
            a[outer] = i
            fixed.append(f"{name}边(差{d:.0f})")
    return a, fixed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="原图路径")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--slide", default="960x540", help="幻灯片尺寸（pt），如 960x540")
    ap.add_argument("--bg", default=None, help="背景基准色 r,g,b；缺省用四边条带中位数估计")
    ap.add_argument("--tol", type=float, default=9, help="背景判定容差（通道最大差）")
    ap.add_argument("--min-area", type=int, default=40, help="连通域面积阈值，低于此视为噪点")
    ap.add_argument("--dilate", type=int, default=6, help="掩码膨胀迭代次数（3x3）")
    ap.add_argument("--sigma", type=float, default=20, help="外推高斯半径")
    ap.add_argument("--clean-below", type=int, default=None,
                    help="该行起（含）全宽改用顶部干净条带覆盖，用于修底部被内容夹住的色带")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    a = np.array(Image.open(args.src).convert("RGB")).astype(np.float64)
    a, fixed = fix_border(a)
    H, W, _ = a.shape
    print(f"原图 {W}x{H}" + (f"；已修边框：{', '.join(fixed)}" if fixed else ""))

    if args.bg:
        bg = np.array([float(v) for v in args.bg.split(",")])
    else:
        # 用四边各 8 px 条带估计背景基准色
        edge = np.concatenate([a[:8].reshape(-1, 3), a[-8:].reshape(-1, 3),
                               a[:, :8].reshape(-1, 3), a[:, -8:].reshape(-1, 3)])
        bg = np.median(edge, axis=0)
    print(f"  背景基准色 {tuple(int(v) for v in bg)}")

    raw = np.abs(a - bg).max(axis=2) > args.tol
    lab, nlab = ndimage.label(raw, structure=np.ones((3, 3)))
    if nlab:
        sizes = ndimage.sum(raw, lab, index=range(1, nlab + 1))
        keep = np.where(sizes >= args.min_area)[0] + 1
        m = np.isin(lab, keep)
    else:
        m = raw
    print(f"  掩码：原始 {int(raw.sum())} → 连通域 {nlab} 个 / 保留 {int(m.sum())} "
          f"({100.0*m.sum()/(W*H):.1f}%)")
    m = ndimage.binary_dilation(m, structure=np.ones((3, 3)), iterations=args.dilate)
    print(f"  膨胀 {args.dilate} 次后 {int(m.sum())} ({100.0*m.sum()/(W*H):.1f}%)")

    idx = ndimage.distance_transform_edt(m, return_distances=False, return_indices=True)
    rough = a[idx[0], idx[1]]
    smooth = ndimage.gaussian_filter(rough, sigma=(args.sigma, args.sigma, 0))
    out = a.copy()
    out[m] = smooth[m]

    clean_row = np.median(a[:24], axis=0)
    if args.clean_below is not None:
        out[args.clean_below:] = clean_row
        print(f"  y>={args.clean_below} 已用顶部干净条带覆盖")

    bgu = np.clip(out, 0, 255).astype(np.uint8)
    Image.fromarray(bgu).save(os.path.join(args.out, "bg.png"))
    print(f"  写出 bg.png  min={bgu.reshape(-1,3).min(axis=0)} max={bgu.reshape(-1,3).max(axis=0)} "
          f"std={bgu.reshape(-1,3).std(axis=0).round(1)}")

    # 整页延展：内容区垂直居中，上下用首/末行延展
    sw_pt, sh_pt = parse_size(args.slide)
    full_h = int(round(sh_pt / (sw_pt / W)))
    ytop = int(round((full_h - H) / 2))
    full = np.zeros((full_h, W, 3), dtype=np.uint8)
    full[:ytop] = bgu[0:1]
    full[ytop:ytop + H] = bgu
    full[ytop + H:] = bgu[-1:]
    Image.fromarray(full).save(os.path.join(args.out, "bg-full.png"))
    print(f"  写出 bg-full.png {W}x{full_h}（内容区 y {ytop}..{ytop+H}）")

    # 顺带输出定位信息，供 layout 使用
    print(f"\n设计基准 canvas={W}x{H}  slide={sw_pt}x{sh_pt}pt  "
          f"1px={sw_pt/W:.4f}pt  内容区上边距={ytop}px")


if __name__ == "__main__":
    main()
