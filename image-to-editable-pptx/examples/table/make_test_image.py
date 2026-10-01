# -*- coding: utf-8 -*-
"""生成一张「含原生表格的幻灯片截图」，用于验证 add_tables.py 的表格原生化。

刻意包含两种合并：
  · 第 4 行「合计」横向合并 2 列（colspan=2）
  · 第 1~2 行「状态」列纵向合并 2 行（rowspan=2）

用法：
  python make_test_image.py            # 输出 slide-with-table.png（同目录）
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding="utf-8")

W, H = 1600, 900
X = [140, 340, 720, 1100, 1460]          # 列边界 → col_widths = [200,380,380,360]
Y = [240, 320, 415, 510, 605, 700]       # 行边界 → row_heights = [80,95,95,95,95]
GRID = (200, 210, 225)
HDR_FILL = (232, 240, 250)
ZEBRA = (249, 251, 254)
HDR_TEXT = (12, 45, 100)
BODY_TEXT = (32, 38, 50)
FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_B = r"C:\Windows\Fonts\msyhbd.ttc"

ROWS = [
    ["序号", "接口名称", "责任人", "状态"],
    ["1", "站务系统主数据接口", "张工", "进行中"],
    ["2", "扶梯状态上报接口", "李工", None],
    ["3", "客流数据同步接口", "王工", "已完成"],
    ["合计", None, "—", "2 项完成"],
]
# 每格的 (x0,x1,y0,y1) —— 已按合并结果算好
SPAN = {
    (1, 3): (3, 4, 1, 3),        # 状态列，第 1~2 行纵向合并
    (4, 0): (0, 2, 4, 5),        # 合计，第 4 行横向合并 2 列
}


def cell_rect(r, c):
    c0, c1, r0, r1 = SPAN.get((r, c), (c, c + 1, r, r + 1))
    return X[c0], X[c1], Y[r0], Y[r1]


def main():
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    fh = ImageFont.truetype(FONT_B, 22, index=0)
    fb = ImageFont.truetype(FONT, 20, index=0)

    d.text((140, 92), "站务系统接口进展", font=ImageFont.truetype(FONT_B, 40, index=0),
           fill=(20, 40, 80), anchor="lm")

    # 单元格底色
    for r in range(5):
        for c in range(4):
            if r == 1 and c == 3:
                continue
            if r == 2 and c == 3:
                continue                       # 被 (1,3) 的 rowspan 吃掉
            if r == 4 and c == 1:
                continue                       # 被 (4,0) 的 colspan 吃掉
            x0, x1, y0, y1 = cell_rect(r, c)
            if r == 0:
                fill = HDR_FILL
            elif r == 4:
                fill = HDR_FILL
            elif r - 1 in (1,):                # 与 zebra 规则一致：(r-hdr)%2==1
                fill = ZEBRA
            else:
                fill = (255, 255, 255)
            d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=fill)

    # 网格线（跳过合并区内部）
    for i in range(1, len(X) - 1):
        for j in range(len(Y) - 1):
            if j == 4 and i == 1:              # 第 4 行横跨 0~1 列
                continue
            d.line([(X[i], Y[j]), (X[i], Y[j + 1])], fill=GRID, width=1)
    for k in range(1, len(Y) - 1):
        if k == 2:
            d.line([(X[0], Y[k]), (X[3], Y[k])], fill=GRID, width=1)   # 状态列跨行处断开
            d.line([(X[4], Y[k]), (X[4], Y[k])], fill=GRID, width=1)
        else:
            d.line([(X[0], Y[k]), (X[4], Y[k])], fill=GRID, width=1)
    d.rectangle([X[0], Y[0], X[4], Y[5]], outline=GRID, width=1)

    # 单元格文字
    for r in range(5):
        for c in range(4):
            txt = ROWS[r][c]
            if txt is None:
                continue
            x0, x1, y0, y1 = cell_rect(r, c)
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            f = fh if r == 0 else fb
            col = HDR_TEXT if r in (0, 4) else BODY_TEXT
            anchor = "mm"
            if c == 1 and r in (1, 2, 3):      # 名称列左对齐
                anchor = "lm"
                cx = x0 + 22
            d.text((cx, cy), txt, font=f, fill=col, anchor=anchor)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "slide-with-table.png")
    img.save(out)
    print(f"已写出 {out}  {W}x{H}")
    print(f"  col_widths = {[X[i+1]-X[i] for i in range(4)]}")
    print(f"  row_heights = {[Y[i+1]-Y[i] for i in range(5)]}")


if __name__ == "__main__":
    main()
