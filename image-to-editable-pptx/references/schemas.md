# 各阶段 JSON 字段说明

所有路径都**相对该 JSON 文件所在目录**解析（写成绝对路径也可以）。

---

## 1. `route.json` — 逐元素取舍判定（`route_elements.py`）

整条链路的**决策文件**：后续 `vecspec` / `vector` / `layout` 都按它走。
默认由脚本判定，可以人工改。

```jsonc
{
  "source_manifest": "slices/manifest.json",
  "ref": "…/原图.png",                     // 原图（绝对路径）
  "page_bg": "#FAFAFC",                    // 整页背景色（原图四角）
  "policy": {"min_conf": 0.12, "pad": 2},
  "summary": {"total": 8, "vector": 6, "raster": 2, "needs_review": 0},
  "items": [ /* 见下 */ ]
}
```

### 每个 item

| 字段 | 说明 |
|---|---|
| `file` `box` `size` `note` | 切片路径 / 外扩框（含 pad）/ 切片像素尺寸 / 备注 |
| `decision` | `vector` / `raster` / `skip` —— **改这里就能人工改判** |
| `vector_submode` | `param` / `trace` / `hand`（`decision=vector` 时有效） |
| `confidence` | 判定置信度 0~1 |
| `needs_review` | 打分落在 margin 内或触发不确定规则时置 true；人工确认后置 false |
| `reasons` | 判定依据（字符串数组，最后一条是主因） |
| `features` | 全部实测特征，见下 |
| `palette` | 实测调色板 `[{hex, ratio, rgb}, …]`，按占比降序，最多 8 项 |
| `hints` | `cx` `cy`（轴线中心）、`bg_hex`（元素局部背景）、`grid_cols` `grid_rows` |

### `features`（**全部在 core 坐标上量**）

| 字段 | 说明 |
|---|---|
| `size` | `[w, h]` = core 尺寸 |
| `n_unique` `topk_ratio` `entropy` | 量化色数 / Top-4 色占比 / 颜色熵 |
| `flat_run_ratio` `grad_energy` | 平坦像素占比 / 平均局部梯度（归一） |
| `periodicity` `period_x` `period_y` `cell_similarity` | 自相关周期性 / 周期 / 格子相似度 |
| `grid_cols` `grid_rows` | 阵列行列数（推不出为 0） |
| `fg_thin` `fill_ratio` `rect_ratio` | 前景最小厚度 / 填充率 / 外接框充实度 |
| `bg_rgb` | 元素局部背景色 |
| `cx_hint` `cy_hint` | 轴线中心（最宽行/列的中点，**不是**外接框中心） |
| `first_x` `first_y` | 前景首列/首行 |
| `arrow` | 箭头剖面几何 `{shaft_w, head_h, line_end, tip_x, x_first}`；非箭头剖面为 `null` |

> ⚠️ **`cx`/`cy`/`first_x`/`first_y`/`arrow` 里的列号都是 core 坐标**，
> 写进 spec 时要 `+pad` 才是切片坐标。`period_x`/`period_y` 是间距，不用加。

### 人工覆盖

```bash
# 强制某些切片的判定（结果写进 reasons，可追溯）
route_elements.py --manifest … --ref … --force-vector a.png b.png --force-raster c.png
# 或直接改 route.json 的 decision，把 needs_review 置 false，重跑 --from vecspec
```

---

## 2. `spec.json` — 参数化矢量重绘（`param_vectors.py`）

```jsonc
{
  "out": "04-vector",                       // 输出目录（可被 --out 覆盖）
  "font": "Microsoft YaHei, PingFang SC, Source Han Sans SC, sans-serif",
  "palette": {                              // 可选，覆盖内置配色
    "chip_fill": "#FCFDFE", "chip_stroke": "#95B6DB", "chip_text": "#1B4F8A",
    "arrow_from": "#A8C8E8", "arrow_to": "#6BA0D6",
    "bar_from": "#D2DEED", "bar_mid": "#E6ECF4", "bar_to": "#D2DEED",
    "bar_top": "#FFFFFF", "bar_bottom": "#B9CBE3",
    "grid_label": "#2C5C8E"
  },
  "items": [ /* 见下 */ ]
}
```

### 通用字段（每个 item）

| 字段 | 说明 |
|---|---|
| `kind` | `chip` / `arrow` / `bar` / `grid`，必填 |
| `name` | 输出文件名（可不带 `.svg`），必填 |
| `title` | 覆盖 SVG 的 `<title>`；写 `null` 则不写 |
| `w` `h` | 画布尺寸 = **切片尺寸**（`box + pad`），必填 |
| `pad` | **切片外扩量**。切片是「元素框 + pad」，元素并不铺满整幅 —— 复刻场景必填，不填图形会整体偏/胀 `pad` 像素。`chip`/`bar` 按它内缩，`arrow` 的杆起点/箭尖按它定位 |
| `palette` | **逐元素覆盖配色**，优先于顶层 `palette`，逐键合并。整页复刻时必给：顶层 palette 是全局的，多元素汇总会串色（实测色带的浅蓝把 chip 填充染成 `#DDEAFA`，而它自己的填充是 `#EBF3FC`） |

> `build_deck.py` 的 `vecspec` 阶段会自动填好 `pad`、`palette` 以及各 kind 的实测几何，
> 手工写 spec 时才需要自己填。**绝对坐标（`x0`/`y0`/`cy`/`line_end`/`tip_x`）要用切片坐标
> （core 坐标 + pad）；`cell_dx`/`row_dy` 这类间距不用加。**

### `kind: "chip"` — 圆角胶囊标签

| 字段 | 必填 | 说明 |
|---|---|---|
| `w` `h` | ✓ | 宽高（px，取自原切片尺寸，便于原位替换） |
| `text` | | 标签文字；`--no-text` 时忽略 |
| `font_size` | | 缺省 `h * 0.45` |
| `stroke_w` | | 描边宽度（px）；缺省 `1.8`。**复刻务必实测**：宽度对边缘逐像素差的影响比颜色还大（实测真值 1.0 而用缺省 1.8 时，胶囊区 mean 从 15.2 升到 17.6）。估法：描边色像素数 ÷ 胶囊轮廓长 `2*(W-H) + π*H` |

### `kind: "arrow"` — 水平链路箭头

| 字段 | 必填 | 说明 |
|---|---|---|
| `w` `h` | ✓ | 画布尺寸 |
| `cy` | | 线杆中心 y；缺省 `h/2`。**原图常不居中，务必实测后覆盖** |
| `shaft_w` | | 线杆宽度（px）；缺省 `2.6`。**务必实测** |
| `line_end` | | 线杆终点 x；缺省 `w - pad - 12` |
| `tip_x` | | 箭头尖 x；缺省 `w - pad` |
| `head_h` | | 三角半高；缺省 `4.6`。**务必实测**：缺省值是某个项目的拟合结果，换图可能差 2~3 倍（实测真值 12，用 4.6 时该元素 mean 18.4） |
| `gid` | | 渐变 id；缺省 `ar-<name>` |

> `cy` / `shaft_w` / `head_h` / `line_end` / `tip_x` 的实测法见
> `route_elements.py::_arrow_geom()`（列高剖面：杆区平坦、头区从 max 收到 0）。
> ⚠️ 杆的渐变 stroke 必须 `gradientUnits="userSpaceOnUse"`，
> 否则零高度 bbox 让渐变退化、**杆整条不渲染**。

### `kind: "bar"` — 通栏结论色带

| 字段 | 必填 | 说明 |
|---|---|---|
| `w` `h` | ✓ | 尺寸 |

固定输出：水平渐变 + 竖向高光，均不含文字。

### `kind: "grid"` — 规则网格阵列

| 字段 | 必填 | 说明 |
|---|---|---|
| `w` `h` | ✓ | 画布尺寸 |
| `cols` `rows` | ✓ | 列数、行数 |
| `cell_dx` `row_dy` | | 列间距、行间距；缺省 `48.5` / `39` |
| `x0` `y0` | | 首个图标左上角（切片坐标）；缺省 `4` / `6` |
| `symbol` | | **一段 SVG 片段**，作为格子符号（局部坐标 0,0 起）。给了就用它，否则按 `icon` 查内置表 |
| `icon` | | 内置符号名，目前只有 `"station"` |
| `label` | | 标签文字（如「站车站」） |
| `label_rows` | | 带标签的行号集合（从 1 起），如 `[2, 3]` |
| `label_dx` `label_dy` | | 标签相对图标的偏移；缺省 `19.5` / `36` |
| `label_font_size` | | 缺省 `9` |

> **阵列的「规律性」是实测的，格子长什么样推不出来。** 内置只有一个示例符号，
> 形状不同必须照原图重绘**一个格子**，放 `work/hand/<切片名>.cell.svg`（`build_deck` 自动读入）。
> 硬套内置货会把整块阵列画错：实测墨迹覆盖只有原图的 0.34 倍、mean 18.7 / >60 占 10.5%。

---

## 3. `layout.json` — 版面（`layout_to_pptx.py` + `svg_embed.py`）

完整字段见 `图片复刻为可编辑PPT` 技能的 SKILL.md。本技能新增的只有一项：

| 字段 | 位置 | 说明 |
|---|---|---|
| `svg` | `images[]` 内 | 该元素要升格成的矢量文件路径。**写了才参与 `svg_embed.py --layout` 的替换**；不写保持位图 |

`images[]` 的**顺序 = PPTX 里图片的加入顺序**，`svg_embed.py --layout` 靠这个顺序对齐
（自动跳过第一张满页背景）。所以**必须用生成该 PPTX 的同一份 layout.json**。

**表格区域不要写进 `images[]`** —— 走下面的 `tables.json` 做成原生表格。
表格是 `graphicFrame`，不占 `images[]` 的位置，所以**加表格不会打乱矢量替换的对应关系**。

**`texts[].ink_box` / `texts[].anchor_box` 的值应当经闭环校准**，不要停在人工目测阶段：
`calibrate_layout.py` 会渲染回读、反解平移这些锚点，
迭代到 |Δ中心| ≤ 1px 后输出 `<stem>-fixed.json`。**校准只改这两个框的数值，不改字段结构**
——所以拿 `-fixed.json` 继续走 `svg_embed.py --layout` 完全兼容（`images[]` 顺序不受影响）。

---

## 4. `tables.json` — 原生表格（`add_tables.py`）

把图里的表格做成 PowerPoint 原生表格（真 `<a:tbl>`：单元格可选中改字、可调行列、可合并）。

```jsonc
{
  "style": {                                  // 全局样式；单个表格可用同名键覆盖
    "font": "微软雅黑",                        // 写入 a:latin / a:ea / a:cs
    "latin_font": "微软雅黑",                  // 可选，缺省同 font
    "size_pt": 12, "color": [32,38,50],
    "align": "center",                        // left / center / right
    "valign": "middle",                       // top / middle / bottom
    "bold": false, "spc": null,                // spc 单位 1/100 pt
    "margin_pt": [2, 13.2, 2, 13.2],          // top, right, bottom, left
    "cell_fill": [255,255,255],                // null 则透明
    "header_fill": [232,240,250], "header_bold": true, "header_color": [12,45,100],
    "zebra_fill": [249,251,254],              // 隔行底色；从表头下一行起算
    "border_color": [200,210,225], "border_w_pt": 0.75,
    "border_sides": "LRTB",                   // "LRTB" 的子集
    "no_border": false
  },
  "tables": [
    {
      "name": "接口清单",                      // 同时作为 PPTX 里的 shape 名
      "box": [140,240,1460,700],              // 必填，画布像素坐标
      "col_widths": [200,380,380,360],        // px；和 ≤1.5 时按比例解释；缺省等分
      "row_heights": [80,95,95,95,95],        // 同上。**行高是最小值**，会被内容撑高
      "header_rows": 1,
      "z": "under-text",                      // under-text（缺省）/ top；也可用 CLI --z 全局指定
      "cover": {"pad": 2, "fill": [255,255,255]},   // 可选：先垫一层纯色底，防漏原图残留
      "style": {...},                          // 可选，覆盖全局
      "cells": [ /* 见下 */ ]
    }
  ]
}
```

### `cells` — 只写**起始格**

行 × 起始格；被 `colspan` / `rowspan` 吃掉的位置**不用写占位**。脚本用占用表
（occupancy grid）自动跳过并校验「无重叠 / 无空洞 / 不越界」，出错会报出具体坐标。

```jsonc
"cells": [
  [{"text":"序号"}, {"text":"接口名称"}, {"text":"责任人"}, {"text":"状态"}],   // 4 项
  [{"text":"1"}, {"text":"站务系统主数据接口","align":"left"},
   {"text":"张工"}, {"text":"进行中","rowspan":2}],                            // 4 项
  [{"text":"2"}, {"text":"扶梯状态上报接口","align":"left"}, {"text":"李工"}],  // 3 项，第 4 列被上面吃掉
  [{"text":"3"}, {"text":"客流数据同步接口","align":"left"},
   {"text":"王工"}, {"text":"已完成"}],
  [{"text":"合计","colspan":2}, {"text":"—"}, {"text":"2 项完成"}]             // 3 项，colspan=2
]
```

| 字段 | 说明 |
|---|---|
| `text` | 单元格文字；`\n` 分成多个段落。数字也可 |
| `size_pt` `bold` `color` `fill` `align` `valign` `spc` | 同全局 style，逐格覆盖 |
| `colspan` `rowspan` | 合并跨度，缺省 1 |
| `no_border` | `true` 则该格四边都不画线 |
| `border` | `{color, w_pt, sides, style}`，覆盖全局边框 |

### probe：从原图探网格线（只读）

```bash
python add_tables.py probe --ref 原图.png --box 140,240,1460,700 [--dark 231] [--frac 0.5]
```

输出 `col_widths` / `row_heights` 建议值、四条边界坐标、框内与框外的众数色
（后者是 `cover.fill` 的候选）。

- `--dark` 缺省**自适应**（框内中位灰度 − 20，clamp 到 150~245）。**不要用固定暗阈值**：
  实测网格线灰度 211、表头填充 241、白底 255，固定 200 会把网格线整条漏掉。
- `--frac` 是「沿该列/行方向暗像素占比」阈值，缺省 0.5。
  合并区会打断网格线导致占比下降，占比过低时可调小（如 0.35）。

---

## 5. `map.json` — 显式指定替换（`svg_embed.py --map`）

用于对**任意已有 PPTX** 后处理，不依赖 layout。

```jsonc
{
  "items": [
    {"shape": "01_cloud-platform", "svg": "04-vector/01_cloud-platform.svg"},
    {"shape": "m1_c1_station-icon", "svg": "04-vector/06_station-icon.svg"},
    {"media": "image7.png",         "svg": "04-vector/07_chip-set1-1.svg"}
  ]
}
```

| 字段 | 说明 |
|---|---|
| `shape` | PPTX 里 shape 的名字（`--list` 可看）。**优先** |
| `media` | 包内媒体文件名（如 `image7.png`）。shape 名匹配不到时的兜底 |
| `svg` | 目标矢量文件 |

- 先用 `svg_embed.py --pptx x.pptx --list` 拿到 shape 名。
- 同一个 SVG 被多项引用是**正常的**（35 枚车站图标共用 1 个 SVG）——
  脚本按路径缓存 part 与 rId，不会重复打包。

---

## 6. `pairs.json` — 对比图（`vector_compare.py`）

```jsonc
{
  "render_dir": "slices/_probe/render",     // 矢量渲染出的 PNG 目录
  "out": "slices/_sheet-vector-compare.png",
  "title": "原图切片（位图） → 矢量重绘（SVG）",
  "subtitle": "自定义副标题",
  "cols_width": 1180,
  "items": [
    {"label": "云平台", "orig": "slices/01-graphics/01_cloud-platform.png",
     "vec": "01_cloud-platform.svg", "max_side": 190},
    {"label": "车站阵列", "vec": "18_station-grid.svg", "max_side": 250,
     "orig_crop": {"src": "原图.png", "box": [812,159,1152,343]}},
    {"label": "箭头 A1", "orig": "slices/01-graphics/05_arrow-a1.png",
     "vec": "14_arrow-a1.svg", "h": 34}
  ]
}
```

| 字段 | 说明 |
|---|---|
| `render_dir` | 必填。`vec` 同名 `.png` 所在目录 |
| `orig` | 原图切片路径（已有独立切片的元素用这个） |
| `orig_crop` | 从原图现场裁剪：`{"src": 原图, "box": [x1,y1,x2,y2]}`。适合**没有独立切片**的整块元素（如车站阵列） |
| `vec` | 矢量文件名（在 `render_dir` 里找同名 `.png`） |
| `max_side` / `h` / `w` | 尺寸控制三选一，缺省 `max_side: 190`。扁长元素（箭头、色带）用 `h`/`w` 才不会被缩得看不见 |
