# -*- coding: utf-8 -*-
"""闭环校准 —— 把「渲一次 → 量墨迹框偏差 → 反解修正 layout」做成固定脚本。

为什么需要它
------------
`layout_to_pptx.py` 是**开环**定位：它按 `ink_box` 反推文本框位置，用的是 PIL 的
ascender 锚点（`ImageFont.getbbox(text, anchor="la")`）。而 PowerPoint 的段落行度量与
PIL 差约 4px（实测：1600x900 画布、24pt 微软雅黑，系统性偏上 3.5px），于是文字渲染出来
整体位移。这个偏差**与文字内容无关、与画布比例无关**，开环推导不出来，只能渲染回读反解。

本脚本做三件事，循环执行：
  1. 用当前 layout 生成 PPTX（走 `layout_to_pptx.py`，可选再叠 `add_tables.py`）
  2. 用 Spire 渲染 PNG，与原图**同坐标系**逐元素量墨迹框，算 Δ中心 / Δ宽高
  3. 按 `new_anchor = old_anchor - Δ` 反解平移定位锚点，产出下一版 layout

收敛（全部 |Δ中心| ≤ tol）即停，最后一版 layout 是**被下一轮渲染验证过**的那一版。

用法
----
  python calibrate_layout.py --layout layout.json --ref 原图.png --out-dir calib
  python calibrate_layout.py --layout layout.json --ref 原图.png --out-dir calib --rounds 3
  # 带原生表格的链路：修正完 layout 会连带重新插表格再验收
  python calibrate_layout.py --layout layout.json --tables tables.json --ref 原图.png --out-dir calib
  # 只诊断不修正
  python calibrate_layout.py --layout layout.json --ref 原图.png --out-dir calib --measure-only

产物
----
  <layout 同目录>/<stem>-fixed.json    修正后的 layout，可直接喂回 layout_to_pptx.py
  <out-dir>/calibration.md             逐轮偏差报告
  <out-dir>/round<k>/                  每轮中间产物（layout-in.json / deck.pptx / render-*）

注意
----
- 只校准 `texts[]`。`images[]` / `background` 是按 box 直接摆放的，不经过字体度量，无偏移。
- Spire 免费版会在渲染图左上角画一条 eval 提示（实测 y≈18..34）。脚本会自动检测并提示，
  元素落进那条带时用 `--ignore-top` 排除。
- 修正只**平移中心**。宽高（Δw/Δh）只作诊断——`ink_box` 的宽高不参与渲染定位
  （文本框 `word_wrap=False`，起点由左边界决定），改它没有意义。
"""
import argparse
import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from render_check import render_and_norm  # noqa: E402  —— 渲染口径的单一真源

sys.stdout.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------- 小工具

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.stderr.write(r.stdout or "")
        sys.stderr.write(r.stderr or "")
        raise SystemExit(f"命令失败(exit {r.returncode})：{' '.join(os.path.basename(c) for c in cmd[:2])} …")
    return r.stdout


def load_json(p):
    return json.load(open(p, encoding="utf-8"))


def dump_json(obj, path, root=None):
    """写 layout。root 给定时把 background / images[].path 转成绝对路径——中间文件常被
    放到别的目录，而 layout_to_pptx.py 是按 layout 文件所在目录解析相对路径的。"""
    o = json.loads(json.dumps(obj, ensure_ascii=False))
    if root:
        if o.get("background"):
            o["background"] = os.path.normpath(os.path.join(root, o["background"]))
        for im in o.get("images", []):
            im["path"] = os.path.normpath(os.path.join(root, im["path"]))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    json.dump(o, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return path


def anchor_key(t):
    """该元素的可平移定位锚点字段名。"""
    return "anchor_box" if t.get("mode") == "center" else "ink_box"


# ---------------------------------------------------------------- 量测

def win_of(box, W, H, pad):
    x1, y1, x2, y2 = [float(v) for v in box]
    return [max(0, int(round(x1 - pad))), max(0, int(round(y1 - pad))),
            min(W, int(round(x2 + pad))), min(H, int(round(y2 + pad)))]


def ink_of(img, win, thr, ignore_top=0):
    """窗口内暗像素（max(R,G,B) < thr）的墨迹框。

    返回 (box | None, touched, relaxed)：
      touched —— 墨迹贴到窗口边缘，可能被截断（Δ 会失真，建议加大 --pad）
      relaxed —— 默认阈值没找到墨迹，已放宽后命中（说明这是浅色文字）
    """
    x1, y1, x2, y2 = win
    y1 = max(y1, ignore_top)
    if x2 <= x1 or y2 <= y1:
        return None, False, False
    g = img[y1:y2, x1:x2].max(axis=2)
    mask, relaxed = g < thr, False
    if not mask.any():
        t2 = min(220, int(g.min()) + 40)
        if t2 > thr:
            mask, relaxed = g < t2, True
    if not mask.any():
        return None, False, False
    ys, xs = np.nonzero(mask)
    box = (x1 + int(xs.min()), y1 + int(ys.min()),
           x1 + int(xs.max()) + 1, y1 + int(ys.max()) + 1)
    touched = bool(xs.min() == 0 or ys.min() == 0
                   or xs.max() == x2 - x1 - 1 or ys.max() == y2 - y1 - 1)
    return box, touched, relaxed


def detect_watermark(orig, ren, probe=56, thr=40, frac=0.003):
    """扫渲染图顶部，找 Spire eval 提示带。返回 (y0, y1) 或 None。"""
    h = min(probe, orig.shape[0], ren.shape[0])
    if h <= 0:
        return None
    d = np.abs(orig[:h].astype(np.int16) - ren[:h].astype(np.int16)).max(axis=2)
    hits = [y for y in range(h) if (d[y] > thr).mean() > frac]
    return (hits[0], hits[-1] + 1) if hits else None


def measure(orig, ren, texts, thr, pad, ignore_top):
    """逐个 text 量 Δ。返回 rows（每项含 i / 文案 / 锚点字段 / 两个墨迹框 / Δ 四值 / 备注）。"""
    H, W = orig.shape[:2]
    rows = []
    for i, t in enumerate(texts):
        key = anchor_key(t)
        win = win_of(t[key], W, H, pad)
        ob, ot, oz = ink_of(orig, win, thr, ignore_top)
        rb, rt, rz = ink_of(ren, win, thr, ignore_top)
        r = {"i": i, "text": t.get("text", ""), "key": key, "win": win,
             "orig": ob, "ren": rb, "pad": pad, "tol": 0.0}
        notes = []
        if oz:
            notes.append("原图放宽阈值")
        if rz:
            notes.append("渲染放宽阈值")
        if ob is None or rb is None:
            if ob is None:
                notes.append("原图未测到墨迹(锚点可能标错)")
            if rb is None:
                notes.append("渲染未测到墨迹(该元素可能没画出来)")
            r["ok"] = False
            r["note"] = "；".join(notes)
            rows.append(r)
            continue
        dcx = (rb[0] + rb[2]) / 2.0 - (ob[0] + ob[2]) / 2.0
        dcy = (rb[1] + rb[3]) / 2.0 - (ob[1] + ob[3]) / 2.0
        r.update({"ok": True, "dcx": dcx, "dcy": dcy,
                  "dw": (rb[2] - rb[0]) - (ob[2] - ob[0]),
                  "dh": (rb[3] - rb[1]) - (ob[3] - ob[1])})
        if ot:
            notes.append("原图墨迹贴窗口边")
        if rt:
            notes.append("渲染墨迹贴窗口边(Δ可能失真，加大 --pad)")
        r["note"] = "；".join(notes)
        rows.append(r)
    return rows


def apply_correction(layout, rows):
    """new_anchor = old_anchor - Δ：把定位锚点朝偏差的反方向平移。"""
    nxt = json.loads(json.dumps(layout, ensure_ascii=False))
    moved = 0
    for r in rows:
        if not r["ok"]:
            continue
        t = nxt["texts"][r["i"]]
        b = [float(v) for v in t[r["key"]]]
        t[r["key"]] = [round(b[0] - r["dcx"], 1), round(b[1] - r["dcy"], 1),
                       round(b[2] - r["dcx"], 1), round(b[3] - r["dcy"], 1)]
        moved += 1
    return nxt, moved


# ---------------------------------------------------------------- 报告

def fmt_rows(rows, tol):
    out = []
    for r in rows:
        tag = f"{r['i'] + 1:02d}-{r['text'][:12]}"
        if not r["ok"]:
            out.append(f"  {tag:<18} {'—':>7}{'—':>8}{'—':>7}{'—':>7}   {r['note'] or '未测到'}")
            continue
        bad = abs(r["dcx"]) > tol or abs(r["dcy"]) > tol
        out.append(f"  {tag:<18} {r['dcx']:>+7.1f}{r['dcy']:>+8.1f}"
                   f"{r['dw']:>+7.1f}{r['dh']:>+7.1f}   {r['note'] or ('超限' if bad else 'OK')}")
    return "\n".join(out)


def write_report(path, args, history, final_verified_round, wm, final_rows):
    L = []
    L.append("# 闭环校准报告\n")
    L.append(f"- layout：`{os.path.basename(args.layout)}`")
    L.append(f"- 原图：`{os.path.basename(args.ref)}`")
    L.append(f"- 画布：{args.canvas or os.path.basename(args.ref)}　收敛阈值：|Δ中心| ≤ {args.tol}px　轮数上限：{args.rounds}")
    if args.tables:
        L.append(f"- 表格：`{os.path.basename(args.tables)}`")
    if wm:
        L.append(f"- ⚠ 检测到渲染水印带 y={wm[0]}..{wm[1]}（Spire 免费版 eval 提示），"
                 f"元素落进该带需用 `--ignore-top {wm[1]}`")
    L.append("")
    L.append("| 轮次 | 元素 | Δcx | Δcy | Δw | Δh | 状态 |")
    L.append("|---|---|---|---|---|---|---|")
    for h in history:
        for r in h["rows"]:
            tag = f"{r['i'] + 1:02d}-{r['text'][:12]}"
            if not r["ok"]:
                L.append(f"| {h['round']} | {tag} | — | — | — | — | {r['note'] or '未测到'} |")
            else:
                bad = abs(r["dcx"]) > args.tol or abs(r["dcy"]) > args.tol
                L.append(f"| {h['round']} | {tag} | {r['dcx']:+.1f} | {r['dcy']:+.1f} "
                         f"| {r['dw']:+.1f} | {r['dh']:+.1f} | {'超限' if bad else 'OK'} |")
    L.append("")
    if args.measure_only:
        L.append("**结果：仅诊断（`--measure-only`）**——上表就是当前 layout 的偏差，"
                 "未产出修正后的 layout。")
    elif final_verified_round:
        L.append(f"**结果：第 {final_verified_round} 轮收敛**，"
                 f"输出的 layout 由该轮渲染验证通过（全部 |Δ中心| ≤ {args.tol}px）。")
    else:
        L.append(f"**结果：{args.rounds} 轮内未收敛**——输出的 layout 是最后一轮的反解修正，"
                 f"**尚未经渲染验证**。逐项增大 `--rounds` 看它是否震荡；若震荡说明偏差非线性，"
                 f"需人工在 PowerPoint 里微调。")
    if final_rows is not None:
        L.append("\n## 末轮明细\n")
        L.append("```")
        L.append(fmt_rows(final_rows, args.tol))
        L.append("```")
    L.append("\n> Δ = 渲染墨迹框 − 原图墨迹框（中心差为正表示渲染偏右下）。"
             "修正按 `new_anchor = old_anchor − Δ` 反解。Δw/Δh 仅诊断："
             "`ink_box` 宽高不参与渲染定位，改它无效。\n")
    open(path, "w", encoding="utf-8").write("\n".join(L))
    return path


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="闭环校准：渲染回读、反解修正 layout 的文本定位锚点")
    ap.add_argument("--layout", required=True)
    ap.add_argument("--ref", required=True, help="原图（校准基准）")
    ap.add_argument("--out-dir", default="calib")
    ap.add_argument("--rows", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--rounds", type=int, default=3,
                    help="最多跑几轮（每轮 = 生成+渲染+量测）。1 = 只量不验；2 起才有验证意义")
    ap.add_argument("--tol", type=float, default=1.0,
                    help="收敛阈值(px)：全部 |Δ中心| ≤ 此值即停，缺省 1.0")
    ap.add_argument("--canvas", default=None, help="原图尺寸 WxH，缺省取原图实际尺寸")
    ap.add_argument("--thr", type=float, default=200.0, help="墨迹灰度阈值，缺省 200")
    ap.add_argument("--pad", type=int, default=8,
                    help="量测窗口外扩像素，缺省 8（要略大于预期偏移，否则墨迹被截断、Δ 失真）")
    ap.add_argument("--ignore-top", type=int, default=0,
                    help="忽略渲染图顶部 N 行（躲 Spire eval 水印带）")
    ap.add_argument("--ref-top", type=int, default=None)
    ap.add_argument("--tables", default=None, help="tables.json；给了就在每轮重生成后插入原生表格")
    ap.add_argument("--no-tables", action="store_true",
                    help="禁用 tables.json 自动探测（表格已作为位图切片进 images[] 时必须加）")
    ap.add_argument("--measure-only", action="store_true", help="只量不修正（诊断用）")
    ap.add_argument("--fixed-out", default=None,
                    help="修正后 layout 的输出路径，缺省 <layout 同目录>/<stem>-fixed.json")
    args = ap.parse_args()
    if args.rounds < 1:
        raise SystemExit("--rounds 至少为 1")

    lay_abs = os.path.abspath(args.layout)
    lay_root = os.path.dirname(lay_abs)
    fixed_out = args.fixed_out or os.path.join(
        lay_root, os.path.splitext(os.path.basename(lay_abs))[0] + "-fixed.json")
    if os.path.abspath(fixed_out) == lay_abs:
        raise SystemExit("--fixed-out 不能覆盖原 layout")

    want_tables = args.tables
    if want_tables is None and not args.no_tables:
        guess = os.path.join(lay_root, "tables.json")
        if os.path.exists(guess):
            want_tables = guess
            print(f"[i] 检测到同目录 tables.json，自动串入表格重建：{guess}（不想串用 --no-tables）")

    ref_img = Image.open(args.ref).convert("RGB")
    orig = np.array(ref_img).astype(np.int16)
    CW, CH = (tuple(int(v) for v in str(args.canvas).lower().split("x")) if args.canvas
              else ref_img.size)

    cur = load_json(lay_abs)
    if not cur.get("texts"):
        raise SystemExit("layout 里没有 texts[]，没什么可校准的")

    history, final, verified_round, wm = [], None, None, None
    final_rows = None

    for rnd in range(1, args.rounds + 1):
        rd = os.path.join(args.out_dir, f"round{rnd}")
        lay_in = dump_json(cur, os.path.join(rd, "layout-in.json"), root=lay_root)

        deck = os.path.join(rd, "deck.pptx")
        run([sys.executable, os.path.join(HERE, "layout_to_pptx.py"),
             "--layout", lay_in, "--out", deck])
        if want_tables:
            deck_t = os.path.join(rd, "deck-table.pptx")
            run([sys.executable, os.path.join(HERE, "add_tables.py"),
                 "--layout", lay_in, "--tables", want_tables, "--pptx", deck, "--out", deck_t])
            deck = deck_t

        info = render_and_norm(deck, rd, canvas=args.canvas, ref=args.ref, ref_top=args.ref_top)
        top = info["top"]
        ren = np.array(Image.open(info["norm"]).convert("RGB")).astype(np.int16)[top:top + CH]

        if wm is None:
            wm = detect_watermark(orig, ren)
            if wm and (args.ignore_top < wm[1]):
                print(f"[!] 渲染图检测到 eval 水印带 y={wm[0]}..{wm[1]}；"
                      f"元素若落入该带请加 --ignore-top {wm[1]}")

        rows = measure(orig, ren, cur["texts"], args.thr, args.pad, args.ignore_top)
        fine = [r for r in rows if r["ok"]]
        conv = bool(fine) and all(abs(r["dcx"]) <= args.tol and abs(r["dcy"]) <= args.tol
                                  for r in fine)
        history.append({"round": rnd, "rows": rows, "converged": conv})
        final_rows = rows

        print(f"\n=== 第 {rnd}/{args.rounds} 轮　"
              f"锚点={os.path.basename(anchor_key(cur['texts'][0])) if len(cur['texts']) else '-'}"
              f"　deck={os.path.basename(deck)} ===")
        print(f"  {'元素':<18} {'Δcx':>7}{'Δcy':>8}{'Δw':>7}{'Δh':>7}")
        print(fmt_rows(rows, args.tol))
        worst_c = max((max(abs(r['dcx']), abs(r['dcy'])) for r in fine), default=0.0)
        print(f"  → 最大中心偏差 {worst_c:.1f}px　"
              f"{'收敛' if conv else '未收敛'}（阈值 {args.tol}px）")

        if conv:
            final, verified_round = cur, rnd
            break
        if args.measure_only:
            final, verified_round = cur, None
            break
        cur, moved = apply_correction(cur, rows)
        print(f"  → 已反解修正 {moved} 个锚点，进入下一轮验证")

    os.makedirs(args.out_dir, exist_ok=True)
    if final is None:                      # 轮数用尽仍未收敛：输出未验证的最后一版修正
        final, verified_round = cur, None

    # measure-only 绝不写 fixed 文件：它是诊断模式，若拿"未修正的 layout"去覆盖掉上一次
    # 正常校准的产物，等于把成果毁掉（这个坑在实测中被触发过一次）。
    if not args.measure_only:
        same_dir = os.path.dirname(os.path.abspath(fixed_out)) == lay_root
        # layout 里的 background / images[].path 是相对 **layout 文件所在目录** 解析的。
        # 输出换到别的目录时，必须绝对化，否则 layout_to_pptx.py 会按新目录去拼路径。
        dump_json(final, fixed_out, root=None if same_dir else lay_root)
        if not same_dir:
            print(f"[i] 输出目录与 layout 不同，已把 background / images[].path 转为绝对路径")
    rep = write_report(os.path.join(args.out_dir, "calibration.md"),
                       args, history, verified_round, wm, final_rows)

    print(f"\n{'=' * 60}")
    if args.measure_only:
        print("--measure-only：仅诊断，**未产出**修正后的 layout（避免误覆盖已有成果）")
    elif verified_round:
        print(f"已收敛（第 {verified_round} 轮验证通过），修正后 layout：{fixed_out}")
    else:
        print(f"未在 {args.rounds} 轮内收敛；输出的是末轮反解修正（未经渲染验证）：{fixed_out}")
    print(f"报告：{rep}")
    if not args.measure_only:
        print(f"下一步：python layout_to_pptx.py --layout \"{fixed_out}\" --out deck.pptx"
              + (f"　然后 python add_tables.py --layout \"{fixed_out}\" --tables \"{want_tables}\" "
                 f"--pptx deck.pptx --out deck-final.pptx" if want_tables else ""))


if __name__ == "__main__":
    main()
