#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 PPTX 里的位图占位替换成**真矢量 SVG**（PowerPoint 2016+ 的 svgBlip 机制）。

## 为什么要这一步

`python-pptx` 的 `add_picture()` 只吃位图；传 .svg 会直接抛 UnidentifiedImageError。
而 PPT 从 2016 起支持把 SVG 作为图片插入：OOXML 里在 `<a:blip>` 下挂一个
`<asvg:svgBlip>` 扩展指向 SVG part，**同时保留一张 PNG 作为老版本的 fallback**。
本脚本就是往已生成的 PPTX 里补这个扩展——不用重排版面。

结果：图形在 PPT 里是真矢量，可无损缩放、可取消组合改色；在不认 SVG 的旧版
PowerPoint 里自动回落到 PNG，不会开天窗。

## 用法

    # 1) 看看 PPTX 里有哪些图片（拿到 media 名，便于写 map）
    python svg_embed.py --pptx deck.pptx --list

    # 2) 按显式映射替换
    python svg_embed.py --pptx deck.pptx --map map.json --out deck-vector.pptx

    # 3) 按 layout.json 的 images 顺序自动对齐（跳过全页背景）
    python svg_embed.py --pptx deck.pptx --layout layout.json \\
                        --vector-dir 04-vector --out deck-vector.pptx

    # 4) 只校验结构，不改文件
    python svg_embed.py --pptx deck-vector.pptx --verify

map.json:
{
  "items": [
    {"media": "image2.png", "svg": "04-vector/01_cloud-platform.svg"},
    {"media": "image5.png", "svg": "04-vector/07_chip-set1-1_system-compat.svg"}
  ]
}

--layout 模式的对齐规则：`layout.json` 的 `images[]` 顺序就是 `layout_to_pptx.py`
加入图片的顺序，PPTX 里第一张是满页背景图（宽 = 幻灯片宽）→ 自动跳过，
其余按下标与 `images[]` 一一对应。所以**必须用生成该 PPTX 的同一份 layout.json**。

路径均相对 map.json / layout.json 所在目录解析。
"""
import argparse
import json
import os
import shutil
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8")

from lxml import etree
from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.opc.package import Part
from pptx.opc.packuri import PackURI
from pptx.oxml.ns import qn

ASVG_NS = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"
R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
SVG_EXT_URI = "{96DAC541-7B7A-43D3-8B79-37D633B846F1}"
SVG_CT = "image/svg+xml"


# ------------------------------------------------------------------ core

def _get_blip(pic):
    blip = pic._element.blipFill.find(qn("a:blip"))
    if blip is None:
        raise RuntimeError(f"{pic.name}: 找不到 a:blip")
    return blip


def inject_svgblip(pic, rid):
    """在 pic 的 a:blip 下注入 svgBlip 扩展，指向已建好的 rId。

    注意：**不要在每次调用里新建 Part** —— 同一个 partname 建两次，
    保存时 zip 里会出现重复同名条目（包体膨胀且部分阅读器可能报错）。
    part 与 rId 应由调用方按 SVG 路径缓存复用。
    """
    blip = _get_blip(pic)
    extLst = blip.find(qn("a:extLst"))
    if extLst is None:
        extLst = etree.SubElement(blip, qn("a:extLst"))
    for ext in list(extLst.findall(qn("a:ext"))):
        if ext.get("uri") == SVG_EXT_URI:            # 重复注入时先清掉旧的
            extLst.remove(ext)
    ext = etree.SubElement(extLst, qn("a:ext"))
    ext.set("uri", SVG_EXT_URI)
    asvg = etree.SubElement(ext, "{%s}svgBlip" % ASVG_NS, nsmap={"asvg": ASVG_NS})
    asvg.set(R_EMBED, rid)
    return rid


def register_svg(slide_part, package, partname, svg_bytes):
    """建 SVG part 并建立关系，返回 rId。同一 partname 只应调用一次。"""
    part = Part(PackURI(partname), SVG_CT, package, svg_bytes)
    return slide_part.relate_to(part, RT.IMAGE)


def svg_has_text(path):
    s = open(path, encoding="utf-8", errors="ignore").read()
    return "<text" in s


def pictures(slide, prs):
    """按加入顺序返回 PICTURE 类型的 shape。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    return [sh for sh in slide.shapes if sh.shape_type == MSO_SHAPE_TYPE.PICTURE]


def build_pairs_by_layout(layout_path, prs, slide):
    """按 layout.json 的 images 顺序对齐到 PPTX 的图片（跳过满页背景）。"""
    base = os.path.dirname(os.path.abspath(layout_path))
    lay = json.load(open(layout_path, encoding="utf-8"))
    imgs = lay.get("images", [])
    pics = pictures(slide, prs)
    sw = prs.slide_width
    # 背景：第一张、且宽度≈幻灯片宽
    start = 0
    if pics and abs(pics[0].width - sw) <= sw * 0.02:
        start = 1
    usable = pics[start:]
    if len(usable) < len(imgs):
        print(f"  ! PPTX 里可用图片 {len(usable)} 张 < layout.images {len(imgs)} 项，"
              f"只能对齐前 {len(usable)} 项")
    pairs, missing = [], []
    for i, im in enumerate(imgs):
        if i >= len(usable):
            break
        svg = im.get("svg")
        if not svg:
            missing.append((i, im.get("path", "")))
            continue
        pairs.append((usable[i], os.path.normpath(os.path.join(base, svg))))
    if missing:
        print(f"  · {len(missing)} 项未指定 svg，保持位图：{[p for _, p in missing][:4]}"
              + (" …" if len(missing) > 4 else ""))
    return pairs


def media_name(pic, slide):
    """取该图片在包内的媒体文件名（如 image3.png）。走关系链，比 Image.filename 可靠。"""
    blip = _get_blip(pic)
    rid = blip.get(R_EMBED)
    if not rid:
        return None
    rel = slide.part.rels.get(rid)
    if rel is None or rel.is_external:
        return None
    return os.path.basename(str(rel.target_part.partname))


def build_pairs_by_map(map_path, prs, slide):
    """按 map.json 定位图片。

    key 优先按 shape 名匹配（shape 名是排版的语义名，唯一且可读），
    其次按包内 media 文件名（imageN.png）。两者都写的话 shape 名优先。
    """
    base = os.path.dirname(os.path.abspath(map_path))
    mp = json.load(open(map_path, encoding="utf-8"))
    by_shape, by_media = {}, {}
    for it in mp.get("items", []):
        if it.get("shape"):
            by_shape[it["shape"]] = it["svg"]
        if it.get("media"):
            by_media[os.path.basename(it["media"])] = it["svg"]
    pairs = []
    for pic in pictures(slide, prs):
        svg = by_shape.pop(pic.name, None)
        if svg is None:
            fn = media_name(pic, slide)
            if fn and fn in by_media:
                svg = by_media.pop(fn)
        if svg:
            pairs.append((pic, os.path.normpath(os.path.join(base, svg))))
    if by_shape:
        print(f"  ! map 里这些 shape 没在 PPTX 中找到: {sorted(by_shape)[:6]}"
              + (" …" if len(by_shape) > 6 else ""))
    if by_media:
        print(f"  ! map 里这些 media 没在 PPTX 中找到: {sorted(by_media)}")
    return pairs


def list_images(pptx_path):
    prs = Presentation(pptx_path)
    print(f"{pptx_path}\n  幻灯片 {len(prs.slides)} 页；尺寸 "
          f"{prs.slide_width/914400:.2f} x {prs.slide_height/914400:.2f} in")
    for si, slide in enumerate(prs.slides, 1):
        pics = pictures(slide, prs)
        print(f"  -- 第 {si} 页：{len(pics)} 张图片")
        for i, p in enumerate(pics):
            try:
                im = p.image
                tag = f"{im.size[0]}x{im.size[1]}px"
            except Exception as e:
                tag = f"(读不到) {e}"
            full = abs(p.width - prs.slide_width) <= prs.slide_width * 0.02
            print(f"     [{i}] {p.name:24s} {p.width/914400*72:7.1f}x"
                  f"{p.height/914400*72:6.1f}pt  {tag}"
                  f"  media={media_name(p, slide)}"
                  + ("   <- 满页宽，疑似背景" if full else ""))


def verify(pptx_path):
    """解压校验 svgBlip / svg part / 关系三者是否齐全。"""
    print(f"校验 {pptx_path}")
    with zipfile.ZipFile(pptx_path) as z:
        names = z.namelist()
        svgs = [n for n in names if n.lower().endswith(".svg")]
        uniq = sorted(set(svgs))
        dup = len(svgs) - len(uniq)
        ct = z.read("[Content_Types].xml").decode("utf-8")
        n_blip = 0
        slides = [n for n in names if n.startswith("ppt/slides/slide")
                  and n.endswith(".xml")]
        for s in slides:
            n_blip += z.read(s).decode("utf-8").count("<asvg:svgBlip")
        ok_ct = SVG_CT in ct
        print(f"  svg part        : {len(uniq)}  {uniq[:6]}{' …' if len(uniq)>6 else ''}")
        if dup:
            print(f"  ! zip 内有 {dup} 个重复同名条目（part 未复用，包体冗余）")
        print(f"  svgBlip 引用    : {n_blip}")
        print(f"  ContentTypes 注册: {'OK' if ok_ct else '缺失 image/svg+xml'}")
        # 每条 svgBlip 的 rId 必须在对应 rels 里指向 .svg
        bad = []
        for s in slides:
            rn = s.replace("slides/", "slides/_rels/") + ".rels"
            if rn not in names:
                continue
            sxml = z.read(s).decode("utf-8")
            rels = z.read(rn).decode("utf-8")
            import re
            for rid in re.findall(r'<asvg:svgBlip[^>]*r:embed="([^"]+)"', sxml):
                m = re.search(r'Id="%s"[^>]*Target="([^"]+)"' % re.escape(rid), rels)
                if not m or not m.group(1).lower().endswith(".svg"):
                    bad.append((s, rid))
        print(f"  rId -> svg 关系  : {'全部正确' if not bad else '异常 ' + str(bad)}")
        good = bool(uniq) and ok_ct and n_blip > 0 and not bad and not dup
        print(f"  结论: {'PASS' if good else 'FAIL'}")
        return 0 if good else 1


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="把 PPTX 里的位图替换为真矢量 SVG")
    ap.add_argument("--pptx", required=True)
    ap.add_argument("--map", default=None, help="map.json（显式 media -> svg）")
    ap.add_argument("--layout", default=None, help="layout.json（按 images 顺序对齐）")
    ap.add_argument("--vector-dir", default=None,
                    help="配合 --layout：SVG 所在目录（当 layout 项未写 svg 时，"
                         "按切片同名的 .svg 猜测）")
    ap.add_argument("--out", default=None, help="输出 PPTX（默认原地覆盖）")
    ap.add_argument("--list", action="store_true", help="只列出图片")
    ap.add_argument("--verify", action="store_true", help="只校验结构")
    ap.add_argument("--allow-text", action="store_true",
                    help="允许 SVG 内含 <text>（复刻场景通常应剥掉文字，改用 PPT 文本框）")
    args = ap.parse_args()

    if args.list:
        list_images(args.pptx)
        return 0
    if args.verify:
        return verify(args.pptx)
    if not (args.map or args.layout):
        sys.exit("需要 --map 或 --layout 之一（或 --list / --verify）")

    prs = Presentation(args.pptx)
    if len(prs.slides) != 1:
        print(f"  注意：本脚本目前只处理第 1 页（本 PPTX 有 {len(prs.slides)} 页）")
    slide = prs.slides[0]

    if args.layout:
        if args.vector_dir:
            base = os.path.dirname(os.path.abspath(args.layout))
            lay = json.load(open(args.layout, encoding="utf-8"))
            vd = os.path.normpath(os.path.join(base, args.vector_dir))
            for im in lay.get("images", []):
                if not im.get("svg") and im.get("path"):
                    stem = os.path.splitext(os.path.basename(im["path"]))[0]
                    stem = stem[:-7] if stem.endswith("_notext") else stem
                    cand = os.path.join(vd, stem + ".svg")
                    if os.path.exists(cand):
                        im["svg"] = os.path.relpath(cand, base)
        pairs = build_pairs_by_layout(args.layout, prs, slide)
    else:
        pairs = build_pairs_by_map(args.map, prs, slide)

    if not pairs:
        sys.exit("没有可替换的图片，检查 layout/map 是否与 PPTX 对应")

    # 去重：同一个 SVG 只建一个 part、只建一条关系，多处引用复用同一 rId
    cache, n_text, n_skip = {}, 0, 0
    for pic, svg_path in pairs:
        if not os.path.exists(svg_path):
            print(f"  ! 缺失 SVG，跳过: {svg_path}")
            n_skip += 1
            continue
        if svg_has_text(svg_path):
            n_text += 1
            if not args.allow_text:
                print(f"  ! {os.path.basename(svg_path)} 含 <text>，"
                      f"复刻场景建议用 --no-text 版本（加 --allow-text 可强制）")
        key = os.path.normcase(os.path.abspath(svg_path))
        if key not in cache:
            idx = len(cache) + 1
            rid = register_svg(slide.part, prs.part.package,
                               f"/ppt/media/vector{idx:02d}.svg",
                               open(svg_path, "rb").read())
            cache[key] = rid
        rid = cache[key]
        inject_svgblip(pic, rid)
        print(f"  {pic.name:26s} <- {os.path.basename(svg_path):44s} ({rid})")

    out = args.out or args.pptx
    if out == args.pptx:
        shutil.copy2(args.pptx, args.pptx + ".bak")
        print(f"  原文件已备份 -> {os.path.basename(args.pptx)}.bak")
    prs.save(out)
    print(f"\n已写出 {out}（{os.path.getsize(out)} B，{len(cache)} 个矢量 part）"
          + (f"，{n_text} 个 SVG 含文字" if n_text else ""))
    print()
    verify(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
