# -*- coding: utf-8 -*-
"""统一编排：图片 → 可编辑 PPTX（规则几何走矢量重绘，照片走切片位图）。

一条命令串完 9 个阶段，每阶段产物落盘可查、可单独重跑、可中途人工改：

    background → slice → route → vecspec → vector → layout → build → calibrate → final

用法:
    python build_deck.py --src 原图.png --work work/                 # 跑全部
    python build_deck.py --src 原图.png --work work/ --stage route    # 只跑某阶段
    python build_deck.py --src 原图.png --work work/ --from route --to build
    python build_deck.py --src 原图.png --work work/ --force          # 覆盖已有产物

人工可编辑的中间产物（脚本只给骨架，改完重跑对应阶段）:
    slice-plan.json   切片清单（group/name/box/kind）
    vec-spec.json     参数化矢量 spec（chip 的文字、grid 的行列、配色）
    texts.json        文字清单（必须人工/agent 提供，含 ink_box 与字号）
    tables.json       原生表格（可选）

取舍规则由 scripts/route_elements.py 决定，写进 route.json；要改判就改 route.json
的 decision 字段（再把 needs_review 置 false），或重跑 route 时加 --force-* 。
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SLICER = os.path.abspath(os.path.join(HERE, "..", "..", "image-subimage-slicer", "scripts"))
PY = sys.executable

STAGES = ["background", "slice", "route", "vecspec", "vector",
          "layout", "build", "calibrate", "final"]


def log(msg):
    print("\n\033[1m=== %s ===\033[0m" % msg, flush=True)


def run(cmd, cwd=None, quiet=False):
    cmd = [str(c) for c in cmd]
    if not quiet:
        print("  $", " ".join(os.path.basename(c) for c in cmd[:2]), " ".join(cmd[2:])[:120])
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode:
        print(r.stdout or "")
        print(r.stderr or "")
        raise SystemExit("  步骤失败：%s" % " ".join(cmd[:3]))
    if not quiet:
        for line in (r.stdout or "").splitlines():
            if line.strip():
                print("    " + line)
    return r.stdout or ""


def jload(p, default=None):
    if not os.path.exists(p):
        return default
    return json.load(open(p, encoding="utf-8"))


def jdump(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(obj, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return p


def rel(p, base):
    return os.path.relpath(p, base).replace("\\", "/")


# ---------------------------------------------------------------- 各阶段

def st_background(ctx):
    src = ctx["src"]
    out = ctx["assets"]
    run([PY, os.path.join(HERE, "make_background.py"), "--src", src,
         "--out", out, "--slide", ctx["slide"]])
    for f in ("bg-full.png", "bg.png"):
        if not os.path.exists(os.path.join(out, f)):
            raise SystemExit("  make_background 未产出 %s" % f)


def st_slice(ctx):
    """切分。slice-plan.json 不存在时用 detect_blocks 自动生成骨架（可编辑）。"""
    plan_p = ctx["slice_plan"]
    if not os.path.exists(plan_p) or ctx["force"]:
        blocks_p = os.path.join(ctx["work"], "blocks.json")
        run([PY, os.path.join(SLICER, "detect_blocks.py"), ctx["src"], "--out", ctx["work"]])
        blocks = jload(blocks_p, []) or []
        # detect_blocks 输出 blocks.json（列表）；兼容 {items:[...]} 形式
        if isinstance(blocks, dict):
            blocks = blocks.get("blocks") or blocks.get("items") or []
        items = []
        for i, b in enumerate(sorted(blocks, key=lambda r: (r.get("y1", 0), r.get("x1", 0))), 1):
            box = [b["x1"], b["y1"], b["x2"], b["y2"]] if "x1" in b else b["box"]
            items.append({"name": "%02d_g%03d.png" % (i, i), "group": "01-graphics",
                          "box": list(box), "kind": "graphic",
                          "note": "自动探测块 %d" % i})
        jdump({"source": ctx["src"], "out": ctx["slices"],
               "items": items,
               "exclude_text": True,
               "overview": True, "sheet": False, "zip": False,
               "sections": {"01-graphics": "一、图形元素"},
               "title": "自动生成的切片清单（可编辑）"}, plan_p)
        print("  已生成切片清单骨架：%s（%d 条，建议人工核对/合并）" % (plan_p, len(items)))

    run([PY, os.path.join(SLICER, "slice_by_plan.py"), plan_p,
         "--no-zip", "--clean"], cwd=os.path.dirname(plan_p))
    ctx["manifest"] = os.path.join(ctx["slices"], "manifest.json")


def st_route(ctx):
    man = ctx.get("manifest") or os.path.join(ctx["slices"], "manifest.json")
    if not os.path.exists(man):
        raise SystemExit("  缺 manifest.json，请先跑 slice 阶段")
    run([PY, os.path.join(HERE, "route_elements.py"), "--manifest", man,
         "--ref", ctx["src"], "--out", ctx["route"], "--pad", str(ctx["pad"])])


def _hex2rgb(h):
    return [int(h[i:i + 2], 16) for i in (1, 3, 5)] if h else None


def _hex(rgb):
    return "#%02X%02X%02X" % tuple(int(v) for v in rgb)


BG_TOL_PAL = 10    # 与页面底色差 ≤ 此值 → 判为「背景泄漏」，从元素配色里剔除
MIN_SHARE = 0.01   # 占切片像素比 < 1% → 判为抗锯齿残留，不参与分档


def _pack_palette(entries, bg=None, tol=BG_TOL_PAL, min_share=MIN_SHARE):
    """把实测配色按明度归三档打包成 param_vectors 的 palette。

    最亮 → 填充，居中 → 描边，最暗 → 文字/标签。
    分档依据是排版惯例（浅底、中边、深字），不是色彩学。

    `entries` 是 (rgb, ratio) 序列，ratio 可为 None（表示不做占比筛选）。

    两道筛选缺一不可：
    1. **按页面底色剔背景泄漏**（容差 10）。切片带 pad，四角和圆角处会漏进页面底色。
       容差不能放大 —— 元素自身填充也可能很接近页面底色
       （实测 chip 填充 #EBF3FC vs 页面 #FAFAFC 只差 15 级），放大到 24 会把真填充一起剔掉。
       `bg` 必须是**页面底色**，不能用元素局部 bg：满幅元素（色带）的局部 bg 就是它自己的颜色。
    2. **按占比剔抗锯齿残留**（< 1% 不要）。胶囊/箭头边缘的抗锯齿中间色种类多、占比低
       （实测 chip 有 4 个 0.2~0.3% 的中间色，数量上压过 3.9% 的真描边），
       不筛就会把中间色当成描边（实测 stroke 取到 #A3BAD5，真值 #78A0CD）。
    """
    lst = [(tuple(int(v) for v in rgb), None if ratio is None else float(ratio))
           for rgb, ratio in entries]
    if bg is not None:
        lst = [(c, r) for c, r in lst
               if max(abs(c[i] - bg[i]) for i in range(3)) > tol]
    sig = _sig_palette(entries, bg, tol, min_share)
    if len(sig) < 2:
        # 全局兜底（ratio 全 None）→ 全收；否则回退到占比最高的两个
        sig = lst if (lst and all(r is None for _, r in lst)) \
            else sorted(lst, key=lambda t: -(t[1] or 0.0))[:2]
    cands = sorted({c for c, _ in sig}, key=lambda c: -sum(c))
    if not cands:
        return {}
    light, dark = cands[0], cands[-1]
    mid = cands[len(cands) // 2]
    return {"chip_fill": _hex(light), "chip_stroke": _hex(mid), "chip_text": _hex(dark),
            "arrow_from": _hex(light), "arrow_to": _hex(mid),
            "bar_from": _hex(mid), "bar_mid": _hex(light), "bar_to": _hex(mid),
            "bar_top": "#FFFFFF", "bar_bottom": _hex(mid), "grid_label": _hex(dark)}


def _sig_palette(entries, bg, tol=BG_TOL_PAL, min_share=MIN_SHARE):
    """返回通过两道筛选的 (rgb, ratio)，按占比降序。ratio 为 None 的排在最后。"""
    lst = []
    for rgb, ratio in entries:
        c = tuple(int(v) for v in rgb)
        if bg is not None and max(abs(c[i] - bg[i]) for i in range(3)) <= tol:
            continue
        if ratio is not None and ratio < min_share:
            continue
        lst.append((c, ratio))
    return sorted(lst, key=lambda t: -(t[1] if t[1] is not None else 0.0))


def _chip_stroke_w(it, page_bg_hex):
    """从「描边色像素数 ÷ 轮廓长度」反推胶囊描边宽度（px）。

    胶囊轮廓长 ≈ 2*(W−H) + π*H（两段直边 + 两个半圆）。
    排除页面底色后按占比降序，第 1 名是填充、第 2 名就是描边。

    为什么要估：描边宽度对边缘逐像素差的影响**比描边颜色还大**。
    实测把 chip 描边色从 #A3BAD5 改准成 #78A0CD、宽度仍留脚本缺省 1.8（真值 1.0），
    胶囊区 mean 反而从 15.15 升到 17.60 —— 错色配错宽度"负负得正"。
    """
    box = it.get("box")
    if not box:
        return None
    x1, y1, x2, y2 = (float(v) for v in box)
    W, H = x2 - x1, y2 - y1
    if W <= H or H <= 0:
        return None                 # 非胶囊形（W ≤ H 说明不是横向胶囊），别乱估
    sig = _sig_palette([(p["rgb"], p.get("ratio")) for p in it.get("palette", [])],
                       _hex2rgb(page_bg_hex) if page_bg_hex else None)
    if len(sig) < 2 or sig[1][1] is None:
        return None
    perim = 2.0 * (W - H) + math.pi * H
    px = float(sig[1][1]) * (W * H)
    return round(min(3.0, max(0.75, px / perim)), 2)


def _derive_palette_for(palette, page_bg_hex):
    """从**单个元素**的实测调色板推它的 palette（逐键覆盖用）。

    逐元素推是必须的：param_vectors 的 palette 是全局的，
    若把所有元素加权汇总成一份，色带的浅蓝会串到 chip 上
    （实测 chip 填充被填成 #DDEAFA，而它自己的填充是 #EBF3FC）。
    """
    return _pack_palette([(p["rgb"], p.get("ratio")) for p in (palette or [])],
                         _hex2rgb(page_bg_hex) if page_bg_hex else None)


def _derive_palette_global(R):
    """全局兜底 palette：汇总所有元素的实测配色。

    正常情况下每个 item 都带自己的 palette（见 st_vecspec），
    这份只在某元素测不出配色（palette 为空）时兜底，避免退到脚本默认蓝。
    跨元素汇总时 ratio 不可比，故不做占比筛选。
    """
    allc = []
    for it in R.get("items", []):
        allc += [(p["rgb"], None) for p in it.get("palette", [])]
    return _pack_palette(allc, _hex2rgb(R.get("page_bg")))


def st_vecspec(ctx):
    """从 route.json 生成参数化 spec 骨架 + 细线描摹 config 骨架。"""
    R = jload(ctx["route"])
    if not R:
        raise SystemExit("  缺 route.json，请先跑 route 阶段")
    items_p, traces = [], []
    # route_elements 的 features/hints 全是在 **core**（切掉 pad 之后）上量的，
    # 而 param_vectors 画在**切片画布**（含 pad）上 —— 凡是绝对坐标都要 +pad 才是切片坐标，
    # 否则图形会整体偏 pad 像素（实测 grid 的 x0/y0 与 arrow 的 cy 各偏 2px）。
    # 间距（cell_dx/row_dy）与相对偏移不受影响。
    pad = ctx["pad"]
    for it in R["items"]:
        if it["decision"] != "vector":
            continue
        name = os.path.basename(it["file"]).rsplit(".", 1)[0]
        w, h = it["size"]
        sub = it.get("vector_submode")
        if sub == "param":
            aspect = max(w, h) / max(1, min(w, h))
            gcols, grows = it["hints"].get("grid_cols", 0), it["hints"].get("grid_rows", 0)
            if gcols >= 2 and grows >= 2:
                kind = "grid"
                f = it["features"]
                e = {"kind": "grid", "name": name, "w": w, "h": h,
                     "cols": gcols, "rows": grows,
                     # 间距优先用实测周期；推不出（行数太少、自相关弱）就按等分估
                     "cell_dx": round(float(f["period_x"] or (w / gcols)), 1),
                     "row_dy": round(float(f["period_y"] or (h / grows)), 1),
                     "x0": round(float(f["first_x"]) + pad, 1),
                     "y0": round(float(f["first_y"]) + pad, 1),
                     "_hint": "格子符号由 symbol 字段给出（见下）；未给则用内置 station"}
                # 格子符号：<work>/hand/<名>.cell.svg，内容是局部坐标(0,0 起)的 SVG 片段。
                # 阵列的「规律性」（行列/间距/起点）是实测的，但**格子长什么样**推不出来。
                csp = os.path.join(ctx["hand"], name + ".cell.svg")
                if os.path.exists(csp):
                    e["symbol"] = open(csp, encoding="utf-8").read()
                    print("      grid 格子符号 <- %s" % rel(csp, ctx["work"]))
                else:
                    print("      ! grid 未提供格子符号（%s），将用内置 station —— "
                          "形状不同请照原图重绘一个格子" % rel(csp, ctx["work"]))
                print("  i %s 判为 grid：实测行列 %dx%d、间距 cell_dx=%s row_dy=%s、"
                      "起点 x0=%s y0=%s（切片坐标）"
                      % (os.path.basename(it["file"]), gcols, grows, e["cell_dx"],
                         e["row_dy"], e["x0"], e["y0"]))
            elif aspect >= 5:
                # 又细又长：色带（填充率高）或箭头（填充率低，杆更细）
                kind = "arrow" if it["features"]["fill_ratio"] < 0.5 else "bar"
                e = {"kind": kind, "name": name, "w": w, "h": h}
                if kind == "arrow":
                    e["cy"] = round(float(it["hints"]["cy"]) + pad, 1)
                    # 杆宽/头高/头底列/尖端列全部实测（core 坐标 → 切片坐标要 +pad）。
                    # 缺省值只对某一个项目的箭头成立，换图可能差 2~3 倍。
                    ag = it["features"].get("arrow")
                    if ag:
                        e["shaft_w"] = ag["shaft_w"]
                        e["head_h"] = ag["head_h"]
                        e["line_end"] = round(float(ag["line_end"]) + pad, 1)
                        e["tip_x"] = round(float(ag["tip_x"]) + pad, 1)
                        print("      arrow 实测：杆宽 %s 头高 %s 头底 x=%s 尖端 x=%s"
                              % (ag["shaft_w"], ag["head_h"], e["line_end"], e["tip_x"]))
                    else:
                        print("      ! arrow 剖面不像「细杆+三角头」，几何字段留缺省 —— "
                              "请人工核对 head_h / line_end，或改判 raster")
            else:
                kind = "chip"
                e = {"kind": "chip", "name": name, "w": w, "h": h, "text": ""}
                sw = _chip_stroke_w(it, R.get("page_bg"))
                if sw:
                    e["stroke_w"] = sw
                print("  i %s 判为 chip：复刻场景下标签一般走文字层（大纲已用 --no-text"
                      "剥掉 SVG 里的文字）；只有标签本身就是图形的一部分"
                      "（如设备屏幕里的小字）才需要填 text%s"
                      % (os.path.basename(it["file"]),
                         "；描边宽度实测 %.2f px" % sw if sw else ""))
            e["pad"] = pad
            # 逐元素实测配色：param_vectors 支持 item.palette 覆盖顶层。
            # 剔除背景泄漏要拿**页面底色**比，不能用元素局部 bg_hex ——
            # 满幅元素（色带）的局部 bg 就是它自己的颜色，拿它过滤会把整个元素剔空。
            epal = _derive_palette_for(it.get("palette"), R.get("page_bg"))
            if epal:
                e["palette"] = epal
            if kind == "bar" and it["features"]["fill_ratio"] < 0.5:
                e["_hint"] = ("填充率 %.2f 偏低，可能是箭头；是箭头就改 kind=arrow 并填 cy"
                              % it["features"]["fill_ratio"])
            items_p.append(e)
        elif sub == "trace":
            cfg = os.path.join(ctx["work"], "vec-spec-trace-%s.json" % name)
            jdump({"src": rel(os.path.join(ctx["slices"], it["file"]), ctx["work"]),
                   "out": "vec-svg/%s.svg" % name,
                   "canvas": [w, h],
                   "background": [int(it["hints"]["bg_hex"][i:i + 2], 16)
                                  for i in (1, 3, 5)],
                   "tol": 12, "region": [0, 0, w, h], "exclude": [],
                   "line_width": None, "colors": 4,
                   "simplify": 0.7, "min_len": 6, "drop_faint": 20,
                   "_note": "line_width 留 null 让脚本按 alpha 积分自动测；"
                            "确认后可写死实测值"}, cfg)
            traces.append(cfg)
        elif sub == "hand":
            print("  ! %s 判为 hand（需手工重绘几何 path）：本阶段先保留位图；\n"
                  "    请把重绘好的 SVG 放到 %s，再重跑 vector 阶段（会自动接入）。\n"
                  "    ⚠️ 画布必须等于切片尺寸 %s，坐标用切片坐标（box 相对坐标 + pad=%d）。"
                  % (it["file"], rel(os.path.join(ctx["hand"], name + ".svg"), ctx["work"]),
                     "%dx%d" % (w, h), pad))

    spec_p = ctx["vec_spec"]
    pal = _derive_palette_global(R)
    if items_p or not os.path.exists(spec_p):
        jdump({"out": "vec-svg", "palette": pal, "items": items_p,
               "_note": "本文件由 build_deck.py 从 route.json 生成骨架：每个 item 带自己的"
                        "实测 palette（逐元素推，避免浅色串味），顶层 palette 只在某元素"
                        "测不出配色时兜底。chip 的文字、grid 的行列与图标形状请人工核对后"
                        "再跑 vector"}, spec_p)
        npal = sum(1 for e in items_p if e.get("palette"))
        print("  参数化 spec 已写入：%d 个 item，其中 %d 个带逐元素实测配色；"
              "顶层兜底 palette = %s" % (len(items_p), npal, json.dumps(pal, ensure_ascii=False)))
    # 细线 config 列表，供 vector 阶段用
    jdump({"traces": traces}, os.path.join(ctx["work"], "vec-trace-list.json"))
    print("  参数化元素 %d 个，细线元素 %d 个" % (len(items_p), len(traces)))


def _adopt_hand_svgs(ctx):
    """把人工/agent 重绘好的 hand SVG 接进流水线。

    约定：`<work>/hand/<切片名>.svg` 是 hand 元素的素材源，
    本阶段自动拷进 `<work>/vec-svg/`，之后与参数化产物同等对待（渲染 → 摆放 → 嵌入）。
    以前这步靠手工拷贝，漏拷就会静默回落到位图切片
    （实测漏拷时 05_icon 仍是旧图，且不明显报错）。

    ⚠️ hand SVG 的画布必须等于**切片尺寸**（box + pad），坐标用**切片坐标**。
    画布只写 box 尺寸会被等比放大到切片，整体偏 1~2px。
    """
    R = jload(ctx["route"]) or {}
    hd = ctx["hand"]
    if not os.path.isdir(hd):
        return
    vd = os.path.join(ctx["work"], "vec-svg")
    os.makedirs(vd, exist_ok=True)
    for it in R.get("items", []):
        if it.get("vector_submode") != "hand":
            continue
        name = os.path.basename(it["file"]).rsplit(".", 1)[0]
        src = os.path.join(hd, name + ".svg")
        if not os.path.exists(src):
            print("  ! hand 元素 %s 判为手工重绘，但 %s 不存在 —— 本次回落位图切片"
                  % (name, rel(src, ctx["work"])))
            continue
        dst = os.path.join(vd, name + ".svg")
        shutil.copyfile(src, dst)
        print("  ← hand 素材 %s 已接入" % rel(src, ctx["work"]))


def _check_vec_ink(ctx):
    """矢量产物 vs 原切片：墨迹覆盖率自检。

    为什么必须有：`render_svg_preview.py` 报 OK 只说明「Chromium 渲染没报错」，
    不代表图形真的画出来了。实测 arrow 的杆因渐变退化整条不渲染，
    渲染自检照样是 OK，只有逐像素比对才发现（该元素 mean 18.4，全页最大热点之一）。

    口径：原切片的「墨迹」= 与页面底色差 > 24 的像素；
    矢量 PNG 的「墨迹」= alpha > 128 的像素。两者都是占比，2x 尺寸不影响。
    重绘不是复制，比值不会正好 1；但**显著偏小就是丢东西**。
    """
    R = jload(ctx["route"]) or {}
    pg = _hex2rgb(R.get("page_bg")) if R.get("page_bg") else None
    if pg is None:
        return
    import numpy as np
    from PIL import Image

    vd = os.path.join(ctx["work"], "vec-png")
    bad = []
    print("  墨迹覆盖自检（矢量/原切片，1.0 为等量）：")
    for it in R.get("items", []):
        if it["decision"] != "vector":
            continue
        name = os.path.basename(it["file"]).rsplit(".", 1)[0]
        vp = os.path.join(vd, name + ".png")
        sp = os.path.join(ctx["slices"], it["file"])
        if not (os.path.exists(vp) and os.path.exists(sp)):
            continue
        s = np.array(Image.open(sp).convert("RGB")).astype(np.int16)
        cov_s = float((np.abs(s - np.array(pg)).max(axis=2) > 24).mean())
        v = np.array(Image.open(vp).convert("RGBA"))
        cov_v = float((v[:, :, 3] > 128).mean())
        if cov_s <= 0.002:
            continue                     # 原切片本身几乎空白，比值没意义
        r = cov_v / cov_s
        flag = ""
        if r < 0.6:
            flag = "  ← 疑似漏画！"
            bad.append(name)
        print("    %-22s 矢量 %.4f / 原图 %.4f = %.2f%s" % (name, cov_v, cov_s, r, flag))
    if bad:
        print("  !! %d 个元素墨迹明显偏少：%s —— 渲染没报错不代表画对了，"
              "请查 SVG（常见：渐变 stroke 退化、坐标落在画布外）"
              % (len(bad), ", ".join(bad)))


def st_vector(ctx):
    """跑参数化生成与细线描摹，再把 SVG 渲染成 PNG。"""
    spec_p = ctx["vec_spec"]
    S = jload(spec_p)
    if S and S.get("items"):
        os.makedirs(os.path.join(ctx["work"], "vec-svg"), exist_ok=True)
        # --no-text：复刻场景下文字由原生文本框承载，SVG 里再画一遍会重影
        run([PY, os.path.join(HERE, "param_vectors.py"), spec_p, "--force", "--no-text"],
            cwd=os.path.dirname(spec_p))
    else:
        print("  （无参数化元素，跳过）")

    tl = jload(os.path.join(ctx["work"], "vec-trace-list.json"), {}) or {}
    for cfg in tl.get("traces", []):
        os.makedirs(os.path.join(ctx["work"], "vec-svg"), exist_ok=True)
        run([PY, os.path.join(HERE, "trace_lines.py"), cfg],
            cwd=os.path.dirname(cfg))

    _adopt_hand_svgs(ctx)

    vd = os.path.join(ctx["work"], "vec-svg")
    if not os.path.isdir(vd) or not os.listdir(vd):
        print("  （vec-svg 为空，跳过渲染）")
        return
    # 注意：render_svg_preview 的 --user-data-dir/--screenshot 必须是绝对路径
    run([PY, os.path.join(SLICER, "render_svg_preview.py"), vd,
         "--out", os.path.join(ctx["work"], "vec-png")])
    _check_vec_ink(ctx)


def st_layout(ctx):
    """把背景 + 路由结果 + 文字清单 + 表格组装成 layout.json。"""
    R = jload(ctx["route"])
    if not R:
        raise SystemExit("  缺 route.json，请先跑 route 阶段")
    work = ctx["work"]
    T = jload(ctx["texts"], {"texts": []}) or {"texts": []}
    texts = T.get("texts", [])

    images = []
    for it in R["items"]:
        if it["decision"] == "skip":
            continue
        name = os.path.basename(it["file"]).rsplit(".", 1)[0]
        png = os.path.join(work, "vec-png", name + ".png")
        svg = os.path.join(work, "vec-svg", name + ".svg")
        box = it["box"]
        if it["decision"] == "vector" and os.path.exists(png):
            e = {"path": rel(png, work), "box": box, "route": "vector"}
            if os.path.exists(svg):
                e["svg"] = rel(svg, work)
            images.append(e)
        else:
            if it["decision"] == "vector" and not os.path.exists(png):
                print("  ! %s 判为矢量但没有产物，回落位图切片" % name)
            images.append({"path": rel(os.path.join(ctx["slices"], it["file"]), work),
                           "box": box, "route": "raster"})

    L = {"source": rel(ctx["src"], work),
         "canvas": ctx["canvas"],
         "slide": {"size": "16:9", "w_pt": ctx["w_pt"], "h_pt": ctx["h_pt"],
                   "fit": "width_center"},
         "background": rel(os.path.join(ctx["assets"], "bg-full.png"), work),
         "images": images,
         "texts": texts}
    if os.path.exists(ctx["tables"]):
        L["_tables"] = rel(ctx["tables"], work)
    jdump(L, ctx["layout"])

    nv = sum(1 for i in images if i.get("route") == "vector")
    print("  layout.json：图片 %d（矢量 %d / 位图 %d）｜文字 %d 条"
          % (len(images), nv, len(images) - nv, len(texts)))
    if not texts:
        print("  !! texts.json 为空 —— 文字层还没建。请补 texts.json 后重跑 layout")


def _build(ctx, layout_path, out_pptx, tables=True, embed=True):
    run([PY, os.path.join(HERE, "layout_to_pptx.py"), "--layout", layout_path,
         "--out", out_pptx, "--ea-font", ctx["ea_font"], "--lat-font", ctx["lat_font"]])
    cur = out_pptx
    if tables and os.path.exists(ctx["tables"]):
        nxt = cur.replace(".pptx", "-table.pptx")
        # --tables 按当前工作目录解析（不是按 layout 目录），必须给绝对路径
        run([PY, os.path.join(HERE, "add_tables.py"), "--layout", layout_path,
             "--tables", os.path.abspath(ctx["tables"]),
             "--pptx", cur, "--out", nxt, "--verify"])
        cur = nxt
    if embed:
        n_svg = sum(1 for i in (jload(layout_path, {}) or {}).get("images", []) if i.get("svg"))
        if n_svg:
            nxt = cur.replace(".pptx", "-vec.pptx")
            # --verify 是「只校验」模式，与 --out 互斥：必须先嵌入，再单独校验产物
            run([PY, os.path.join(HERE, "svg_embed.py"), "--pptx", cur,
                 "--layout", layout_path, "--out", nxt])
            run([PY, os.path.join(HERE, "svg_embed.py"), "--pptx", nxt, "--verify"])
            cur = nxt
        else:
            print("  （layout 里没有声明 svg，跳过矢量升格）")
    return cur


def st_build(ctx):
    ctx["deck"] = _build(ctx, ctx["layout"], os.path.join(ctx["work"], "deck.pptx"))


def st_calibrate(ctx):
    deck = ctx.get("deck") or os.path.join(ctx["work"], "deck.pptx")
    if not os.path.exists(deck):
        raise SystemExit("  缺 deck，请先跑 build 阶段")
    cmd = [PY, os.path.join(HERE, "calibrate_layout.py"), "--layout", ctx["layout"],
           "--ref", ctx["src"], "--out-dir", os.path.join(ctx["work"], "calib"),
           "--canvas", ctx["canvas_str"], "--rounds", str(ctx["rounds"])]
    # 校准内部会自行重建并渲染：把表格串进去，避免表格区被当差异
    if os.path.exists(ctx["tables"]):
        cmd += ["--tables", ctx["tables"]]
    else:
        cmd += ["--no-tables"]
    run(cmd, cwd=ctx["work"])
    fixed = os.path.join(ctx["work"], os.path.splitext(os.path.basename(ctx["layout"]))[0] + "-fixed.json")
    ctx["layout_fixed"] = fixed if os.path.exists(fixed) else ctx["layout"]


def st_final(ctx):
    lay = ctx.get("layout_fixed") or os.path.join(ctx["work"], "layout-fixed.json")
    if not os.path.exists(lay):
        lay = ctx["layout"]
    fin = os.path.join(ctx["work"], "final")
    os.makedirs(fin, exist_ok=True)
    deck = _build(ctx, lay, os.path.join(fin, "deck.pptx"))
    print("  最终文件：%s" % deck)
    run([PY, os.path.join(HERE, "render_check.py"), "--pptx", deck, "--ref", ctx["src"],
         "--canvas", ctx["canvas_str"], "--out-dir", fin])
    ctx["final_deck"] = deck


DISPATCH = {"background": st_background, "slice": st_slice, "route": st_route,
            "vecspec": st_vecspec, "vector": st_vector, "layout": st_layout,
            "build": st_build, "calibrate": st_calibrate, "final": st_final}


def main():
    ap = argparse.ArgumentParser(description="图片 → 可编辑 PPTX（混合路由统一编排）")
    ap.add_argument("--src", required=True, help="原图")
    ap.add_argument("--work", default=None, help="工作目录（缺省 <原图目录>/<图名>-work）")
    ap.add_argument("--texts", default=None, help="文字清单 texts.json（缺省 <work>/texts.json）")
    ap.add_argument("--tables", default=None, help="原生表格 tables.json（可选）")
    ap.add_argument("--hand", default=None,
                    help="手工重绘 SVG 目录（缺省 <work>/hand，文件名 = <切片名>.svg）")
    ap.add_argument("--canvas", default=None, help="画布像素尺寸 WxH；缺省取原图尺寸")
    ap.add_argument("--slide", default="960x540", help="幻灯片 pt 尺寸，缺省 960x540")
    ap.add_argument("--stage", default=None, help="只跑一个阶段", choices=STAGES)
    ap.add_argument("--from", dest="from_", default=None, choices=STAGES, help="从该阶段起跑")
    ap.add_argument("--to", dest="to_", default=None, choices=STAGES, help="跑到该阶段止")
    ap.add_argument("--pad", type=int, default=2, help="切片外扩像素，缺省 2")
    ap.add_argument("--rounds", type=int, default=3, help="闭环校准轮数上限，缺省 3")
    ap.add_argument("--ea-font", default="微软雅黑")
    ap.add_argument("--lat-font", default="微软雅黑")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的人工产物骨架")
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    if not os.path.exists(src):
        raise SystemExit("原图不存在：%s" % src)
    from PIL import Image
    with Image.open(src) as im:
        W, H = im.size
    canvas = [int(x) for x in args.canvas.split("x")] if args.canvas else [W, H]
    w_pt, h_pt = [float(x) for x in args.slide.lower().split("x")]

    name = os.path.splitext(os.path.basename(src))[0]
    work = os.path.abspath(args.work or os.path.join(os.path.dirname(src), name + "-work"))
    ctx = {
        "src": src, "work": work, "canvas": canvas,
        "canvas_str": "%dx%d" % (canvas[0], canvas[1]),
        "slices": os.path.join(work, "slices"),
        "assets": os.path.join(work, "assets"),
        "slice_plan": os.path.join(work, "slice-plan.json"),
        "route": os.path.join(work, "route.json"),
        "vec_spec": os.path.join(work, "vec-spec.json"),
        "layout": os.path.join(work, "layout.json"),
        "texts": os.path.abspath(args.texts) if args.texts else os.path.join(work, "texts.json"),
        "tables": os.path.abspath(args.tables) if args.tables else os.path.join(work, "tables.json"),
        "hand": os.path.abspath(args.hand) if args.hand else os.path.join(work, "hand"),
        "slide": args.slide, "w_pt": w_pt, "h_pt": h_pt,
        "pad": args.pad, "rounds": args.rounds, "force": args.force,
        "ea_font": args.ea_font, "lat_font": args.lat_font,
    }
    os.makedirs(work, exist_ok=True)
    # 显式给了 --hand 却不存在的目录 = 写错路径，必须硬报错。
    # 否则 hand 元素会静默回落（icon 回退位图切片、grid 回退内置符号），
    # 表现为「看着跑通了，但 mean 悄悄变高」（实测整页 4.90 → 5.28）。
    if args.hand and not os.path.isdir(ctx["hand"]):
        raise SystemExit(
            "--hand 指向 %s，但该目录不存在。\n"
            "  手工重绘 SVG 的约定位置是 <work>/hand/<切片名>.svg。\n"
            "  确认要手工重绘就建好目录再放素材；不需要就删掉 --hand 用默认路径。"
            % ctx["hand"])
    if not os.path.exists(ctx["texts"]):
        jdump({"texts": [], "_note": "文字层清单：每条 {text, size_pt, bold, color, mode, "
                                     "ink_box|anchor_box}。必须人工/agent 提供，"
                                     "脚本不做 OCR。ink_box 是原图上的墨迹框。"},
              ctx["texts"])

    if args.stage:
        todo = [args.stage]
    else:
        i0 = STAGES.index(args.from_) if args.from_ else 0
        i1 = STAGES.index(args.to_) if args.to_ else len(STAGES) - 1
        if i0 > i1:
            raise SystemExit("--from 不能晚于 --to")
        todo = STAGES[i0:i1 + 1]

    print("原图 %s  %dx%d ｜ 工作目录 %s" % (os.path.basename(src), W, H, work))
    print("阶段：%s" % " → ".join(todo))
    for s in todo:
        log(s)
        DISPATCH[s](ctx)
    print("\n完成。")


if __name__ == "__main__":
    main()
