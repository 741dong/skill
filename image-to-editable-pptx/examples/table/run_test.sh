#!/usr/bin/env bash
# 原生表格（add_tables.py）的可复现自测：从零生成测试图 → 跑完整链路 → 打印验收数字。
#
#   bash run_test.sh
#
# 产出全部落在 ./out/。验收口径（复刻技能的主判据）：
#   逐元素墨迹框 Δ中心 ≤ 3px、Δ宽 ≤ 5px；逐像素(内容区) mean ≤ 12/255、>60 ≤ 5%。
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SK="$(cd "$DIR/../.." && pwd)"
PY="${PY:-C:/Users/AIOT/.workbuddy/binaries/python/envs/default/Scripts/python.exe}"
OUT="$DIR/out"
mkdir -p "$OUT"

echo "### 1) 生成测试图（含 colspan=2 与 rowspan=2 两处合并）"
"$PY" "$DIR/make_test_image.py"

echo
echo "### 2) probe：从原图探网格线（应与设计值一致）"
echo "    设计值 col_widths=[200,380,380,360]  row_heights=[80,95,95,95,95]"
"$PY" "$SK/scripts/add_tables.py" probe --ref "$DIR/slide-with-table.png" --box 140,240,1460,700

echo
echo "### 3) 平滑底图"
"$PY" "$SK/scripts/make_background.py" --src "$DIR/slide-with-table.png" --out "$OUT/assets" --slide 960x540

echo
echo "### 4) 位图版 PPTX（表格区不切片，留空）"
"$PY" "$SK/scripts/layout_to_pptx.py" --layout "$DIR/layout.json" \
      --out "$OUT/deck.pptx" --preview "$OUT/preview.png" --ref "$DIR/slide-with-table.png" | tail -4

echo
echo "### 5) 插入原生表格"
"$PY" "$SK/scripts/add_tables.py" --layout "$DIR/layout.json" --tables "$DIR/tables.json" \
      --pptx "$OUT/deck.pptx" --out "$OUT/deck-table.pptx" --verify

echo
echo "### 6) Spire 真实渲染 + 与原图比像素"
"$PY" "$SK/scripts/render_check.py" --pptx "$OUT/deck-table.pptx" \
      --ref "$DIR/slide-with-table.png" --canvas 1600x900 --out-dir "$OUT" | tail -8

echo
echo "### 7) 逐单元格墨迹框（主判据）"
"$PY" - "$DIR" "$OUT" <<'PYEOF'
import sys
import numpy as np
from PIL import Image
D, O = sys.argv[1], sys.argv[2]
o = np.array(Image.open(D + "/slide-with-table.png").convert("RGB")).astype(int)
r = np.array(Image.open(O + "/render-pptx-norm.png").convert("RGB")).astype(int)

def ink(a, win):
    x1, y1, x2, y2 = win
    s = a[y1:y2, x1:x2]
    m = s.max(axis=2) < 200
    if not m.any():
        return None
    ys, xs = np.nonzero(m)
    return (x1 + xs.min(), y1 + ys.min(), x1 + xs.max() + 1, y1 + ys.max() + 1)

CELLS = [("(0,0) 序号", (146,246,334,314)), ("(0,1) 接口名称", (346,246,714,314)),
         ("(1,1) 站务系统主数据接口", (346,326,714,409)),
         ("(1,3) 进行中[跨2行]", (1106,326,1454,504)),
         ("(3,3) 已完成", (1106,516,1454,599)),
         ("(4,0) 合计[跨2列]", (146,611,714,694)),
         ("(4,3) 2项完成", (1106,611,1454,694))]
print(f"  {'单元格':<24}{'Δcx':>7}{'Δcy':>7}{'Δw':>7}{'Δh':>7}   判定")
wc = ww = 0.0
for name, win in CELLS:
    a, b = ink(o, win), ink(r, win)
    if a is None or b is None:
        print(f"  {name:<24}  !! 未测到墨迹")
        continue
    dcx = (b[0]+b[2])/2 - (a[0]+a[2])/2
    dcy = (b[1]+b[3])/2 - (a[1]+a[3])/2
    dw = (b[2]-b[0]) - (a[2]-a[0])
    dh = (b[3]-b[1]) - (a[3]-a[1])
    wc, ww = max(wc, abs(dcx)), max(ww, abs(dw))
    print(f"  {name:<24}{dcx:>7.1f}{dcy:>7.1f}{dw:>7.1f}{dh:>7.1f}   "
          f"{'合格' if abs(dcx)<=3 and abs(dw)<=5 else '超限'}")
print(f"\n  最差 Δ中心={wc:.1f}px  Δ宽={ww:.1f}px   （口径：Δ中心≤3、Δ宽≤5）")
PYEOF

echo
echo "### 8) 闭环校准（calibrate_layout.py）：未校准锚点 → 渲染回读 → 反解修正"
echo "    预期：第 1 轮量到字体度量偏差（Δcy 约 -2 ~ -4px），第 2 轮收敛到 |Δ中心| ≤ 1px；"
echo "    layout-multi.json 刻意混用 mode=left(ink_box) 与 mode=center(anchor_box) 两类锚点。"
"$PY" "$SK/scripts/calibrate_layout.py" --layout "$DIR/layout-multi.json" \
      --ref "$DIR/slide-with-table.png" --canvas 1600x900 --no-tables \
      --out-dir "$OUT/calib" --fixed-out "$OUT/layout-multi-fixed.json" 2>&1 |
      grep -vE "^载入|^已写出|^  形状|^canvas |^归一化|^渲染输出"

echo
echo "产物：$OUT/deck-table.pptx   对比图：$OUT/compare-render.png"
echo "闭环：$OUT/layout-multi-fixed.json   报告：$OUT/calib/calibration.md"
