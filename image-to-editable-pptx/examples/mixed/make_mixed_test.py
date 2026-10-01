# -*- coding: utf-8 -*-
"""造一张混合元素的测试图（规则几何 + 照片类），并生成配套 plan/texts/tables。

产物（默认 examples/mixed/out/）:
    src/slide-mixed.png          测试图 1600x900
    work/slice-plan.json         切片清单（不含表格与文字）
    work/texts.json              文字层清单（ink_box 由实测得出）
    work/tables.json             原生表格
    hand/05_icon.svg             手工重绘的星形图标（演示 hand 路径）

用法: python make_mixed_test.py [--out DIR]
"""
import argparse
import json
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_BD = r"C:\Windows\Fonts\msyhbd.ttc"
W, H = 1600, 900
BG = (250, 250, 252)
RNG = np.random.default_rng(20260929)

# 画布像素 → pt（slide 960pt / 画布 1600px）
PX2PT = 960.0 / W


def pt(p):
    return int(round(p / PX2PT))


# ---------------------------------------------------------------- 元素几何
CHIP = (100, 170, 360, 226)
ARROW = (400, 175, 640, 215)
PHOTO_A = (100, 270, 400, 490)
GRID = (460, 270, 760, 430)
ICON = (800, 170, 950, 320)
PHOTO_B = (1000, 170, 1300, 390)
THIN = (100, 520, 660, 526)
BAND = (100, 680, 1500, 740)
TABLE = (800, 450, 1500, 660)

STAR_R, STAR_r = 70.0, 28.0


def star_points(box, n=0):
    """五角星顶点。n>0 时抬起左侧第 n 个点，做出「手工重绘」与手绘原图的细微差别。"""
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    s = min(x2 - x1, y2 - y1) / 150.0
    pts = []
    for i in range(10):
        ang = math.radians(-90 + i * 36)
        r = (STAR_R if i % 2 == 0 else STAR_r) * s
        px, py = cx + r * math.cos(ang), cy + r * math.sin(ang)
        pts.append((px, py))
    return pts


# ---------------------------------------------------------------- 绘制
def draw_all():
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    arr = np.array(im)

    # --- 实拍照片 A：连续色调 + 多尺度噪点
    a = photo_like(300, 220, (146, 108, 82), seed=1)
    arr[PHOTO_A[1]:PHOTO_A[3], PHOTO_A[0]:PHOTO_A[2]] = a

    # --- 纹理插画 B：冷色 + 高频纹理
    b = photo_like(300, 220, (92, 118, 150), seed=7, noise=18)
    arr[PHOTO_B[1]:PHOTO_B[3], PHOTO_B[0]:PHOTO_B[2]] = b

    im = Image.fromarray(arr)
    d = ImageDraw.Draw(im)

    # --- 胶囊 chip（纯色几何，无内嵌图标）
    d.rounded_rectangle([CHIP[0], CHIP[1], CHIP[2] - 1, CHIP[3] - 1],
                        radius=(CHIP[3] - CHIP[1]) // 2,
                        fill=(235, 243, 252), outline=(120, 160, 205), width=1)

    # --- 箭头：细杆 + 三角头
    x1, y1, x2, y2 = ARROW
    cy = (y1 + y2) // 2
    d.rectangle([x1, cy - 3, x2 - 22, cy + 3], fill=(140, 176, 216))
    d.polygon([(x2 - 22, cy - 12), (x2 - 2, cy), (x2 - 22, cy + 12)], fill=(110, 152, 200))

    # --- 网格阵列 4×2
    gx1, gy1, gx2, gy2 = GRID
    cols, rows = 4, 2
    dx, dy = (gx2 - gx1) / cols, (gy2 - gy1) / rows
    for r in range(rows):
        for c in range(cols):
            cx, cyi = int(gx1 + dx * (c + .5)), int(gy1 + dy * (r + .5))
            d.rounded_rectangle([cx - 19, cyi - 19, cx + 19, cyi + 19], radius=7,
                                fill=(210, 226, 244), outline=(90, 130, 180), width=2)
            d.rectangle([cx - 8, cyi - 3, cx + 8, cyi + 3], fill=(50, 90, 140))
            d.rectangle([cx - 3, cyi - 12, cx + 3, cyi - 7], fill=(90, 130, 180))

    # --- 五角星图标（扁平几何）
    d.polygon(star_points(ICON), fill=(242, 206, 92), outline=(190, 140, 30))

    # --- 2px 细连线
    arr = np.array(im)
    ly = (THIN[1] + THIN[3]) // 2
    arr[ly - 1:ly + 1, THIN[0]:THIN[2]] = (86, 104, 132)
    im = Image.fromarray(arr)
    d = ImageDraw.Draw(im)

    # --- 通栏色带：水平渐变 + 竖向高光
    bw = BAND[2] - BAND[0]
    t = np.linspace(0, 1, bw)
    band = np.stack([np.interp(t, [0, .5, 1], [206, 226, 206]),
                     np.interp(t, [0, .5, 1], [220, 236, 220]),
                     np.interp(t, [0, .5, 1], [238, 246, 238])], axis=1)
    bh = BAND[3] - BAND[1]
    v = np.linspace(16, -20, bh)[:, None, None]
    band = np.clip(band[None, :, :] + v, 0, 255).astype(np.uint8)
    ar2 = np.array(im)
    ar2[BAND[1]:BAND[3], BAND[0]:BAND[2]] = band
    im = Image.fromarray(ar2)
    d = ImageDraw.Draw(im)

    # --- 原生表格（内容交给 tables.json，这里只画格线与底色）
    tx1, ty1, tx2, ty2 = TABLE
    cols_w, rows_h = [140, 340, 220], [48, 54, 54, 54]
    xs = [tx1]
    for c in cols_w:
        xs.append(xs[-1] + c)
    ys = [ty1]
    for r in rows_h:
        ys.append(ys[-1] + r)
    d.rectangle([tx1, ty1, xs[-1], ys[-1]], fill=(255, 255, 255),
                outline=(200, 210, 225))
    for r in range(len(rows_h)):
        if r == 0:
            d.rectangle([tx1, ys[0], xs[-1], ys[1]], fill=(232, 240, 250))
        elif r % 2 == 0:
            d.rectangle([tx1, ys[r], xs[-1], ys[r + 1]], fill=(249, 251, 254))
    for x in xs:
        d.line([x, ty1, x, ys[-1]], fill=(200, 210, 225))
    for y in ys:
        d.line([tx1, y, xs[-1], y], fill=(200, 210, 225))

    # --- 文字层（同时画进原图，位置随后实测）
    ft_title = ImageFont.truetype(FONT_BD, pt(24))
    ft_chip = ImageFont.truetype(FONT, pt(12))
    ft_cap = ImageFont.truetype(FONT, pt(15))
    d.text((112, 68), "混合路由测试", font=ft_title, fill=(20, 40, 80), anchor="la")
    d.text(((CHIP[0] + CHIP[2]) // 2, (CHIP[1] + CHIP[3]) // 2), "系统兼容",
           font=ft_chip, fill=(27, 79, 138), anchor="mm")
    d.text((110, 762), "结论：规则几何走矢量重绘，照片走切片位图。",
           font=ft_cap, fill=(60, 70, 90), anchor="la")

    # --- 表格文字
    ft_cell = ImageFont.truetype(FONT, pt(12))
    ft_head = ImageFont.truetype(FONT_BD, pt(12))
    rows_txt = [("模块", "说明", "状态"),
                ("图标", "规则几何，走矢量重绘", "已矢量"),
                ("照片", "连续色调，走切片位图", "已切片"),
                ("细线", "2px 连线，中心线描摹", "已描摹")]
    for r, row in enumerate(rows_txt):
        yy = (ys[r] + ys[r + 1]) // 2
        f = ft_head if r == 0 else ft_cell
        col = (12, 45, 100) if r == 0 else (32, 38, 50)
        for c, txt in enumerate(row):
            d.text(((xs[c] + xs[c + 1]) // 2, yy), txt, font=f, fill=col, anchor="mm")

    return np.array(im)


def photo_like(w, h, base, seed=1, noise=24):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    ramp = 0.55 + 0.45 * (0.6 * xx / (w - 1) + 0.4 * yy / (h - 1))
    img = np.zeros((h, w, 3), np.float64)
    for c, b in enumerate(base):
        img[:, :, c] = b * ramp
    coarse = rng.normal(0, 1, (max(2, h // 14), max(2, w // 14), 3))
    ci = Image.fromarray(np.clip(coarse * 24 + 128, 0, 255).astype(np.uint8))
    ci = ci.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(3))
    img += (np.array(ci).astype(np.float64) - 128) * 1.25
    img += rng.normal(0, noise, (h, w, 3))
    return np.clip(img, 0, 255).astype(np.uint8)


def measure_ink(arr, win, thr=200):
    x1, y1, x2, y2 = win
    s = arr[y1:y2, x1:x2]
    m = s.max(axis=2) < thr
    if not m.any():
        return None
    ys, xs = np.nonzero(m)
    return [int(x1 + xs.min()), int(y1 + ys.min()),
            int(x1 + xs.max() + 1), int(y1 + ys.max() + 1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    src_dir, work = os.path.join(out, "src"), os.path.join(out, "work")
    # hand 素材必须放在 <work>/hand/ —— build_deck 的 vector 阶段从这里自动接入。
    # 放到 work 外面（如 out/hand）不会报错，但会被静默忽略、回落到位图切片。
    hand = os.path.join(work, "hand")
    for d in (src_dir, work, hand):
        os.makedirs(d, exist_ok=True)

    arr = draw_all()
    png = os.path.join(src_dir, "slide-mixed.png")
    Image.fromarray(arr).save(png)
    print("测试图 ->", png)

    # ---- 切片清单（表格与文字不走切片）
    items = [
        ("01_chip.png", CHIP, "胶囊 chip（规则几何）"),
        ("02_arrow.png", ARROW, "链路箭头（规则几何）"),
        ("03_photo-a.png", PHOTO_A, "实拍照片（应判位图）"),
        ("04_grid.png", GRID, "车站图标阵列 4x2（规则几何）"),
        ("05_icon.png", ICON, "扁平星形图标（需手工重绘）"),
        ("06_photo-b.png", PHOTO_B, "纹理插画（应判位图）"),
        ("07_thinline.png", THIN, "2px 细连线（中心线描摹）"),
        ("08_band.png", BAND, "通栏色带（规则几何）"),
    ]
    json.dump({"source": "../src/slide-mixed.png", "out": "slices",
               "items": [{"name": n, "group": "01-graphics", "box": list(b),
                          "kind": "graphic", "note": note} for n, b, note in items],
               "exclude_text": True, "overview": True, "sheet": False, "zip": False,
               "sections": {"01-graphics": "一、图形元素"},
               "title": "混合路由测试切片清单"},
              open(os.path.join(work, "slice-plan.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # ---- 文字清单（ink_box 实测）
    specs = [("混合路由测试", 24, True, [20, 40, 80], "left", (90, 50, 700, 130)),
             ("系统兼容", 12, False, [27, 79, 138], "center", (110, 180, 350, 220)),
             ("结论：规则几何走矢量重绘，照片走切片位图。", 15, False, [60, 70, 90],
              "left", (100, 748, 1000, 812))]
    texts = []
    for txt, spt, bold, col, mode, win in specs:
        ink = measure_ink(arr, win)
        if ink is None:
            print("  ! 未测到墨迹：%s" % txt)
            continue
        e = {"text": txt, "size_pt": spt, "bold": bold, "color": col, "mode": mode}
        if mode == "center":
            e["anchor_box"] = ink
        else:
            e["ink_box"] = ink
        texts.append(e)
    json.dump({"texts": texts}, open(os.path.join(work, "texts.json"), "w",
                                     encoding="utf-8"), ensure_ascii=False, indent=1)
    print("文字 %d 条：" % len(texts))
    for t in texts:
        print("   %-24s %s = %s" % (t["text"][:22], "anchor_box" if t["mode"] == "center"
                                    else "ink_box",
                                    t.get("anchor_box") or t.get("ink_box")))

    # ---- 原生表格
    json.dump({
        "style": {"font": "微软雅黑", "size_pt": 12, "color": [32, 38, 50],
                  "align": "center", "valign": "middle",
                  "margin_pt": [2, 8, 2, 8], "cell_fill": [255, 255, 255],
                  "header_fill": [232, 240, 250], "header_bold": True,
                  "header_color": [12, 45, 100], "zebra_fill": [249, 251, 254],
                  "border_color": [200, 210, 225], "border_w_pt": 0.75,
                  "border_sides": "LRTB"},
        "tables": [{"name": "混合路由表", "box": list(TABLE),
                    "col_widths": [140, 340, 220], "row_heights": [48, 54, 54, 54],
                    "header_rows": 1,
                    "cells": [[{"text": "模块"}, {"text": "说明"}, {"text": "状态"}],
                              [{"text": "图标"}, {"text": "规则几何，走矢量重绘", "align": "left"},
                               {"text": "已矢量"}],
                              [{"text": "照片"}, {"text": "连续色调，走切片位图", "align": "left"},
                               {"text": "已切片"}],
                              [{"text": "细线"}, {"text": "2px 连线，中心线描摹", "align": "left"},
                               {"text": "已描摹"}]]}]
    }, open(os.path.join(work, "tables.json"), "w", encoding="utf-8"),
        ensure_ascii=False, indent=1)

    # ---- 阵列的「格子符号」（演示 grid 的 symbol 通道）
    # 阵列的行列/间距/起点是脚本实测的，但格子长什么样推不出来 ——
    # 照原图把一个格子重绘成局部坐标(0,0 起)的 SVG 片段即可。
    # ⚠️ 不提供就会退回内置 station 符号，整块阵列画错
    #（实测墨迹覆盖只有原图的 0.34 倍）。
    cell = (
        '      <rect x="1" y="1" width="36" height="36" rx="7" '
        'fill="#D2E2F4" stroke="#5A82B4" stroke-width="2"/>\n'
        '      <rect x="11" y="16" width="17" height="7" fill="#325A8C"/>\n'
        '      <rect x="16" y="7" width="7" height="6" fill="#5A82B4"/>\n')
    open(os.path.join(hand, "04_grid.cell.svg"), "w", encoding="utf-8").write(cell)
    print("格子符号 -> hand/04_grid.cell.svg")

    # ---- 手工重绘的星形 SVG（演示 hand 路径）
    # ⚠️ hand SVG 的画布必须等于**切片尺寸**、坐标用**切片坐标**。
    # 切片是「元素框 + pad」外扩出来的，画布若只写 box 尺寸，
    # 摆放时会被等比放大到切片，整体偏 1~2px
    # （实测 icon 区 mean 36.4，主因就是这个 2px 的画布不匹配）。
    PAD = 2
    x1, y1, x2, y2 = ICON
    w, h = x2 - x1 + 2 * PAD, y2 - y1 + 2 * PAD
    pts = [(round(px - x1 + PAD, 2), round(py - y1 + PAD, 2))
           for px, py in star_points(ICON)]
    dpath = "M " + " L ".join("%s %s" % p for p in pts) + " Z"
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
           'width="%d" height="%d">\n'
           '  <path d="%s" fill="#F2CE5C" stroke="#BE8C1E" stroke-width="1"/>\n'
           '</svg>\n') % (w, h, w, h, dpath)
    open(os.path.join(hand, "05_icon.svg"), "w", encoding="utf-8").write(svg)
    print("手工重绘 SVG -> hand/05_icon.svg  (%dx%d)" % (w, h))


if __name__ == "__main__":
    sys.exit(main())
