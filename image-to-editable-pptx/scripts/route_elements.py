# -*- coding: utf-8 -*-
"""元素路由判定：每个图形切片该走「矢量重绘」还是「切片位图」。

用法:
    python route_elements.py --manifest slices/manifest.json [--out route.json] [--ref 原图.png]
    python route_elements.py --manifest m.json --list                 # 只打印判定表
    python route_elements.py --manifest m.json --force-vector 03_a.png,07_b.png
    python route_elements.py --manifest m.json --force-raster 02_photo.png
    python route_elements.py --manifest m.json --apply-route 旧route.json   # 在旧决定上叠加人工改动

判定策略（先在末轮由 --min-conf 控制宽严）:
    1. 先跑硬规则（细线 / 极扁色带 / 周期阵列 / 纯色矩形），命中即高置信度判矢量；
    2. 否则算 vector_score 与 raster_score 两组证据分，取差值；
    3. 差值不足 margin（默认 0.12）→ 判 raster 并标 needs_review（保守：位图不丢保真度）。

特征全部本地计算，不联网、不调用任何模型。

输出 route.json 的 items[] 字段:
    file            切片文件（相对 slices 根）
    box/size        源图坐标与尺寸
    decision        vector | raster | skip
    vector_submode  param | trace | hand      （decision=vector 时）
    confidence      0~1，两组证据分的差值经归一
    needs_review    布尔，建议人工过一眼
    features        原始特征值（可回测）
    reasons         逐条可读判定依据
    palette         主色列表（含 hex 与占比），供填 spec.json 用
    hints           几何提示（轴线中心、边框色等），供填 spec.json 用
"""
import argparse
import json
import math
import os
import sys

import numpy as np
from PIL import Image

QUANT_BITS = 5                      # 每通道量化位数，压掉抗锯齿噪声
GRAD_REF = 0.14                     # 梯度能量参考值（照片量级，实测标定）
ENTROPY_REF = 8.0                   # 颜色熵参考值（照片量级，实测标定）
FLAT_TOL = 6                        # 3x3 邻域「平坦」阈值（通道最大差）
BG_TOL = 24                         # 与背景色的距离阈值，判定前景
MIN_FILL = 0.01                     # 前景占比低于此视为空切片


# ---------------------------------------------------------------- 基础工具

def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def load_rgb(path):
    return np.array(Image.open(path).convert("RGB")).astype(np.int16)


def _quant_key(a):
    q = (a >> (8 - QUANT_BITS)).astype(np.int32)
    return (q[:, :, 0] << (2 * QUANT_BITS)) | (q[:, :, 1] << QUANT_BITS) | q[:, :, 2]


def _local_spread(a):
    """3x3 邻域内 (max-min) 的逐通道最大值。无 scipy 依赖。

    ⚠ 必须**按通道分别**求邻域极值，最后才跨通道取最大。
    若先跨通道取极值再求邻域极值，得到的是「单像素内的通道差」——
    纯蓝底 [130,130,240] 会算出 110，于是整张纯色块被判成照片。
    这个写错在单测里立刻暴露（chip 与渐变插画的平坦率都掉到 0.00）。
    """
    h, w = a.shape[:2]
    mx = np.full((h, w, 3), -32768, np.int16)
    mn = np.full((h, w, 3), 32767, np.int16)
    p = np.pad(a, ((1, 1), (1, 1), (0, 0)), mode="edge")
    for dy in range(3):
        for dx in range(3):
            win = p[dy:dy + h, dx:dx + w]
            mx = np.maximum(mx, win)
            mn = np.minimum(mn, win)
    return (mx - mn).max(axis=2)


def _cell_similarity(gray, period, axis):
    """按 period 把灰度切成若干 cell，与首个 cell 比较，返回 0~1 相似度。

    这是「规则阵列」与「碰巧自相关强的不规则形状」的分水岭：
    真实阵列各 cell 近乎相同（相似度 ≥0.9），而五角星/星形这类图形虽然
    投影自相关也强，各 cell 差异很大。
    """
    if period < 3:
        return 0.0
    n = (gray.shape[1] if axis == 0 else gray.shape[0]) // period
    if n < 2:
        return 0.0
    cells = []
    for i in range(n):
        c = gray[:, i * period:(i + 1) * period] if axis == 0 else \
            gray[i * period:(i + 1) * period, :]
        cells.append(c)
    hh = min(c.shape[0] for c in cells)
    ww = min(c.shape[1] for c in cells)
    stack = np.stack([c[:hh, :ww] for c in cells]).astype(np.float64)
    diff = float(np.abs(stack[1:] - stack[0]).mean())
    return _clamp(1.0 - diff / 50.0)


def _best_period(profile, gray, axis):
    """在自相关候选峰里挑「真阵列」周期。

    只取自相关最强的 lag 是不够的：图标内部结构（圆角框+内条）会产生一个
    更短的周期，其自相关比阵列的真实间距还强，于是 cols/rows 全推不出来。
    这里改成**优先选各 cell 确实相同（相似度 ≥0.85）的候选**，
    内部纹理的候选因 cell 差异大而被排除。返回 (ac, lag, sim)。
    """
    p = profile.astype(np.float64)
    n = p.size
    if n < 8:
        return 0.0, 0, 0.0
    p = p - p.mean()
    denom = float((p * p).sum())
    if denom <= 0:
        return 0.0, 0, 0.0
    ac = np.correlate(p, p, mode="full")[n - 1:] / denom
    hi = min(ac.size - 1, n // 2)
    cands = [i for i in range(3, hi)
             if ac[i] >= 0.25 and ac[i] >= ac[i - 1]
             and (i + 1 >= ac.size or ac[i] >= ac[i + 1])]
    if not cands:
        return 0.0, 0, 0.0
    cands = sorted(cands, key=lambda i: -ac[i])[:12]
    scored = [(float(ac[i]), int(i), _cell_similarity(gray, i, axis)) for i in cands]
    good = [s for s in scored if s[2] >= 0.85]
    return max(good or scored, key=lambda s: s[0])


def _bands(profile, frac=0.20):
    """数投影里的连续前景带个数。阵列的行/列数兜底估计。

    只有 2 行的阵列，行方向自相关太弱（样本不足两个完整周期），
    周期法推不出 rows；但「前景带数」直接就是 2。
    """
    p = profile.astype(np.float64)
    if p.size == 0 or p.max() <= 0:
        return 0
    on = p > p.max() * frac
    n, prev = 0, False
    for v in on:
        if v and not prev:
            n += 1
        prev = bool(v)
    return n


def _modal_color(px, bits=4):
    """一批 (N,3) 像素的主色。先量化再取众数格，比取中位数稳。

    ⚠ 占比的分母必须是 px.shape[0]（像素数），不能写 px.size——
    后者把 3 个通道也算进去，占比会凭空除以 3。实测 0.99 被算成 0.33，
    正好掉到「环主色占比 <0.5」的阈值以下，于是所有切片都被误判成铺满。
    """
    if px.size == 0:
        return np.zeros(3), 0.0
    q = (px >> (8 - bits)).astype(np.int32)
    key = (q[:, 0] << (2 * bits)) | (q[:, 1] << bits) | q[:, 2]
    vals, cnts = np.unique(key, return_counts=True)
    k = vals[int(cnts.argmax())]
    sel = key == k
    return np.median(px[sel], axis=0), float(cnts.max()) / float(px.shape[0])


def _bg_color(core):
    """估计背景色，并判断元素是否铺满切片（full-bleed）。

    判据是「1px 边框环的主色」与「四角主色」是否同色，**不是**只看四角。
    原因：切片常把元素四周的余量吃光，此时边框环整圈都是元素自己的颜色
    （胶囊的描边、色带的渐变边），而四角可能残留背景色。
    只看四角会误判成「背景 = 四角色」，于是浅色元素相对浅背景的色差不够阈值，
    前景掩码只剩一圈描边——实测 chip 前景占比掉到 0.097、rect_ratio 0.097、
    手法从 param 跌成 hand。这个洞对「浅色元素 + 浅背景」是致命的：
    整块内容会从掩码里消失。

    返回 (bg, full_bleed)。
    """
    h, w = core.shape[:2]
    k = max(1, min(4, h // 4, w // 4))
    ring = np.concatenate([core[0], core[-1], core[:, 0], core[:, -1]])
    corners = np.concatenate([core[:k, :k].reshape(-1, 3), core[:k, -k:].reshape(-1, 3),
                              core[-k:, :k].reshape(-1, 3), core[-k:, -k:].reshape(-1, 3)])
    ring_c, ring_share = _modal_color(ring)
    cor_c, cor_share = _modal_color(corners)
    if float(np.abs(ring_c - cor_c).max()) > BG_TOL:
        # 边框环与四角不同色：元素吃到边上，看不到背景
        return cor_c, True
    # 两者同色。但若该色占比极低，说明整个切片都没有稳定底色，同样按铺满处理
    if ring_share < 0.5:
        return cor_c, True
    return cor_c, False


def _page_bg(ref_path):
    """从原图四角估整页背景色。用于区分「空切片」与「纯色铺满的切片」。"""
    if not ref_path or not os.path.exists(ref_path):
        return None
    a = load_rgb(ref_path)
    h, w = a.shape[:2]
    k = max(1, min(8, h // 8, w // 8))
    c = np.concatenate([a[:k, :k].reshape(-1, 3), a[:k, -k:].reshape(-1, 3),
                        a[-k:, :k].reshape(-1, 3), a[-k:, -k:].reshape(-1, 3)])
    return _modal_color(c)[0]


# ---------------------------------------------------------------- 特征

def _arrow_geom(mask):
    """水平箭头的几何参数，从**列高度剖面**实测（全部是 core 坐标）。

    箭头 = 细杆 + 三角头，列高剖面是「杆区平坦、头区从 max 线性收到 0」的形状：
      · 杆宽 shaft_w = 杆区列高的中位（不是 max：max 落在头底那一条竖边）
      · 头高 half_h = max(列高) / 2（三角形底边全高 = 2×半高）
      · 头底列 line_end = 从右往左第一个列高 ≥ 0.85×max 的列
      · 尖端列 tip_x = 最右有前景的列

    为什么必须实测：`param_vectors` 的 arrow 缺省 `head_h=4.6` / `line_end=w-14`
    是某项目的值，换图就可能差 2~3 倍。实测混测图 head_h 真值 12，
    用缺省值箭头明显偏小，该元素逐像素 mean 18.4、>60 占 16.2%
    （同口径全位图只要 2.6 —— 也就是矢量化在箭头上是**净损失**，根源就是没量参数）。

    非箭头形状（无前景、或剖面不是「平坦 + 单峰收敛」）返回 None，别硬套。
    """
    col_h = mask.sum(axis=0).astype(float)
    if col_h.size == 0 or col_h.max() <= 0:
        return None
    nz = np.nonzero(col_h)[0]
    x_first, x_last = int(nz[0]), int(nz[-1])
    mx = float(col_h.max())
    if mx < 3 or x_last - x_first < 8:
        return None
    # 杆区 = 非零列里高度 ≤ 0.6×max 的那些（头区才会超过）
    shaft_pool = col_h[(col_h > 0) & (col_h <= 0.6 * mx)]
    if shaft_pool.size == 0:
        return None
    shaft_w = float(np.median(shaft_pool))
    if shaft_w <= 0 or mx < 1.4 * shaft_w:
        return None                      # 头和杆差不多高，不是箭头剖面的形状
    thr = 0.85 * mx
    head_x = x_last
    for x in range(x_last, -1, -1):
        if col_h[x] >= thr:
            head_x = x
            break
    return {"shaft_w": round(shaft_w, 1), "head_h": round(mx / 2.0, 1),
            "line_end": head_x, "tip_x": x_last, "x_first": x_first}


def features(slice_path, pad=2, page_bg=None):
    a = load_rgb(slice_path)
    if pad and a.shape[0] > 2 * pad + 2 and a.shape[1] > 2 * pad + 2:
        core = a[pad:-pad, pad:-pad]
    else:
        core = a
    h, w = core.shape[:2]

    key = _quant_key(core)
    vals, counts = np.unique(key, return_counts=True)
    order = np.argsort(counts)[::-1]
    total = int(counts.sum())
    n_unique = int(vals.size)

    topk = min(4, n_unique)
    topk_ratio = float(counts[order[:topk]].sum()) / total

    p = counts.astype(np.float64) / total
    entropy = float(-(p * np.log2(p)).sum())

    spread = _local_spread(core)
    flat_run_ratio = float((spread <= FLAT_TOL).mean())
    grad_energy = float(spread.mean()) / 255.0

    bg, full_bleed = _bg_color(core)
    if full_bleed:
        mask = np.ones((h, w), bool)
    else:
        mask = np.abs(core - bg).max(axis=2) > BG_TOL
        if mask.mean() < MIN_FILL:
            # 掩码几乎空，两种可能：(a) 真空白切片；(b) 纯色块正好铺满切片、
            # 且其颜色与背景帧色差不到阈值。用整页背景色区分：
            # 与整页背景同色 = 真空白；否则是铺满的纯色块。
            dom, dom_share = _modal_color(core.reshape(-1, 3))
            is_page_bg = (page_bg is not None
                          and float(np.abs(dom - page_bg).max()) <= BG_TOL)
            if dom_share >= 0.5 and not is_page_bg:
                mask = np.ones((h, w), bool)
    fill_ratio = float(mask.mean())

    ys = np.nonzero(mask.any(axis=1))[0]
    xs = np.nonzero(mask.any(axis=0))[0]
    if ys.size and xs.size:
        bh, bw = ys[-1] - ys[0] + 1, xs[-1] - xs[0] + 1
        rect_ratio = float(mask.sum()) / float(bh * bw)
        fg_thin = int(min(bh, bw))
        # 中心用「最宽的那些行/列」求，不要用外接框中心：
        # 箭头这类上下不对称的图形，外接框中心会偏离杆轴（实测偏 1.5px）
        rw, ch = mask.sum(axis=1), mask.sum(axis=0)
        sel_y = np.nonzero(rw >= 0.9 * rw.max())[0]
        sel_x = np.nonzero(ch >= 0.9 * ch.max())[0]
        cy_hint = float(sel_y[0] + (sel_y[-1] - sel_y[0] + 1) / 2.0)
        cx_hint = float(sel_x[0] + (sel_x[-1] - sel_x[0] + 1) / 2.0)
        first_x, first_y = float(xs[0]), float(ys[0])
    else:
        rect_ratio, cy_hint, cx_hint, fg_thin = 0.0, h / 2.0, w / 2.0, min(h, w)
        first_x, first_y = 0.0, 0.0

    gray = core.mean(axis=2)
    per_x, lag_x, sim_x = _best_period(mask.sum(axis=0), gray, 0)
    per_y, lag_y, sim_y = _best_period(mask.sum(axis=1), gray, 1)
    if per_x >= per_y:
        periodicity, period_x, period_y, cell_similarity = per_x, lag_x, 0, sim_x
    else:
        periodicity, period_x, period_y, cell_similarity = per_y, 0, lag_y, sim_y

    # 主色（按量化格的众数代表色）
    cnt_of = dict(zip(vals.tolist(), counts.tolist()))
    palette = []
    for k in vals[order[:8]].tolist():
        rgb = [int(v) for v in np.median(core[key == k], axis=0)]
        palette.append({"hex": "#%02X%02X%02X" % tuple(rgb),
                        "ratio": round(cnt_of[k] / total, 4),
                        "rgb": rgb})

    # 网格阵列行列数：优先用周期反推，推不出就用前景带数兜底
    band_cols = _bands(mask.sum(axis=0))
    band_rows = _bands(mask.sum(axis=1))
    cols = int(round(w / period_x)) if period_x else band_cols
    rows = int(round(h / period_y)) if period_y else band_rows
    if not (2 <= cols <= 40):
        cols = 0
    if not (2 <= rows <= 40):
        rows = 0

    return {
        "size": [w, h],
        "n_unique": n_unique,
        "topk_ratio": round(topk_ratio, 4),
        "entropy": round(entropy, 4),
        "flat_run_ratio": round(flat_run_ratio, 4),
        "grad_energy": round(grad_energy, 5),
        "periodicity": round(periodicity, 4),
        "period_x": period_x,
        "period_y": period_y,
        "cell_similarity": round(cell_similarity, 4),
        "grid_cols": cols if 2 <= cols <= 40 else 0,
        "grid_rows": rows if 2 <= rows <= 40 else 0,
        "fg_thin": fg_thin,
        "fill_ratio": round(fill_ratio, 4),
        "rect_ratio": round(rect_ratio, 4),
        "bg_rgb": [int(v) for v in bg],
        "_palette": palette,
        "cx_hint": round(cx_hint, 1),
        "cy_hint": round(cy_hint, 1),
        "first_x": round(first_x, 1),
        "first_y": round(first_y, 1),
        "arrow": _arrow_geom(mask),
    }


# ---------------------------------------------------------------- 判定

def score(f):
    """返回 (vector_score, raster_score, 明细 dict)。"""
    n_unique = f["n_unique"]
    topk = f["topk_ratio"]
    flat = f["flat_run_ratio"]
    grad = f["grad_energy"]
    ent = f["entropy"]

    v = {
        "色数少": _clamp(1 - n_unique / 64.0),
        "主色集中": _clamp((topk - 0.55) / 0.45),
        "大面积平坦": _clamp((flat - 0.30) / 0.55),
        "梯度低": _clamp(1 - grad / 0.10),
        "颜色熵低": _clamp(1 - ent / ENTROPY_REF),
    }
    r = {
        "色数多": _clamp(n_unique / 200.0),
        "色分布分散": _clamp(1 - (topk - 0.30) / 0.55),
        "纹理细碎": _clamp(1 - (flat - 0.10) / 0.55),
        "梯度高": _clamp(grad / GRAD_REF),
        "颜色熵高": _clamp(ent / ENTROPY_REF),
    }
    wv = {"色数少": .20, "主色集中": .20, "大面积平坦": .25, "梯度低": .20, "颜色熵低": .15}
    wr = {"色数多": .20, "色分布分散": .20, "纹理细碎": .25, "梯度高": .20, "颜色熵高": .15}

    vs = sum(v[k] * wv[k] for k in v)
    rs = sum(r[k] * wr[k] for k in r)
    return vs, rs, {"vector": {k: round(x, 3) for k, x in v.items()},
                    "raster": {k: round(x, 3) for k, x in r.items()}}


def classify(f, margin=0.12):
    """返回 (decision, submode, confidence, reasons, needs_review)。"""
    w, h = f["size"]
    thin = min(w, h)
    aspect = max(w, h) / max(1.0, thin)
    n_unique = f["n_unique"]
    reasons = []

    if f["fill_ratio"] < MIN_FILL:
        return "skip", None, 1.0, ["切片前景占比 %.3f，几乎全是背景，疑似切错" % f["fill_ratio"]], True

    # ---- 硬规则 -------------------------------------------------------
    # 用前景掩码的实际粗细，不要用切片尺寸：切片四周有 pad，
    # 一条 2px 的线切出来是 6px，「min(w,h) ≤ 5」会漏掉，转而命中色带规则。
    if f["fg_thin"] <= 4:
        reasons.append("前景实际粗细 %dpx ≤ 4，属细线/连线段" % f["fg_thin"])
        return "vector", "trace", 0.95, reasons, False

    if aspect >= 5 and n_unique <= 40:
        reasons.append("长宽比 %.1f ≥ 5 且色数 %d ≤ 40，属通栏色带/箭头" % (aspect, n_unique))
        return "vector", "param", 0.92, reasons, False

    if f["periodicity"] >= 0.5 and f["cell_similarity"] >= 0.90 and n_unique <= 32:
        reasons.append("投影自相关 %.2f 且各 cell 相似度 %.2f，属规则阵列（%d×%d）"
                       % (f["periodicity"], f["cell_similarity"],
                          f["grid_cols"], f["grid_rows"]))
        return "vector", "param", 0.90, reasons, False

    if n_unique <= 3 and f["fill_ratio"] > 0.85 and f["rect_ratio"] > 0.85:
        reasons.append("色数 %d、填充率 %.2f、矩形度 %.2f，属纯色矩形/色块"
                       % (n_unique, f["fill_ratio"], f["rect_ratio"]))
        return "vector", "param", 0.90, reasons, False

    # 平滑渐变：多色但没有主色、梯度极低、也不是细长条——
    # param 内置 kind 只有 chip/arrow/bar/grid，表达不了径向或复合渐变，
    # 强行矢量化是「付出重绘成本换回一张看不出差别的图」，不如留位图。
    if (n_unique >= 16 and f["topk_ratio"] < 0.70
            and f["grad_energy"] < 0.03 and aspect < 5):
        reasons.append("色数 %d 但主色仅占 %.2f、梯度仅 %.4f，属平滑渐变："
                       "无规则几何特征，内置 kind 无法表达，矢量化收益低于成本"
                       % (n_unique, f["topk_ratio"], f["grad_energy"]))
        return "raster", None, 0.75, reasons, True

    # ---- 评分 -------------------------------------------------------
    vs, rs, detail = score(f)
    reasons.append("矢量证据 %.3f ｜ 位图证据 %.3f ｜ 差 %.3f（阈值 %.2f）" % (vs, rs, vs - rs, margin))
    if vs >= rs:
        top = sorted(detail["vector"].items(), key=lambda kv: -kv[1])[:2]
        why = "、".join("%s %.2f" % (k, x) for k, x in top)
    else:
        top = sorted(detail["raster"].items(), key=lambda kv: -kv[1])[:2]
        why = "、".join("%s %.2f" % (k, x) for k, x in top)

    if rs - vs >= margin:
        reasons.append("位图证据占优（%s）→ 保守判切片位图，保真度不丢" % why)
        return "raster", None, _clamp((rs - vs) / 0.5 + 0.5, 0.5, 1.0), reasons, False

    if vs - rs >= margin:
        # 手法建议：规整矩形或有「真阵列」证据 → param；否则交给人重绘
        is_array = f["periodicity"] >= 0.5 and f["cell_similarity"] >= 0.90
        submode = "param" if (is_array or f["rect_ratio"] >= 0.85) else "hand"
        reasons.append("矢量证据占优（%s）→ 判矢量重绘，手法建议 %s" % (why, submode))
        return "vector", submode, _clamp((vs - rs) / 0.5 + 0.5, 0.5, 1.0), reasons, False

    reasons.append("两组证据差不足阈值（%s 略占）→ 保守判切片位图，建议人工过一眼" % why)
    return "raster", None, 0.5, reasons, True


# ---------------------------------------------------------------- 主流程

def load_manifest(path):
    m = json.load(open(path, encoding="utf-8"))
    if isinstance(m, dict) and "items" in m:
        return m["items"]
    return m


def rgb_to_hex(rgb):
    return "#%02X%02X%02X" % tuple(int(v) for v in rgb)


def run(args):
    man_path = os.path.abspath(args.manifest)
    root = os.path.dirname(man_path)          # slices 根目录
    items = load_manifest(man_path)

    forced_v = set(x.strip() for x in (args.force_vector or "").split(",") if x.strip())
    forced_r = set(x.strip() for x in (args.force_raster or "").split(",") if x.strip())

    page_bg = _page_bg(args.ref)
    if page_bg is not None:
        print("[i] 整页背景色（原图四角）= #%s" % rgb_to_hex(page_bg))

    prev = {}
    if args.apply_route:
        pm = json.load(open(args.apply_route, encoding="utf-8"))
        prev = {it["file"]: it for it in pm.get("items", [])}
        print("[i] 叠加人工改动：%s（%d 条）" % (os.path.basename(args.apply_route), len(prev)))

    out_items, tally = [], {}
    for it in items:
        f = it["file"]
        slice_path = os.path.join(root, f)
        if not os.path.exists(slice_path):
            print("  ! 切片不存在，跳过：%s" % f)
            continue

        feats = features(slice_path, pad=args.pad, page_bg=page_bg)
        decision, submode, conf, reasons, review = classify(feats, margin=args.min_conf)

        # 人工改动优先
        if f in forced_v:
            decision, submode, conf, review = "vector", submode or "hand", 1.0, False
            reasons.insert(0, "人工指定：强制矢量")
        elif f in forced_r:
            decision, submode, conf, review = "raster", None, 1.0, False
            reasons.insert(0, "人工指定：强制位图")
        elif f in prev and prev[f].get("decision"):
            p = prev[f]
            if p.get("manual"):
                decision, submode = p["decision"], p.get("vector_submode")
                conf, review = p.get("confidence", conf), False
                reasons.insert(0, "沿用上次人工决定")

        rec = {
            "file": f, "box": it.get("box"), "size": it.get("size"),
            "note": it.get("note", ""),
            "decision": decision, "vector_submode": submode,
            "confidence": round(conf, 3), "needs_review": bool(review),
            "reasons": reasons,
            "features": {k: v for k, v in feats.items() if not k.startswith("_")},
            "palette": feats["_palette"],
            "hints": {"cx": feats["cx_hint"], "cy": feats["cy_hint"],
                      "bg_hex": rgb_to_hex(feats["bg_rgb"]),
                      "grid_cols": feats["grid_cols"], "grid_rows": feats["grid_rows"]},
        }
        out_items.append(rec)
        tally[decision] = tally.get(decision, 0) + 1

    # ---- 终端表 ----
    print("\n%-34s %-7s %-7s %5s %s" % ("切片", "判定", "手法", "置信", "依据"))
    print("-" * 108)
    for r in out_items:
        flag = " *" if r["needs_review"] else ""
        print("%-34s %-7s %-7s %5.2f %s%s" % (
            os.path.basename(r["file"])[:34], r["decision"],
            r["vector_submode"] or "-", r["confidence"],
            r["reasons"][-1][:48], flag))
    print("-" * 108)
    print("合计：矢量 %d ｜ 位图 %d ｜ 跳过 %d ｜ 待人工确认 %d  （* 标记）" % (
        tally.get("vector", 0), tally.get("raster", 0), tally.get("skip", 0),
        sum(1 for r in out_items if r["needs_review"])))

    if args.list:
        return 0

    out = args.out or os.path.join(os.path.dirname(man_path), "route.json")
    payload = {
        "source_manifest": os.path.relpath(man_path, os.path.dirname(out)).replace("\\", "/"),
        "ref": args.ref,
        "page_bg": rgb_to_hex(page_bg) if page_bg is not None else None,
        "policy": {"min_conf": args.min_conf, "pad": args.pad},
        "summary": {"total": len(out_items), **tally,
                    "needs_review": sum(1 for r in out_items if r["needs_review"])},
        "items": out_items,
    }
    json.dump(payload, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nroute -> %s" % out)
    return 0


def main():
    ap = argparse.ArgumentParser(description="元素路由判定：矢量重绘 vs 切片位图")
    ap.add_argument("--manifest", required=True, help="slice_by_plan.py 产出的 manifest.json")
    ap.add_argument("--out", default=None, help="输出 route.json（缺省与 manifest 同目录）")
    ap.add_argument("--ref", default=None, help="原图路径，仅用于记录溯源")
    ap.add_argument("--pad", type=int, default=2, help="切片四周的外扩像素，缺省 2（同 slice_by_plan）")
    ap.add_argument("--min-conf", type=float, default=0.12,
                    help="两组证据分的最小差值；调小更倾向判矢量，调大更倾向判位图，缺省 0.12")
    ap.add_argument("--force-vector", default=None, help="逗号分隔的切片名，强制矢量")
    ap.add_argument("--force-raster", default=None, help="逗号分隔的切片名，强制位图")
    ap.add_argument("--apply-route", default=None, help="在已有 route.json 基础上叠加人工改动")
    ap.add_argument("--list", action="store_true", help="只打印判定表，不写文件")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
