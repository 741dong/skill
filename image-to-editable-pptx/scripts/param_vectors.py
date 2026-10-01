#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""参数化矢量重绘器 —— 批量生成「规则几何型」图形元素的 SVG。

适用：纯色块 / 圆角胶囊(chip) / 直线箭头 / 通栏色带 / 规则网格阵列。
不适用：有机形状（云、设备外观、插画）—— 那些要手工重绘，见 SKILL.md。

为什么要参数化：同一元素在原图里出现多次、尺寸各异（如两组能力标签共 7 个胶囊），
逐个手写 SVG 既慢又不一致。按尺寸表循环生成，几何、描边、圆角全部一致。

关键：**用绝对坐标**，不要用 transform="scale()" —— scale 会连 stroke-width 一起缩放，
放大后笔画粗细失控。只有 translate() 是安全的。

用法：
    python param_vectors.py spec.json [--out DIR] [--no-text] [--force]

spec.json:
{
  "out": "04-vector",
  "font": "Microsoft YaHei, PingFang SC, Source Han Sans SC, sans-serif",
  "palette": {"chip_fill": "#FCFDFE", "chip_stroke": "#95B6DB", "chip_text": "#1B4F8A"},
  "items": [
    {"kind": "chip",  "name": "07_chip-set1-1_system-compat", "text": "系统兼容",
     "w": 93, "h": 34, "font_size": 15},
    {"kind": "arrow", "name": "14_arrow-a1", "w": 212, "h": 19,
     "line_end": 198, "tip_x": 210},
    {"kind": "bar",   "name": "17_conclusion-bar", "w": 1193, "h": 42},
    {"kind": "grid",  "name": "18_station-grid", "w": 345, "h": 210,
     "cols": 7, "rows": 5, "cell_dx": 48.5, "row_dy": 39,
     "icon": "station", "label": "站车站", "label_rows": [2, 3],
     # 逐元素覆盖配色：优先于顶层 palette，只覆盖写到的键
     "palette": {"grid_label": "#1B4F8A"}}
  ]
}

复刻场景三个额外字段（**都由 build_deck.py 自动填，手工写 spec 时别忘**）：
    "pad"       切片外扩量。切片是「元素框 + pad」，元素不铺满整幅。
                chip/bar 按它内缩；arrow 的杆起点/箭尖按它定位。不填图形就整体偏/胀 pad 像素。
    "stroke_w"  chip 描边宽度（px）。推法见 build_deck.py 的 `_chip_stroke_w`。
                它对边缘逐像素差的影响比描边颜色更大。
⭐ 绝对坐标（x0/y0/cy）要**切片坐标**（core 坐标 + pad）；cell_dx/row_dy 这类间距不用加。

配色优先级：item.palette > spec.palette > DEFAULT_PALETTE（逐键合并）。
复刻整页时务必逐元素给 palette：顶层 palette 是全局的，
把所有元素的实测色汇总成一份会让浅色串味
（实测色带的浅蓝把 chip 填充染成 #DDEAFA，而 chip 自己的填充是 #EBF3FC）。

--no-text 会剥掉全部 <text>，生成「复刻 PPT 专用」版本：
图形归 SVG、文字归 PPT 文本框，两者不重叠。矢量素材交付用带文字版，
复刻 PPT 用 --no-text 版。
"""
import argparse
import json
import os
import sys

DEFAULT_FONT = "Microsoft YaHei, PingFang SC, Source Han Sans SC, sans-serif"
DEFAULT_PALETTE = {
    "chip_fill": "#FCFDFE",
    "chip_stroke": "#95B6DB",
    "chip_text": "#1B4F8A",
    "arrow_from": "#A8C8E8",
    "arrow_to": "#6BA0D6",
    "bar_from": "#D2DEED",
    "bar_mid": "#E6ECF4",
    "bar_to": "#D2DEED",
    "bar_top": "#FFFFFF",
    "bar_bottom": "#B9CBE3",
    "grid_label": "#2C5C8E",
}

HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n'
# 带 xlink 声明：阵列用 <use> 引用符号，PowerPoint 的 SVG 渲染器只认 SVG1.1 的
# xlink:href，纯 SVG2 的 href 会导致符号渲染不出来（Chrome 两者都吃，所以自测发现不了）。
SVG_OPEN = ('<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="http://www.w3.org/1999/xlink" ')


def _svg(w, h, body, title=""):
    t = f"  <title>{title}</title>\n" if title else ""
    return (f'{HEAD}{SVG_OPEN}'
            f'width="{w}" height="{h}" viewBox="0 0 {w} {h}">\n{t}{body}</svg>\n')


def _title(item, default):
    """<title> 可被 spec 的 title 字段覆盖；title: null 则不写。"""
    if "title" in item:
        return item["title"] or ""
    return default


def _num(v):
    """去掉无意义的小数尾巴：48.5 -> 48.5，48.0 -> 48"""
    f = float(v)
    return f"{f:g}"


# ---------------------------------------------------------------- chip

def chip(item, pal, font, with_text=True):
    """圆角胶囊标签。字号经验值 ≈ 胶囊高 × 0.45（×0.5 会顶满边框，视觉发撑）。

    `stroke_w` 可由 spec 覆盖（缺省 1.8）。**复刻场景务必实测**：
    描边宽度对边缘逐像素差的影响比描边颜色还大 ——
    实测把描边色改准、宽度仍留 1.8（真值 1.0）时，胶囊区 mean 反而从 15.2 升到 17.6。
    估法见 build_deck.py 的 `_chip_stroke_w`。

    `pad`（缺省 0）是切片外扩量。**复刻场景必须填**：切片通常是「元素框 + pad」，
    元素并不铺满整幅 —— 不填就会把胶囊画到切片边缘，
    实测 chip 顶/底各多出 2 行、左右各多出 2 列，胶囊区一半的误差来自这里。
    """
    w, h = float(item["w"]), float(item["h"])
    sw = float(item.get("stroke_w", 1.8))
    pad = float(item.get("pad", 0.0))
    x = pad + sw / 2
    y = pad + sw / 2
    rw = w - 2 * pad - sw
    rh = h - 2 * pad - sw
    rx = rh / 2
    body = (f'  <rect x="{_num(x)}" y="{_num(y)}" '
            f'width="{_num(rw)}" height="{_num(rh)}" rx="{_num(rx)}"\n'
            f'        fill="{pal["chip_fill"]}" stroke="{pal["chip_stroke"]}" '
            f'stroke-width="{_num(sw)}"/>\n')
    if with_text and item.get("text"):
        fs = item.get("font_size") or round(h * 0.45, 1)
        body += (
            f'  <text x="{_num(w/2)}" y="{_num(h/2)}" text-anchor="middle" '
            f'dominant-baseline="central"\n'
            f'        font-family="{font}" font-size="{_num(fs)}" font-weight="700" '
            f'fill="{pal["chip_text"]}"\n'
            f'        letter-spacing="0.5">{item["text"]}</text>\n')
    return _svg(w, h, body,
                _title(item, f'能力标签 · {item.get("text","")}'.strip(" ·")))


# --------------------------------------------------------------- arrow

def arrow(item, pal, font, with_text=True):
    """水平链路箭头：渐变线杆 + 三角头。

    注意 cy 默认取 h/2，但原图里箭头常不居中（本项目 cy=8 / h=19），
    顺手把 cy 量出来覆盖，别硬套居中。

    `pad`（缺省 **2**，与最初的构建器一致）是切片外扩量：杆起点在 pad、箭头尖在 w−pad。

    ⚠️ 杆宽（`shaft_w`，缺省 2.6）、杆长（`line_end`）、箭头半高（`head_h`，缺省 4.6）
    **都必须从原图量，不能靠缺省**：缺省值是某一个项目的拟合结果，
    换一张图可能差 2~3 倍（实测混测图的 head_h 真值 12，用缺省 4.6 时
    箭头区逐像素 mean 18.4 / >60 占 16.2%，比同口径**全位图**的 2.6 差得多）。
    量法见 route_elements.py 的 `_arrow_geom`（列高剖面）。
    """
    name = item.get("name", "arrow")
    w, h = float(item["w"]), float(item["h"])
    pad = float(item.get("pad", 2.0))
    cy = float(item.get("cy", h / 2.0))
    line_end = float(item.get("line_end", w - pad - 12))
    tip_x = float(item.get("tip_x", w - pad))
    head_h = float(item.get("head_h", 4.6))
    shaft_w = float(item.get("shaft_w", 2.6))
    gid = item.get("gid", "ar-" + name)
    body = (
        f'  <defs>\n'
        # ⚠️ 必须 userSpaceOnUse。默认的 objectBoundingBox 以**路径自己的 bbox** 为单位，
        # 而杆是一条水平线、bbox 高度为 0 → 渐变退化，**杆整条不渲染**
        # （实测：同一 SVG，默认单位下非透明像素 996，改成 userSpaceOnUse 后 7184 ——
        #  差的正是杆的 220×7=1540；此前箭头区 mean 18.4 的主因就是这个）。
        f'    <linearGradient id="{gid}" gradientUnits="userSpaceOnUse" '
        f'x1="0" y1="{_num(cy)}" x2="{_num(w)}" y2="{_num(cy)}">\n'
        f'      <stop offset="0" stop-color="{pal["arrow_from"]}"/>\n'
        f'      <stop offset="1" stop-color="{pal["arrow_to"]}"/>\n'
        f'    </linearGradient>\n'
        f'  </defs>\n'
        f'  <path d="M {_num(pad)} {_num(cy)} L {_num(line_end)} {_num(cy)}" '
        f'stroke="url(#{gid})" stroke-width="{_num(shaft_w)}"\n'
        f'        stroke-linecap="round" fill="none"/>\n'
        f'  <path d="M {_num(line_end-1)} {_num(cy-head_h)} L {_num(tip_x)} {_num(cy)} '
        f'L {_num(line_end-1)} {_num(cy+head_h)} Z"\n'
        f'        fill="{pal["arrow_to"]}"/>\n')
    return _svg(w, h, body, _title(item, f'链路箭头 · {name}'))


# ----------------------------------------------------------------- bar

def bar(item, pal, font, with_text=True):
    """通栏结论色带：水平渐变 + 竖向高光，均不含文字。

    `pad`（缺省 0）是切片外扩量，复刻场景必须填 —— 见 `chip` 的同名说明。
    """
    w, h = float(item["w"]), float(item["h"])
    pad = float(item.get("pad", 0.0))
    rw, rh = w - 2 * pad, h - 2 * pad
    body = (
        f'  <defs>\n'
        # 同 arrow：显式 userSpaceOnUse。这里 rect 有面积、默认单位也能work，
        # 但统一写绝对坐标更稳（缩放到别的尺寸时行为可预期）。
        f'    <linearGradient id="cb-h" gradientUnits="userSpaceOnUse" '
        f'x1="0" y1="0" x2="{_num(w)}" y2="0">\n'
        f'      <stop offset="0" stop-color="{pal["bar_from"]}"/>\n'
        f'      <stop offset="0.5" stop-color="{pal["bar_mid"]}"/>\n'
        f'      <stop offset="1" stop-color="{pal["bar_to"]}"/>\n'
        f'    </linearGradient>\n'
        f'    <linearGradient id="cb-v" gradientUnits="userSpaceOnUse" '
        f'x1="0" y1="0" x2="0" y2="{_num(h)}">\n'
        f'      <stop offset="0" stop-color="{pal["bar_top"]}" stop-opacity="0.35"/>\n'
        f'      <stop offset="1" stop-color="{pal["bar_bottom"]}" stop-opacity="0.25"/>\n'
        f'    </linearGradient>\n'
        f'  </defs>\n'
        f'  <rect x="{_num(pad)}" y="{_num(pad)}" width="{_num(rw)}" height="{_num(rh)}" '
        f'fill="url(#cb-h)"/>\n'
        f'  <rect x="{_num(pad)}" y="{_num(pad)}" width="{_num(rw)}" height="{_num(rh)}" '
        f'fill="url(#cb-v)"/>\n')
    return _svg(w, h, body, _title(item, '底部结论色带（不含文字）'))


# ---------------------------------------------------------------- grid

# 内置符号：站务系统「车站」图标（按原图形态重绘）
GRID_SYMBOLS = {
    "station": (
        '      <path d="M 19.5 1.2 L 21.8 5.4 L 17.2 5.4 Z" fill="#3E81C2"/>\n'
        '      <path d="M 4 10.4 Q 19.5 2.6 35 10.4 Q 19.5 6.6 4 10.4 Z" fill="#5E9BD6"/>\n'
        '      <rect x="10.5" y="8" width="18" height="7" fill="#7FB4E4"/>\n'
        '      <rect x="12.6" y="10" width="3.8" height="3.6" fill="#EAF3FC"/>\n'
        '      <rect x="17.6" y="10" width="3.8" height="3.6" fill="#EAF3FC"/>\n'
        '      <rect x="22.6" y="10" width="3.8" height="3.6" fill="#EAF3FC"/>\n'
        '      <path d="M 0 19.2 Q 19.5 11.2 39 19.2 Q 19.5 15.3 0 19.2 Z" fill="#3E81C2"/>\n'
        '      <rect x="6.5" y="17.4" width="26" height="7.6" fill="#4E90CB"/>\n'
        '      <rect x="17" y="19.4" width="5" height="5.6" fill="#D6E8F8"/>\n'
        '      <rect x="10" y="19.4" width="3.4" height="5.6" fill="#BBD8F1"/>\n'
        '      <rect x="25.6" y="19.4" width="3.4" height="5.6" fill="#BBD8F1"/>\n'
        '      <rect x="3.5" y="24.6" width="32" height="3.4" rx="1" fill="#2A629C"/>\n'
    ),
}


def grid(item, pal, font, with_text=True):
    """规则网格阵列：同一图标按 cell_dx / row_dy 平铺，指定行带文字标签。

    `symbol`（一段 SVG 片段字符串）可覆盖内置符号；给了就用它，否则按 `icon` 查表。
    **格子形状推不出来，必须人工给**：内置只有 `station` 一个示例符号，
    形状不同时硬套内置货会把整块阵列画错
    （实测混测图的圆角方块格子套 station：墨迹覆盖只有原图的 0.34 倍、
     该元素逐像素 mean 18.7 / >60 占 10.5%，是全页最大热点）。
    做法：照原图把**一个格子**重绘成局部坐标（0,0 起）的 SVG 片段，
    写进 `<work>/hand/<切片名>.cell.svg`，build_deck 会自动读进来。
    """
    w, h = float(item["w"]), float(item["h"])
    icon = item.get("symbol") or GRID_SYMBOLS[item.get("icon", "station")]
    cols, rows = int(item["cols"]), int(item["rows"])
    cell_dx = float(item.get("cell_dx", 48.5))
    row_dy = float(item.get("row_dy", 39))
    x0, y0 = float(item.get("x0", 4)), float(item.get("y0", 6))
    label = item.get("label", "")
    label_rows = set(item.get("label_rows", [])) if with_text else set()
    label_dx = float(item.get("label_dx", 19.5))
    label_dy = float(item.get("label_dy", 36))
    label_fs = float(item.get("label_font_size", 9))

    body = [f'  <defs>\n    <g id="st">\n{icon}    </g>\n  </defs>\n']
    for r in range(1, rows + 1):
        y = y0 + (r - 1) * row_dy
        for c in range(1, cols + 1):
            x = x0 + (c - 1) * cell_dx
            body.append(f'  <use xlink:href="#st" href="#st" '
                        f'x="{_num(x)}" y="{_num(y)}"/>\n')
            if r in label_rows:
                body.append(
                    f'  <text x="{_num(x+label_dx)}" y="{_num(y+label_dy)}" '
                    f'text-anchor="middle"\n'
                    f'        font-family="{font}" font-size="{_num(label_fs)}" '
                    f'font-weight="700"\n'
                    f'        fill="{pal["grid_label"]}">{label}</text>\n')
    return _svg(w, h, ''.join(body),
                _title(item, f'阵列（{cols} 列 x {rows} 行）'))


# ----------------------------------------------------------------------

BUILDERS = {"chip": chip, "arrow": arrow, "bar": bar, "grid": grid}


def strip_text(svg):
    """剥掉所有 <text>…</text>（含跨行），用于生成复刻 PPT 专用版本。"""
    out, i = [], 0
    while True:
        j = svg.find("<text", i)
        if j < 0:
            out.append(svg[i:])
            break
        out.append(svg[i:j])
        k = svg.find("</text>", j)
        if k < 0:
            break
        k = svg.find(">", k) + 1
        i = k + 1 if k < len(svg) and svg[k] == "\n" else k
    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description="参数化生成规则几何型 SVG")
    ap.add_argument("spec", help="spec.json")
    ap.add_argument("--out", default=None, help="覆盖 spec 里的 out")
    ap.add_argument("--no-text", action="store_true",
                    help="剥掉全部文字，生成复刻 PPT 专用版本")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的文件")
    args = ap.parse_args()

    spec_path = os.path.abspath(args.spec)
    spec = json.load(open(spec_path, encoding="utf-8"))
    # out 相对 spec 文件所在目录解析（与其它脚本的路径约定一致），
    # 否则 "out": "." 会写到 cwd，而不是 spec 旁边。
    out = args.out or spec.get("out") or "."
    if not os.path.isabs(out):
        out = os.path.normpath(os.path.join(os.path.dirname(spec_path), out))
    os.makedirs(out, exist_ok=True)
    font = spec.get("font", DEFAULT_FONT)
    pal = dict(DEFAULT_PALETTE)
    pal.update(spec.get("palette", {}))

    written, skipped = [], []
    overridden = 0
    for item in spec.get("items", []):
        kind = item["kind"]
        if kind not in BUILDERS:
            sys.exit(f"未知 kind: {kind}（可用 {sorted(BUILDERS)}）")
        name = item["name"]
        fn = name if name.endswith(".svg") else name + ".svg"
        path = os.path.join(out, fn)
        if os.path.exists(path) and not args.force:
            skipped.append(fn)
            continue
        # 逐元素配色覆盖：只覆盖 item.palette 里写到的键
        item_pal = pal
        if item.get("palette"):
            item_pal = dict(pal)
            item_pal.update(item["palette"])
            overridden += 1
        svg = BUILDERS[kind](item, item_pal, font, with_text=not args.no_text)
        if args.no_text:
            svg = strip_text(svg)
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)
        written.append((fn, len(svg.encode("utf-8"))))

    for fn, sz in written:
        print("  %-46s %6d B" % (fn, sz))
    if skipped:
        print(f"  跳过 {len(skipped)} 个已存在文件（加 --force 覆盖）: {skipped}")
    print(f"共写出 {len(written)} 个 SVG -> {os.path.abspath(out)}"
          + ("  [--no-text 复刻版]" if args.no_text else "")
          + (f"  其中 {overridden} 个用了逐元素 palette" if overridden else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
