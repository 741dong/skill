#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""细线/曲线连通图的**中心线描摹器**：位图掩码 → 干净的可缩放 SVG stroke 路径。

适用：链路连线、曲线扇面、流程引线、括号、装饰细线等「一条一条細线」的图形。
不适用：实心色块/插画（那些走 vtracer 或手工重绘）。

原理：
  前景掩码 → Zhang-Suen 细化成 1px 骨架 → 建图提取度!=2 的节点与度==2 的链
  → 每条链做 Douglas-Peucker 简化 → Catmull-Rom 转三次贝塞尔出光滑 path
  → 圆形端点单独识别为 <circle>

为什么要中心线而不是轮廓：轮廓法（vtracer）对 2–3px 细线会产出锯齿状填充多边形，
点多、体积大、缩放后毛边明显；中心线 + stroke 只有几十个点，且线宽、圆头、颜色都可控。

用法：
    python trace_lines.py config.json

config.json:
{
  "src": "sources/phase2.png",
  "out": "vec/wiring.svg",
  "canvas": [1084, 416],                 // 输出 SVG 的 viewBox 尺寸（= 原图尺寸）
  "background": [218, 226, 241],         // 背景基准色
  "tol": 10,                             // 前景判定容差
  "region": [260, 130, 870, 380],        // 只处理该矩形内
  "exclude": [[88,128,296,378], ...],    // 区域内要排除的矩形（卡片、图标等）
  "line_width": 2.2,                     // 描线宽度（px）；null = 按实测自动
  "colors": 5,                           // 把线条按颜色聚成几组
  "dots": {"min_area": 25, "max_area": 130, "max_ratio": 1.7},
  "simplify": 0.7,                       // Douglas-Peucker 容差（px）
  "min_len": 12,                         // 短于此长度的链丢弃
  "drop_faint": 26,                      // 链颜色距底色小于此值则丢弃（≈背景，看不见）
  "recenter": true,                      // 骨架沿法线重新居中到掩码中线（见下）
  "recenter_half": null                 // 采样半径；缺省 ceil(lw/2)+2
}

⚠️ line_width 的坑（本机实测）：不要用「掩码面积 / 骨架长度」估，tol 判出来的掩码
   包含抗锯齿边，宽度会被高估 30%。正确做法是 **alpha 积分**：
   p = alpha*c + (1-alpha)*bg  =>  alpha = |p-bg| / |c-bg|，
   沿垂直方向对 alpha 求和就等于真实线宽（与抗锯齿无关）。

⚠️ 骨架必须重新居中（recenter，缺省开）：Zhang-Suen 对**偶数宽度**的条带只能收敛到
   其中一行，骨架因此贴在条带一侧；而描边以骨架为轴、宽 lw，整条就偏 lw/2。
   实测 564x10 切片里的 2px 横条：实体占行 4~5、骨架落在行 4 → 描边覆盖 [3,5)
   渲染成行 3~4，与原图行 4~5 整条错位（mean 31.3 / >60 占 19.9%）。
   修正后逐行 alpha 与原图完全一致。位移 = 「被覆盖像素的质心」，
   1px 宽 +0.5、2px 宽 +1.0 —— **不能统一用 +0.5**。
   本项目实测：配置里写 2.6，真实只有 1.95 —— 渲染出来线明显偏粗。
   圆点同理，用径向 alpha 剖面找 0.5 交叉半径（实测 2.6，配置里写 3.0~4.0，偏大）。
"""
import argparse
import json
import math
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

sys.stdout.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------- 细化
def zhang_suen(img):
    """Zhang-Suen 细化：二值 bool 数组 → 1px 宽骨架。"""
    I = img.astype(np.uint8).copy()
    I = np.pad(I, 1)
    changed = True
    while changed:
        changed = False
        for step in (0, 2):
            P = I
            p2 = P[:-2, 1:-1]; p3 = P[:-2, 2:]; p4 = P[1:-1, 2:]
            p5 = P[2:, 2:];   p6 = P[2:, 1:-1]; p7 = P[2:, :-2]
            p8 = P[1:-1, :-2]; p9 = P[:-2, :-2]
            C = ((~p2 & p3).astype(np.uint8) + (~p3 & p4) + (~p4 & p5) +
                 (~p5 & p6) + (~p6 & p7) + (~p7 & p8) + (~p8 & p9) + (~p9 & p2))
            N = p2 + p3 + p4 + p5 + p6 + p7 + p8 + p9
            core = P[1:-1, 1:-1]
            if step == 0:
                cond = (p2 * p4 * p6 == 0) & (p4 * p6 * p8 == 0)
            else:
                cond = (p2 * p4 * p8 == 0) & (p2 * p6 * p8 == 0)
            rm = (core == 1) & (C == 1) & (N >= 2) & (N <= 6) & cond
            if rm.any():
                core[rm] = 0
                changed = True
    return I[1:-1, 1:-1].astype(bool)


NB = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def neighbors(sk, y, x):
    H, W = sk.shape
    out = []
    for dy, dx in NB:
        ny, nx = y + dy, x + dx
        if 0 <= ny < H and 0 <= nx < W and sk[ny, nx]:
            out.append((ny, nx))
    return out


def crossing_number(sk):
    """Rutovitz 交叉数。**不要用「8 邻域计数」判节点**：
    8 连通骨架的阶梯状走法会让一条直线上的像素出现 3 个邻居（斜邻 + 正交邻），
    整条曲线被切成大量 2 像素碎链，表现为描摹结果断成虚线。
    交叉数只数邻域序列里 0→1 的跳变，阶梯像素仍为 2，是可靠的分支判据。
    """
    H, W = sk.shape
    S = np.pad(sk.astype(np.uint8), 1)
    ring = [S[:-2, 1:-1], S[:-2, 2:], S[1:-1, 2:], S[2:, 2:],
            S[2:, 1:-1], S[2:, :-2], S[1:-1, :-2], S[:-2, :-2]]
    cn = np.zeros((H, W), np.float64)
    for i in range(8):
        a1, a2 = ring[i], ring[(i + 1) % 8]
        cn += np.abs(a1.astype(np.int16) - a2.astype(np.int16))
    return (cn / 2.0).astype(np.uint8)


def extract_chains(sk):
    """骨架 → 链列表（每条链是点序列）。交叉数 != 2 处断开。"""
    H, W = sk.shape
    cn = crossing_number(sk)
    deg = np.where(sk, cn, 0)
    ys, xs = np.where(sk)
    visited = np.zeros((H, W), bool)
    chains = []

    def walk(start, first):
        chain = [start, first]
        visited[start] = True
        visited[first] = True
        prev, cur = start, first
        while True:
            if deg[cur] != 2:
                visited[cur] = True
                break
            nxt = [p for p in neighbors(sk, *cur) if p != prev and not visited[p]]
            if not nxt:
                break
            n = nxt[0]
            visited[n] = True
            chain.append(n)
            prev, cur = cur, n
        return chain

    # 先从节点（端点 / 分支点）起步
    for y, x in zip(ys, xs):
        if deg[y, x] != 2 and not visited[y, x]:
            for n in neighbors(sk, y, x):
                if not visited[n]:
                    chains.append(walk((y, x), n))
            visited[y, x] = True
    # 剩下的是纯环
    for y, x in zip(ys, xs):
        if not visited[y, x]:
            nb = [p for p in neighbors(sk, y, x) if not visited[p]]
            if nb:
                chains.append(walk((y, x), nb[0]))
            else:
                visited[y, x] = True
    return chains


def recenter_on_mask(pts, valid, half):
    """把骨架点沿**局部法线**平移，使其落在掩码实体的中线上。

    为什么必须做：`zhang_suen` 对偶数宽度的条带只能收敛到其中一行，
    骨架因此贴在条带的一侧；而描边以骨架为轴、宽 `lw`，整条就偏 `lw/2`。

    实测（564x10 切片里的 2px 横条）：实体占行 4~5，骨架落在行 4，
    描边中心线 y=4、宽 2 → 覆盖 [3,5) → 渲染到行 3~4，与原图行 4~5
    整条错位，该元素逐像素 mean 31.3、>60 占 19.9%（全页最大热点）。
    把中心线移到 y=5 后，逐行 alpha 与原图**完全一致**。

    做法：在每个点处沿法线取 ±`half` 个整数偏移，收集掩码覆盖到的偏移 t，
    需要的位移 = 「被覆盖像素的质心」= `(t_min + t_max + 1) / 2`
    （像素 t 在连续坐标上占 [t, t+1)，中心 t+0.5）。
    1px 宽时 t 只有 {0} → 位移 +0.5（等于把索引换成像素中心），
    2px 时为 {0,1} → +1.0。**不能统一用 +0.5**：那样 2px 的条仍差 0.5px。

    `pts` / 返回值都是 (x, y) 索引坐标；`half` 取「lw/2 向上取整 + 2」留余量。
    """
    n = len(pts)
    if n < 2 or valid is None or half < 1:
        return pts
    H, W = valid.shape
    out = []
    for i, (x, y) in enumerate(pts):
        ax, ay = pts[max(0, i - 1)]
        bx, by = pts[min(n - 1, i + 1)]
        tx, ty = bx - ax, by - ay
        L = math.hypot(tx, ty)
        if L < 1e-6:
            out.append((x, y))
            continue
        nx, ny = -ty / L, tx / L          # 单位法线
        cov = []
        for t in range(-half, half + 1):
            xi, yi = int(round(x + nx * t)), int(round(y + ny * t))
            if 0 <= yi < H and 0 <= xi < W and valid[yi, xi]:
                cov.append(t)
        if not cov:
            out.append((x, y))
            continue
        shift = (cov[0] + cov[-1] + 1) / 2.0
        out.append((x + nx * shift, y + ny * shift))
    return out


def rdp(pts, eps):
    """Douglas-Peucker（迭代实现，避免递归深度）。"""
    if len(pts) < 3:
        return pts
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    P = np.asarray(pts, float)
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        seg = P[j] - P[i]
        L = np.hypot(*seg)
        if L < 1e-9:
            d = np.hypot(*(P[i + 1:j] - P[i]).T)
        else:
            v = P[i + 1:j] - P[i]
            d = np.abs(seg[0] * v[:, 1] - seg[1] * v[:, 0]) / L
        k = int(np.argmax(d))
        if d[k] > eps:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
    return [pts[i] for i in range(len(pts)) if keep[i]]


def smooth(pts, k=2, passes=2):
    """链点移动平均平滑。

    **必须在 RDP 之前做。** 骨架是 1px 量化的，垂直走一步、水平走一步会留下阶梯顶点；
    RDP 会把这些阶梯顶点当成「真实特征」保留下来，转成 Catmull-Rom 后就是肉眼可见的硬角。
    先平滑把阶梯抹掉，RDP 才会在真正的曲率处留点。
    端点不参与平均：后面还要吸附到圆点中心，端点必须保持真实位置。
    """
    P = np.asarray(pts, float)
    if len(P) < 2 * k + 1:
        return [tuple(p) for p in P]
    for _ in range(passes):
        Q = P.copy()
        for i in range(len(P)):
            lo, hi = max(0, i - k), min(len(P), i + k + 1)
            Q[i] = P[lo:hi].mean(axis=0)
        Q[0], Q[-1] = P[0], P[-1]
        P = Q
    return [tuple(p) for p in P]


def cluster_ends(ends, snap):
    """把距离 <= snap 的链端聚成一簇（并查集）。

    ⚠️ 不能用「坐标 / snap 取整」当簇键 —— 这是本机实测踩过的坑：
    snap=2 时端点 (399,165) 与 (397,166) 只差 2.2px，取整后落在 199 和 200
    两个不同格子里，聚不到一起。表现是曲线在极小间隙处断成两截、
    渲染出来是一个刺眼的硬角，而且断头还会被 min_len 当碎屑丢掉（悬空断线）。

    距离聚类没有这个边界问题。
    """
    n = len(ends)
    if n < 2:
        return []
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    s2 = snap * snap
    for i in range(n):
        xi, yi = ends[i][2]
        for j in range(i + 1, n):
            xj, yj = ends[j][2]
            if (xi - xj) ** 2 + (yi - yj) ** 2 <= s2:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[rb] = ra
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(ends[i])
    return [g for g in groups.values() if len(g) > 1]


def merge_chains(chains, snap=3, ang_thr=-0.55):
    """把在节点处首尾相接的链拼回整条曲线。

    为什么要拼：一条曲线穿过另一条线时，骨架会在交点处断成 3–4 条链。
    不拼的话会被 min_len 当成碎屑丢掉（表现为曲线断成虚线），
    拼回来还能显著减少 path 数与文件体积、让曲线更顺。

    配对规则：同一簇里的多个链端，按「出去方向」两两配对，
    取方向最接近相反（dot 最小）的一对；2 个端时直接拼。
    **一轮里把所有能配的都配上**（早先版本每个节点只配一对就 break，收敛慢且易漏）。
    """
    chains = [list(c) for c in chains]
    guard = 0
    while guard < 400:
        guard += 1
        ends = []
        for i, ch in enumerate(chains):
            if ch is None:
                continue
            ends.append((i, 0, ch[0]))
            ends.append((i, 1, ch[-1]))
        clusters = cluster_ends(ends, snap)
        merged = False
        for grp in clusters:
            live = [(i, e) for i, e, _ in grp if chains[i] is not None]
            if len(live) < 2:
                continue
            # 端点方向一律要算：**len==2 也必须过检验**。
            # 早先版本对 2 端簇无条件合并，结果两条近平行的线（左端都在 x≈268）
            # 被粘成一条发夹 —— 起终坐标都是 (435.5, 194) / (435.5, 217)。
            dirs = {}
            for i, e in live:
                ch = chains[i]
                k = min(6, len(ch) - 1)
                p0 = ch[0] if e == 0 else ch[-1]
                p1 = ch[k] if e == 0 else ch[-1 - k]
                v = np.array([p1[0] - p0[0], p1[1] - p0[1]], float)
                n = np.hypot(*v)
                dirs[(i, e)] = v / n if n > 1e-6 else np.array([0.0, 0.0])
            cand = []
            for m in range(len(live)):
                for n2 in range(m + 1, len(live)):
                    a1, a2 = live[m], live[n2]
                    if a1[0] == a2[0]:
                        continue
                    cand.append((float(dirs[a1] @ dirs[a2]), a1, a2))
            cand.sort()
            pairs = []
            used = set()
            for dot, a1, a2 in cand:
                if dot > ang_thr:
                    continue          # 不是同一条直线上的延续（含发夹情形）
                if a1 in used or a2 in used:
                    continue
                used.add(a1); used.add(a2)
                pairs.append((a1, a2))
            for (i1, e1), (i2, e2) in pairs:
                if i1 == i2 or chains[i1] is None or chains[i2] is None:
                    continue
                c1, c2 = chains[i1], chains[i2]
                if e1 == 0:
                    c1 = c1[::-1]
                if e2 == 1:
                    c2 = c2[::-1]
                chains[i1] = c1 + c2
                chains[i2] = None
                merged = True
        if not merged:
            break
    return [c for c in chains if c is not None]


def catmull_path(pts, prec=2):
    """点序列 → 平滑三次贝塞尔 path（Catmull-Rom 转 Bezier）。"""
    P = np.asarray(pts, float)
    if len(P) == 1:
        return f"M {P[0,0]:.{prec}f} {P[0,1]:.{prec}f}"
    if len(P) == 2:
        return (f"M {P[0,0]:.{prec}f} {P[0,1]:.{prec}f} "
                f"L {P[1,0]:.{prec}f} {P[1,1]:.{prec}f}")
    d = [f"M {P[0,0]:.{prec}f} {P[0,1]:.{prec}f}"]
    Q = np.vstack([P[0], P, P[-1]])
    for i in range(1, len(Q) - 2):
        p0, p1, p2, p3 = Q[i - 1], Q[i], Q[i + 1], Q[i + 2]
        c1 = p1 + (p2 - p0) / 6.0
        c2 = p2 - (p3 - p1) / 6.0
        d.append(f"C {c1[0]:.{prec}f} {c1[1]:.{prec}f} "
                 f"{c2[0]:.{prec}f} {c2[1]:.{prec}f} "
                 f"{p2[0]:.{prec}f} {p2[1]:.{prec}f}")
    return " ".join(d)


def kmeans_rgb(px, k, iters=25, seed=3):
    rng = np.random.RandomState(seed)
    C = px[rng.choice(len(px), k, replace=False)].astype(float)
    for _ in range(iters):
        d = ((px[:, None, :] - C[None, :, :]) ** 2).sum(axis=2)
        lbl = d.argmin(axis=1)
        for j in range(k):
            if (lbl == j).any():
                C[j] = px[lbl == j].mean(axis=0)
    return C, lbl


def hexc(c):
    return "#%02X%02X%02X" % tuple(int(round(v)) for v in np.clip(c, 0, 255))


def main():
    ap = argparse.ArgumentParser(description="细线连通图的中心线描摹")
    ap.add_argument("config", help="config.json")
    ap.add_argument("--out", default=None, help="覆盖 config 的 out")
    ap.add_argument("--debug-dir", default=None, help="输出掩码/骨架调试图")
    args = ap.parse_args()

    base = os.path.dirname(os.path.abspath(args.config))
    cfg = json.load(open(args.config, encoding="utf-8"))

    def R(p):
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    src = R(cfg["src"])
    out = R(args.out or cfg["out"])
    CW, CH = cfg["canvas"]
    bg = np.array(cfg.get("background", [218.0, 226.5, 241.5]), float)
    tol = cfg.get("tol", 10)

    a = np.array(Image.open(src).convert("RGB")).astype(np.float64)
    H, W, _ = a.shape
    assert (W, H) == (CW, CH), f"原图 {W}x{H} 与 canvas {CW}x{CH} 不一致"

    fg = np.abs(a - bg).max(axis=2) > tol
    mask = np.zeros((H, W), bool)
    if cfg.get("region"):
        x1, y1, x2, y2 = cfg["region"]
        reg = np.zeros((H, W), bool)
        reg[y1:y2 + 1, x1:x2 + 1] = True
    else:
        reg = np.ones((H, W), bool)
    mask = fg & reg
    valid = reg.copy()                      # 颜色采样许可区（见下）
    for ex in cfg.get("exclude", []):
        x1, y1, x2, y2 = ex
        mask[y1:y2 + 1, x1:x2 + 1] = False
        valid[y1:y2 + 1, x1:x2 + 1] = False
    # 采样许可区收缩 2px：链条末端紧贴卡片左缘/阴影，5x5 邻域会采到卡片像素
    # （实测把卡片头部蓝 #16589F 当成线色，聚出过饱和的 #1473CB）
    valid = ndimage.binary_erosion(valid, np.ones((5, 5), bool))
    print(f"原图 {W}x{H}  前景 {int(fg.sum())}  区域后 {int(mask.sum())}px")

    # ---- 圆点识别 ----
    # 关键：圆点与线是**同一个连通域**（点长在线头上），所以「连通域圆度」法必然失败。
    # 正确做法是距离变换：线半宽约 1–1.5px、圆点半宽约 3–4px，取 dt 的局部极大即可分开。
    dcfg = cfg.get("dots", {})
    dmin = dcfg.get("min_radius", 2.2)
    dots = []
    for md in cfg.get("manual_dots", []):
        x, y, r = float(md["x"]), float(md["y"]), float(md.get("r", 3.5))
        col = md.get("rgb")
        if col is None:
            yy, xx = int(round(y)), int(round(x))
            col = a[max(0, yy - 1):yy + 2, max(0, xx - 1):xx + 2].reshape(-1, 3).mean(axis=0)
        dots.append((x, y, r, np.array(col, float)))
    if not dots:
        dt = ndimage.distance_transform_edt(mask)
        mx = ndimage.maximum_filter(dt, size=int(dcfg.get("window", 7)))
        peaks = (dt >= mx - 0.01) & (dt >= dmin)
        plab, pn = ndimage.label(peaks, structure=np.ones((3, 3)))
        for i, sl in enumerate(ndimage.find_objects(plab), 1):
            if sl is None:
                continue
            ys, xs = np.where(plab[sl] == i)
            yy, xx = ys.mean() + sl[0].start, xs.mean() + sl[1].start
            r = float(dt[int(round(yy)), int(round(xx))])
            col = a[max(0, int(yy) - 1):int(yy) + 2,
                    max(0, int(xx) - 1):int(xx) + 2].reshape(-1, 3).mean(axis=0)
            dots.append((xx, yy, r, col))
        # 同一位置的重复峰去重
        uniq = []
        for x, y, r, c in sorted(dots, key=lambda t: -t[2]):
            if all((x - u[0]) ** 2 + (y - u[1]) ** 2 > (u[2] + 3) ** 2 for u in uniq):
                uniq.append((x, y, r, c))
        dots = uniq
    # 从掩码里挖掉圆点，避免它们被细化成小撮骨架
    yy, xx = np.mgrid[0:H, 0:W]
    for x, y, r, _ in dots:
        mask[(yy - y) ** 2 + (xx - x) ** 2 <= (r + 2.0) ** 2] = False
    print(f"识别圆点 {len(dots)} 个")

    # ---- 细化 + 提链 ----
    sk = zhang_suen(mask)
    print(f"骨架像素 {int(sk.sum())}")
    chains = extract_chains(sk)
    print(f"原始链 {len(chains)} 条")

    eps = cfg.get("simplify", 0.7)
    min_len = cfg.get("min_len", 12)
    # 先全部转成点序列并合并（合并前不能按长度过滤，否则穿线处会断）
    raw = [[(x, y) for y, x in ch] for ch in chains if len(ch) >= 2]
    raw = merge_chains(raw, snap=int(cfg.get("snap", 2)),
                       ang_thr=float(cfg.get("merge_angle", -0.55)))
    print(f"合并后 {len(raw)} 条")
    kept = []
    for pts in raw:
        # 端点吸附到圆点中心，避免线头与点错位
        for k in (0, -1):
            px, py = pts[k]
            for dx, dy, r, _ in dots:
                if (px - dx) ** 2 + (py - dy) ** 2 < (r + 4) ** 2:
                    pts[k] = (dx, dy)
        L = sum(np.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
                for i in range(len(pts) - 1))
        if L < min_len:
            continue
        # 平滑必须在 RDP 之前：否则 1px 量化的阶梯顶点被当成特征保留，出硬角
        kept.append((rdp(smooth(pts, k=int(cfg.get("smooth_k", 2)),
                                passes=int(cfg.get("smooth_passes", 2))), eps), L))
    print(f"保留链 {len(kept)} 条，总长 {sum(l for _, l in kept):.0f}px")

    # ---- 颜色：对每条链取「离背景最远」的像素做中位，再对链颜色聚类 ----
    # 直接取路径点上的像素会拿到抗锯齿混色（细线尤其严重），结果整片发灰。
    k = int(cfg.get("colors", 5))

    def chain_color(pts):
        cand = []
        for x, y in pts:
            xi, yi = int(round(x)), int(round(y))
            blk = a[max(0, yi - 2):yi + 3, max(0, xi - 2):xi + 3].reshape(-1, 3)
            ok = valid[max(0, yi - 2):yi + 3, max(0, xi - 2):xi + 3].reshape(-1)
            if ok.any():
                blk = blk[ok]               # 只允许采「线自己的」像素
            dd = np.abs(blk - bg).max(axis=1)
            if len(dd):
                cand.append(blk[int(dd.argmax())])
        cand = np.array(cand)
        dd = np.abs(cand - bg).max(axis=1)
        sel = cand[dd >= np.percentile(dd, 55)] if len(cand) > 4 else cand
        return np.median(sel if len(sel) else cand, axis=0)

    ccol = np.array([chain_color(pts) for pts, _ in kept])

    # ---- 先丢弃「几乎是背景色」的链，再聚类 ----
    # 顺序很重要：这类链（实测距底色仅 8）视觉上不存在，但会渲染成一条多余的浅色虚段。
    # 如果先聚类，它们会把 k-means 的簇心拖进背景区，真正的线色被挤到只剩一两个簇。
    drop = float(cfg.get("drop_faint", 26))
    cdist = np.abs(ccol - bg).max(axis=1)
    live = cdist >= drop
    drop_n = int((~live).sum())
    if drop_n:
        print(f"丢弃近底色碎链 {drop_n} 条")
        if cfg.get("debug_drop"):
            for (pts, L), dv in zip(kept, cdist):
                if dv < drop:
                    print("   drop len=%6.1f 距底色%6.1f  起%s 终%s"
                          % (L, dv, pts[0], pts[-1]))
    kept = [k for k, m in zip(kept, live) if m]
    ccol = ccol[live]

    if k > 1 and len(ccol) >= k:
        Ck, lbl = kmeans_rgb(ccol, k)
        # 按 (色相不敏感) 亮度+蓝度排序，输出稳定
        order = np.argsort(Ck[:, 0] - Ck[:, 1] * 0.5)
        Ck = Ck[order]
        remap = {int(o): i for i, o in enumerate(order)}
        lbl = np.array([remap[int(v)] for v in lbl])
    else:
        Ck, lbl = ccol, np.zeros(len(ccol), int)
    assign = [Ck[l] for l in lbl]

    # ---- 线宽：由距离变换估（掩码面积/骨架长度会把抗锯齿边也算进去，偏大）----
    lw = cfg.get("line_width")
    if lw is None:
        dt = ndimage.distance_transform_edt(mask)
        vals = dt[sk]
        vals = vals[vals > 0]
        lw = round(float(np.median(vals)) * 2.0, 2)
        lw = max(1.0, lw)

    # ---- 骨架重新居中到掩码中线（见 recenter_on_mask 的说明）----
    if cfg.get("recenter", True):
        half = int(cfg.get("recenter_half", math.ceil(lw / 2) + 2))
        moved, new_kept = 0.0, []
        for pts, L in kept:
            rp = recenter_on_mask(pts, mask, half)
            for (x0, y0), (x1, y1) in zip(pts, rp):
                moved = max(moved, math.hypot(x1 - x0, y1 - y0))
            new_kept.append((rp, L))
        kept = new_kept
        print(f"骨架重新居中（法线 ±{half}px）：最大位移 {moved:.2f}px")

    body = []
    for pts, L in kept:
        body.append(f'  <path d="{catmull_path(pts)}" fill="none" stroke="{hexc(assign.pop(0))}"\n'
                    f'        stroke-width="{lw}" stroke-linecap="round" '
                    f'stroke-linejoin="round"/>\n')
    for dx, dy, r, col in dots:
        body.append(f'  <circle cx="{dx:.2f}" cy="{dy:.2f}" r="{r:.2f}" fill="{hexc(col)}"/>\n')

    svg = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<svg xmlns="http://www.w3.org/2000/svg" '
           f'width="{CW}" height="{CH}" viewBox="0 0 {CW} {CH}">\n'
           f'  <title>{cfg.get("title", "线条描摹")}</title>\n'
           + "".join(body) + '</svg>\n')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w", encoding="utf-8").write(svg)
    print(f"\n写出 {out}  {len(svg.encode('utf-8'))} B  "
          f"{len(kept)} path + {len(dots)} circle  线宽 {lw}  颜色 {[hexc(c) for c in Ck]}")

    if args.debug_dir:
        os.makedirs(args.debug_dir, exist_ok=True)
        Image.fromarray(np.where(mask[..., None], 0, 255).astype(np.uint8).repeat(3, 2)) \
            .save(os.path.join(args.debug_dir, "mask.png"))
        Image.fromarray(np.where(sk[..., None], 0, 255).astype(np.uint8).repeat(3, 2)) \
            .save(os.path.join(args.debug_dir, "skeleton.png"))
        print(f"调试图 -> {args.debug_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
