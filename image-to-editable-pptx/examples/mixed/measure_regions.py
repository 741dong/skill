# -*- coding: utf-8 -*-
"""逐区域量化：把 route.json 的判定结果落到「每类元素各自差多少」上。

整页一个 mean 说明不了混合路由值不值：照片走位图应该几乎无损，
矢量元素会因为「几何参数是推的、配色是实测的」而有偏差 —— 得分开看。

用法：
    python measure_regions.py --work <work 目录> --pptx <产物.pptx>

work 目录里要有 route.json（提供每个元素的 box 与判定）；
原图取 route.json 的 source 字段（相对 work）解析。
渲染+归一化直接 import render_check.render_and_norm，保证与报告同口径。
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "scripts")))
sys.stdout.reconfigure(encoding="utf-8")

from render_check import render_and_norm   # noqa: E402


def measure(work, pptx, canvas=None):
    R = json.load(open(os.path.join(work, "route.json"), encoding="utf-8"))
    # route.json 里原图字段是 ref（绝对路径）；兼容 source（相对 work）
    src = R.get("ref") or R.get("source")
    if not src:
        raise SystemExit("route.json 里既没有 ref 也没有 source，取不到原图")
    if not os.path.isabs(src):
        src = os.path.join(work, src)
    src = os.path.normpath(src)
    info = render_and_norm(pptx, os.path.join(work, "region-measure"), canvas, src)
    CW, CH, top = info["CW"], info["CH"], info["top"]

    o = np.array(Image.open(src).convert("RGB")).astype(np.int16)
    p = np.array(Image.open(info["norm"]).convert("RGB")).astype(np.int16)[top:top + CH]
    # 前置条件：渲染内容区尺寸必须与原图一致，否则区域框会错位
    if p.shape[:2] != o.shape[:2]:
        raise SystemExit("内容区 %s 与原图 %s 尺寸不一致，区域框会错位"
                         % (p.shape[:2], o.shape[:2]))
    D = np.abs(o - p).max(axis=2)

    rows = []
    for it in R["items"]:
        x1, y1, x2, y2 = (int(v) for v in it["box"])
        d = D[y1:y2, x1:x2]
        if d.size == 0:
            continue
        rows.append({
            "file": os.path.basename(it["file"]),
            "decision": it["decision"],
            "submode": it["vector_submode"] or "-",
            "w": x2 - x1, "h": y2 - y1,
            "mean": float(d.mean()),
            "p60": 100.0 * float((d > 60).mean()),
        })

    # 整页
    whole = {"mean": float(D.mean()), "p60": 100.0 * float((D > 60).mean())}

    # 分组小计
    def grp(pred):
        sel = [r for r in rows if pred(r)]
        if not sel:
            return None
        area = sum(r["w"] * r["h"] for r in sel)
        m = sum(r["mean"] * r["w"] * r["h"] for r in sel) / area
        q = sum(r["p60"] * r["w"] * r["h"] for r in sel) / area
        return len(sel), area, m, q

    return rows, whole, grp, info


def main():
    ap = argparse.ArgumentParser(description="逐区域差异量化（混合路由验收）")
    ap.add_argument("--work", required=True)
    ap.add_argument("--pptx", required=True)
    ap.add_argument("--canvas", default=None)
    ap.add_argument("--json", default=None, help="把结果另存为 json")
    args = ap.parse_args()

    rows, whole, grp, info = measure(args.work, args.pptx, args.canvas)

    print("产物 %s" % os.path.basename(args.pptx))
    print("%-22s %-7s %-6s %8s %8s %8s %8s"
          % ("切片", "判定", "手法", "宽", "高", "mean", ">60%"))
    print("-" * 74)
    for r in sorted(rows, key=lambda r: (r["decision"], -r["mean"])):
        print("%-22s %-7s %-6s %8d %8d %8.2f %7.2f%%"
              % (r["file"], r["decision"], r["submode"], r["w"], r["h"], r["mean"], r["p60"]))
    print("-" * 74)

    print("分组小计（按面积加权）:")
    for label, pred in (("位图 raster", lambda r: r["decision"] == "raster"),
                        ("矢量 vector", lambda r: r["decision"] == "vector"),
                        ("  其中 param", lambda r: r["submode"] == "param"),
                        ("  其中 trace", lambda r: r["submode"] == "trace"),
                        ("  其中 hand ", lambda r: r["submode"] == "hand")):
        g = grp(pred)
        if g:
            n, area, m, q = g
            print("  %-14s n=%d  面积=%7d px²  mean=%6.2f  >60=%5.2f%%" % (label, n, area, m, q))
    print("  %-14s %21s mean=%6.2f  >60=%5.2f%%" % ("整页", "", whole["mean"], whole["p60"]))

    if args.json:
        json.dump({"pptx": os.path.abspath(args.pptx), "regions": rows,
                   "whole": whole}, open(args.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("\n已写出 %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
