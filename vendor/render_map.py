"""把 nxmap 导出的 map.json 渲染成 PNG —— 逐条对齐 NxMapLoader.cs 的绘制语义。

用途：
  1) 给"AI 生成的地图"出预览图，肉眼验收；
  2) 反推原版地图的贴砖/分层规则（对比原版地图渲染结果）。

绘制顺序（与客户端一致）：
  back(front=0)  →  layer0..layer7 [ tile(z=0) + obj(z 升序) ]  →  back(front=1)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from PIL import Image

# ---------------------------------------------------------------- 素材索引


class TexStore:
    """按文件名加载 PNG，带缓存；搜索多个目录（地图自己的 img / 素材库 img）。"""

    def __init__(self, dirs: list[str]):
        self.dirs = [d for d in dirs if d]
        self._cache: dict[str, Image.Image | None] = {}

    def get(self, name: str) -> Image.Image | None:
        if not name:
            return None
        if name in self._cache:
            return self._cache[name]
        img = None
        for d in self.dirs:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                try:
                    img = Image.open(p).convert("RGBA")
                    break
                except Exception:
                    continue
        self._cache[name] = img
        return img


# ---------------------------------------------------------------- 背景平铺


# 原版 back.type 语义（v83 Map.wz）：
#   0 单张 / 1 横条带 / 2 纵条带 / 3 双向铺 / 4 横条带 / 5 纵条带 / 6 双向铺
# 从原版数据反推出来的用法：
#   天幕是 256×256 的 tile，type=3 必须双向铺，否则上方会露出硬边；
#   丘陵/云层是 1024×h 的横条带，type=1/4 只横铺，纵铺会重复出多层山。
BACK_TILE_X = {1, 2, 3, 4, 5, 6}
BACK_TILE_Y = {2, 3, 5, 6}


def _paste_tiled(canvas, tex, x0, y0, step_x, step_y, w, h, alpha, clip):
    """把 tex 按 step 平铺覆盖矩形 (x0,y0,w,h)，坐标是画布坐标。"""
    if step_x <= 0 or step_y <= 0:
        return
    im = tex
    if alpha < 255:
        im = tex.copy()
        a = im.getchannel("A").point(lambda v: v * alpha // 255)
        im.putalpha(a)
    # 只贴覆盖 clip 区域所需的那些份
    nx = int(w // step_x) + 2
    ny = int(h // step_y) + 2
    for j in range(ny):
        for i in range(nx):
            canvas.alpha_composite(im, (int(x0 + i * step_x), int(y0 + j * step_y)))


def draw_back(canvas, b, tex, origin, bounds, parallax=1.0):
    """origin=(ox,oy) 为世界→画布偏移；bounds=(minx,miny,w,h) 为世界坐标可视范围。"""
    if tex is None:
        return
    ox, oy = origin
    t = int(b.get("type", 0))
    a = int(b.get("a", 255))
    bx = float(b.get("x", 0)) * parallax
    by = float(b.get("y", 0)) * parallax
    tw = int(b.get("cx", 0)) or tex.width
    th = int(b.get("cy", 0)) or tex.height
    minx, miny, bw, bh = bounds

    if t == 0:
        # 单图：以 (x,y) 为中心
        canvas.alpha_composite(tex, (int(bx - tex.width / 2 - minx), int(by - tex.height / 2 - miny)))
        return

    # 平铺：以 (x,y) 为相位锚点，铺满整个可视范围
    sx = tw if t in BACK_TILE_X else 10 ** 9
    sy = th if t in BACK_TILE_Y else 10 ** 9
    # 对齐 (bx,by) 相位
    x0 = bx - ((bx - minx) // sx) * sx if sx < 10 ** 8 else bx
    y0 = by - ((by - miny) // sy) * sy if sy < 10 ** 8 else by
    _paste_tiled(canvas, tex, x0 - minx, y0 - miny, sx, sy, bw + sx, bh + sy, a, bounds)


# ---------------------------------------------------------------- 主流程


@dataclass
class RenderOpts:
    scale: float = 1.0
    max_width: int = 1600
    parallax: float = 1.0     # 背景视差系数（1.0 = 世界坐标直贴，仅预览用）
    bounds: tuple | None = None
    view: tuple | None = None    # (x, y, w, h)：按「相机取景框」渲染，背景才是游戏里的样子
    show_footholds: bool = False
    fill_base: bool = False      # 预览专用：底天幕强制双向平铺（小尺寸天空砖时用）
    no_bg: bool = False          # 不画背景（back 层全跳过，画布留透明）
    pad: int = 40


def map_bounds(m: dict) -> tuple[float, float, float, float]:
    """内容包围盒。注意不能算进 back：背景是屏幕空间的，(x,y) 不是世界坐标，
    算进来会把取景框推到地图外面（之前草原小镇左边全空就是这个原因）。"""
    xs, ys = [], []
    for L in m.get("layers", []):
        for it in list(L.get("tile", [])) + list(L.get("obj", [])):
            x, y = float(it["x"]), float(it["y"])
            xs += [x - it.get("ox", 0), x - it.get("ox", 0) + it.get("w", 0)]
            ys += [y - it.get("oy", 0), y - it.get("oy", 0) + it.get("h", 0)]
    for s in m.get("foothold", []):
        xs += [s["x1"], s["x2"]]
        ys += [s["y1"], s["y2"]]
    if not xs:
        return (0, 0, 800, 600)
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _blit(canvas, tex, x0, y0, x1, y1, alpha=255):
    """把 tex 贴进画布矩形 [x0,x1)×[y0,y1)（尺寸对不上就缩放）。

    关键：**两端各自按世界坐标取整**，而不是「起点取整 + 缩放后的宽度」。
    相邻瓦片在世界坐标里共享同一条边，取整后也必然共享同一个像素边界，
    不会漏出 1px 的缝（以前就是这么漏出天空色的青色网格线的）。
    """
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    if (w, h) == tex.size:
        im = tex
    elif w * h < tex.width * tex.height:
        # 缩小用面积平均（BOX）：和「整图缩一次」几乎一致，且没有 LANCZOS 的
        # 振铃在每块贴图边缘留下的一圈亮边（缩略图上就是淡淡的方格）。
        im = tex.resize((w, h), Image.BOX)
    else:
        im = tex.resize((w, h), Image.LANCZOS)
    if alpha < 255:
        im = im.copy()
        im.putalpha(im.getchannel("A").point(lambda v: v * alpha // 255))
    canvas.alpha_composite(im, (x0, y0))


def render_map(map_json: dict, img_dirs: list[str], opts: RenderOpts | None = None) -> Image.Image:
    opts = opts or RenderOpts()
    store = TexStore(img_dirs)
    if opts.view:
        minx, miny, bw, bh = opts.view
    else:
        minx, miny, bw, bh = opts.bounds or map_bounds(map_json)
        minx -= opts.pad
        miny -= opts.pad
        bw += opts.pad * 2
        bh += opts.pad * 2

    s = min(opts.scale, opts.max_width / max(1.0, bw))
    W, H = max(1, int(bw * s)), max(1, int(bh * s))
    # no_bg：跳过所有 back 层（天幕/云/山），画布留全透明——只留地形与物件，
    # 透明 PNG 还能直接叠到别的底图上（做对比图 / 素材用）
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0) if opts.no_bg else (16, 18, 24, 255))
    bounds_world = (minx, miny, bw, bh)

    def place(im: Image.Image, wx: float, wy: float, alpha: int = 255):
        """把世界坐标 (wx,wy) 当左上角贴图；目标矩形两端各自取整，避免相邻图之间出缝。"""
        _blit(canvas, im,
              int(round((wx - minx) * s)), int(round((wy - miny) * s)),
              int(round((wx + im.width - minx) * s)), int(round((wy + im.height - miny) * s)),
              alpha)

    # --- 背景（front=0）：预览时提供世界坐标可视化 ---
    base_backs = [] if opts.no_bg else [b for b in map_json.get("back", [])
                                        if int(b.get("front", 0)) == 0]
    if base_backs:
        # 底天幕在游戏里只铺一部分屏幕，其余是引擎清屏色。
        # 预览时用「把底图按行平均后纵向拉伸」的渐变铺满，既不出黑边也不假。
        t0 = store.get(base_backs[0].get("img", ""))
        if t0 is not None:
            strip = t0.resize((1, max(2, t0.height)), Image.LANCZOS)
            canvas = strip.resize((W, H), Image.BILINEAR)
    for bi, b in enumerate(base_backs):
        tex = store.get(b.get("img", ""))
        if tex is None:
            continue
        # 背景是屏幕空间：把「屏幕中心」当作画布中心，x/y 是相对屏幕中心的偏移
        t = int(b.get("type", 0))
        bx = float(b.get("x", 0)) * opts.parallax + W / 2
        by = float(b.get("y", 0)) * opts.parallax + H / 2
        tw = int(b.get("cx", 0)) or tex.width
        th = int(b.get("cy", 0)) or tex.height
        wd = max(1, int(round(tex.width * s)))
        ht = max(1, int(round(tex.height * s)))
        if t == 0:
            x0 = int(round(bx - wd / 2))
            y0 = int(round(by - ht / 2))
            _blit(canvas, tex, x0, y0, x0 + wd, y0 + ht)
            continue
        tiled_x = t in BACK_TILE_X
        tiled_y = t in BACK_TILE_Y
        # 不平铺的方向只能画一份。以前这里填 W/H 当"步长"，等于每屏重复一次，
        # 结果 77×440 的浅色层被复制到屏幕顶部，露出大片白色横带。
        # 步长必须跟着缩放走（以前用未缩放的 cx/cy，缩略图上就出现整条空带）。
        sx = tw * s if tiled_x else 0
        sy = th * s if tiled_y else 0
        import math as _m
        if tiled_x:
            i0 = int(_m.floor(-bx / max(1e-6, sx))) - 1
            i1 = int(_m.ceil((W - bx) / max(1e-6, sx))) + 1
        else:
            i0 = i1 = 0
        if tiled_y:
            j0 = int(_m.floor(-by / max(1e-6, sy))) - 1
            j1 = int(_m.ceil((H - by) / max(1e-6, sy))) + 1
        else:
            j0 = j1 = 0
        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                x0 = int(round(bx + i * sx))
                y0 = int(round(by + j * sy))
                # 平铺方向上一直画到下一份的起始边界：步长比图大就拉满补缝，
                # 步长比图小就按原尺寸重叠（重叠处后贴的盖住前面的，和客户端一致）。
                x1 = max(x0 + wd, int(round(bx + (i + 1) * sx)) if tiled_x else x0 + wd)
                y1 = max(y0 + ht, int(round(by + (j + 1) * sy)) if tiled_y else y0 + ht)
                _blit(canvas, tex, x0, y0, x1, y1)

    # --- 8 层 ---
    layers = map_json.get("layers", [])
    for li in range(8):
        L = layers[li] if li < len(layers) else {}
        for t in L.get("tile", []):
            tex = store.get(t.get("img", ""))
            if tex is None:
                continue
            place(tex, float(t["x"]) - t.get("ox", 0), float(t["y"]) - t.get("oy", 0), int(t.get("a", 255)))
        objs = sorted(L.get("obj", []), key=lambda o: (int(o.get("z", 0)), int(o.get("no", 0))))
        for o in objs:
            tex = store.get(o.get("img", ""))
            if tex is None:
                continue
            im = tex
            if int(o.get("f", 0)) == 1:
                im = im.transpose(Image.FLIP_LEFT_RIGHT)
            place(im, float(o["x"]) - o.get("ox", 0), float(o["y"]) - o.get("oy", 0), int(o.get("a", 255)))

    # --- 前景背景（front=1）---
    for b in ([] if opts.no_bg else map_json.get("back", [])):
        if int(b.get("front", 0)) != 1:
            continue
        tex = store.get(b.get("img", ""))
        if tex is None:
            continue
        _blit(canvas, tex,
              int(round((float(b["x"]) - tex.width / 2 - minx) * s)),
              int(round((float(b["y"]) - tex.height / 2 - miny) * s)),
              int(round((float(b["x"]) + tex.width / 2 - minx) * s)),
              int(round((float(b["y"]) + tex.height / 2 - miny) * s)))

    if opts.show_footholds:
        from PIL import ImageDraw
        d = ImageDraw.Draw(canvas)
        for fh in map_json.get("foothold", []):
            d.line([((fh["x1"] - minx) * s, (fh["y1"] - miny) * s),
                    ((fh["x2"] - minx) * s, (fh["y2"] - miny) * s)], fill=(40, 255, 90, 220), width=2)
    return canvas


def render_file(map_dir: str, out_png: str, lib_img: str = "", **kw):
    with open(os.path.join(map_dir, "map.json"), encoding="utf-8") as f:
        m = json.load(f)
    dirs = [os.path.join(map_dir, "img"), lib_img]
    im = render_map(m, dirs, RenderOpts(**kw))
    im.save(out_png)
    return im


def edge_ground_y(map_json: dict, side: str) -> int:
    """地图某一端（'left'/'right'）边缘 90px 内的地面高度（世界中位 y）。

    两半图要拼成一张时，真正的"对接线"不是各自的主地面（全图最高票那条），
    而是**接缝处两端的地面**——地图右端可以比主地面低 2 行（坡/坑），
    按主地面对齐拼起来照样错位。取边缘 90px（1 列）内地面的中位数，抗装饰噪声。
    """
    segs = [(f["x1"], f["y1"], f["x2"], f["y2"]) for f in map_json.get("foothold", [])
            if abs(f["y1"] - f["y2"]) <= 2 and f["x1"] != f["x2"]]
    if not segs:
        return 0
    xs = [v for s in segs for v in (s[0], s[2])]
    lo, hi = min(xs), max(xs)
    if side == "right":
        cand = sorted(s[1] for s in segs if max(s[0], s[2]) >= hi - 90)
    else:
        cand = sorted(s[1] for s in segs if min(s[0], s[2]) <= lo + 90)
    return cand[len(cand) // 2] if cand else 0


def main_ground_y(map_json: dict) -> int:
    """主地平线的世界 y：按 foothold 的 **x 覆盖宽度** 投票（同 y 区间合并去重）。

    不能按段长直接累加 —— 学习模型生成的多层浮台有十几块，段长总和会超过主链，
    真正的连续主地面反而被挤出画面中心（对照图两半因此错位成断崖）。
    也不能取最低那条 —— 地图里有个深坑就会把主地面整个顶出画面（只剩一条土墙）。
    主链是一条贯穿全宽的连续区间，浮台再分散也合并不出等宽覆盖，抢不走票。
    """
    miny, _, _, _ = map_bounds(map_json)
    cover: dict[int, list] = {}
    for f in map_json.get("foothold", []):
        if abs(f["y1"] - f["y2"]) <= 2:
            x1, x2 = sorted((f["x1"], f["x2"]))
            if x2 - x1 < 2:
                continue
            cover.setdefault(int(f["y1"]), []).append((x1, x2))
    main_y, best = miny, -1
    for y, ivals in cover.items():
        ivals.sort()
        tot, cur_s, cur_e = 0, ivals[0][0], ivals[0][1]
        for s, e in ivals[1:]:
            if s > cur_e:
                tot += cur_e - cur_s
                cur_s, cur_e = s, e
            else:
                cur_e = max(cur_e, e)
        tot += cur_e - cur_s
        if tot > best:
            best, main_y = tot, y
    return main_y


def render_screens(map_json: dict, img_dirs: list[str], view=(1024, 640), n=None,
                   scale=1.0, sep=6, ground_at=0.55, mode="panorama",
                   max_w=2600, main_y: int | None = None) -> Image.Image:
    """预览图。

    mode="panorama"（默认）——整张地图一条连续长图。渲染时按 1:1 世界坐标一次画完，
        最后整体缩一次；背景层也按世界对齐，所以**没有拼接缝**。
        这是"地图长什么样"的视图，判断地形连不连、装饰疏密都看它。

    mode="screen"——游戏里真正的一屏（1024×640），以出生传送门为中心。
        背景是有视差的屏幕空间层，只有这个视图跟游戏画面完全一致。

    ground_at：主地面在画面里落在第几成高度。原版地图基本在 0.52~0.58，
        背景层（草地远景、丘陵）就是照着这个位置画的，取景偏了背景就会错位。
    main_y：外部指定主地平线（世界 y）。两半图要拼成一张时必须传同一个值，
        否则两半各自选主地面，行差可达几十像素，接缝处错位成断崖。
    """
    vw, vh = view
    minx, miny, bw, bh = map_bounds(map_json)
    if main_y is None:
        main_y = main_ground_y(map_json)
    # 让主地平线落在画面的 ground_at 处：cy = main_y - (ground_at - 0.5) * vh
    cy = main_y - (ground_at - 0.5) * vh

    if mode == "screen":
        sx = [p["x"] for p in map_json.get("portal", []) if p.get("pn") == "sp"]
        cx = float(sx[0]) if sx else minx + bw / 2
        if bw >= vw:
            cx = min(max(cx, minx + vw / 2), minx + bw - vw / 2)
        else:
            cx = minx + bw / 2
        return render_map(map_json, img_dirs,
                          RenderOpts(view=(cx - vw / 2, cy - vh / 2, vw, vh),
                                     scale=scale, max_width=vw))

    # 连续长图：必须 scale=1.0 一次画完再整体缩放。
    # 如果让 render_map 自己缩放，每块砖都会各自取整贴图 → 砖缝处会露出 1px 黑线。
    W = max(vw, bw)
    im = render_map(map_json, img_dirs,
                    RenderOpts(view=(minx, cy - vh / 2, W, vh), scale=1.0,
                               max_width=int(W)))
    if im.width > max_w:
        im = im.resize((max_w, max(1, round(im.height * max_w / im.width))), Image.LANCZOS)
    return im


if __name__ == "__main__":
    import sys
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else "out/preview.png"
    lib = sys.argv[3] if len(sys.argv) > 3 else ""
    render_file(src, dst, lib, max_width=1800)
    print("wrote", dst)
