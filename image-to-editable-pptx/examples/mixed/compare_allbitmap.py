# -*- coding: utf-8 -*-
"""对照实验：同一张图，「全部走位图」vs「按路由混合」。

用途：回答「把图形矢量化到底付了多少逐像素代价」。
只报矢量元素自己的差异是不够的 —— 得给一个**同口径的全位图产物**做基线，
否则 10.10 这样的数字没有参照。

做法：读 `work/layout.json`，把所有 `images[].path` 换回**原始切片**、删掉 `svg` 字段，
重新生成一份 PPTX（不动主产物），再交给 measure_regions.py 量。

用法：
    python compare_allbitmap.py --work <work 目录>
    # → <work>/allbitmap/deck.pptx 与 <work>/regions-allbitmap.json
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "scripts")))
sys.stdout.reconfigure(encoding="utf-8")

import build_deck as B            # noqa: E402
import measure_regions as M       # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="全位图 vs 混合路由 对照")
    ap.add_argument("--work", required=True)
    ap.add_argument("--ea-font", default="微软雅黑")
    ap.add_argument("--lat-font", default="微软雅黑")
    args = ap.parse_args()

    work = os.path.abspath(args.work)
    R = json.load(open(os.path.join(work, "route.json"), encoding="utf-8"))
    L = json.load(open(os.path.join(work, "layout.json"), encoding="utf-8"))

    # layout.images 的顺序 = route.items 里非 skip 的顺序（见 build_deck.st_layout）
    iters = [i for i in R["items"] if i["decision"] != "skip"]
    if len(iters) != len(L["images"]):
        raise SystemExit("layout.images(%d) 与 route.items 非 skip(%d) 数量不一致，"
                         "顺序假设不成立，别硬对齐" % (len(L["images"]), len(iters)))
    for im, it in zip(L["images"], iters):
        im["path"] = os.path.relpath(os.path.join(work, "slices", it["file"]),
                                     work).replace("\\", "/")
        im.pop("svg", None)
        im["route"] = "raster"

    lay = os.path.join(work, "allbitmap-layout.json")
    B.jdump(L, lay)
    out_dir = os.path.join(work, "allbitmap")
    os.makedirs(out_dir, exist_ok=True)
    ctx = {"tables": os.path.join(work, "tables.json"), "work": work,
           "ea_font": args.ea_font, "lat_font": args.lat_font}
    deck = B._build(ctx, lay, os.path.join(out_dir, "deck.pptx"),
                    tables=True, embed=False)
    print("\n全位图产物：%s" % deck)

    d = os.path.dirname(deck)
    print("\n" + "=" * 74)
    print("【全位图】不做路由，所有图形按原切片贴回")
    print("=" * 74)
    rows_a, whole_a, grp_a, _ = M.measure(work, deck)
    _report(rows_a, whole_a, grp_a)

    vec = os.path.join(work, "final", "deck-table-vec.pptx")
    if os.path.exists(vec):
        print("\n" + "=" * 74)
        print("【混合路由】规则几何走矢量，照片走位图")
        print("=" * 74)
        rows_b, whole_b, grp_b, _ = M.measure(work, vec)
        _report(rows_b, whole_b, grp_b)

        print("\n" + "=" * 74)
        print("差额（混合 − 全位图；正 = 矢量版更差，负 = 矢量版更好）")
        print("=" * 74)
        ga = {r["file"]: r for r in rows_a}
        for r in rows_b:
            a = ga.get(r["file"])
            if a and r["decision"] == "vector":
                print("  %-22s mean %6.2f → %6.2f (%+6.2f)   >60 %5.2f%% → %5.2f%% (%+5.2fpp)"
                      % (r["file"], a["mean"], r["mean"], r["mean"] - a["mean"],
                         a["p60"], r["p60"], r["p60"] - a["p60"]))
        print("  %-22s mean %6.2f → %6.2f (%+6.2f)   >60 %5.2f%% → %5.2f%% (%+5.2fpp)"
              % ("整页", whole_a["mean"], whole_b["mean"],
                 whole_b["mean"] - whole_a["mean"],
                 whole_a["p60"], whole_b["p60"], whole_b["p60"] - whole_a["p60"]))

        json.dump({"allbitmap": {"regions": rows_a, "whole": whole_a},
                   "mixed": {"regions": rows_b, "whole": whole_b}},
                  open(os.path.join(work, "..", "compare-allbitmap.json"), "w",
                       encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0


def _report(rows, whole, grp):
    print("%-22s %-7s %-6s %8s %8s" % ("切片", "判定", "手法", "mean", ">60%"))
    print("-" * 60)
    for r in sorted(rows, key=lambda r: (r["decision"], -r["mean"])):
        print("%-22s %-7s %-6s %8.2f %7.2f%%"
              % (r["file"], r["decision"], r["submode"], r["mean"], r["p60"]))
    print("-" * 60)
    for label, pred in (("位图", lambda r: r["decision"] == "raster"),
                        ("矢量", lambda r: r["decision"] == "vector")):
        g = grp(pred)
        if g:
            n, area, m, q = g
            print("  %-6s n=%d  mean=%6.2f  >60=%5.2f%%" % (label, n, m, q))
    print("  %-6s %12s mean=%6.2f  >60=%5.2f%%" % ("整页", "", whole["mean"], whole["p60"]))


if __name__ == "__main__":
    sys.exit(main())
