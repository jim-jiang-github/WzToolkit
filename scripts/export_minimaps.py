"""批量导出「全库小地图」为一套可交付的图鉴目录（PNG + 映射表 + 图鉴页）。

数据源：`.cache/minimap/`（nxmap mini-all 产出 + 服务预热补生成的兜底图）。
- official/  官方 canvas（WZ 里烘焙好的 miniMap/canvas，透明底地形轮廓）
- auto/      自动兜底（源数据 canvas 退化成 1x1 的图，按客户端 UIMiniMap 几何
             重新生成：无背景、黑底、只画 pt=2 传送口蓝圈）

产出（默认 .exports/minimap-demo/）：
    official/<id>.png   auto/<id>.png
    index.json          全量坐标映射表（世界坐标 <-> 小地图像素）
    data.js             图鉴页数据（window.MM_DATA，file:// 下不受 CORS 限制）
    index.html          图鉴总览（分页 + 搜索 + 来源筛选，双击即可打开）

用法：
    PYTHONPATH=.pylibs .pyruntime\\python.exe scripts\\export_minimaps.py
    ... --all                          # 全库 14449 张
    ... --only 000030001,102030000     # 只导指定 id（同样目录结构）
    ... --zoom 4                       # 额外输出 N 倍放大 PNG（默认不生成，
                                       #   图鉴里用 CSS pixelated 放大，不占体积）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / ".pylibs") not in sys.path:          # 依赖只走 .pylibs，不动系统 Python
    sys.path.insert(0, str(ROOT / ".pylibs"))

from PIL import Image, ImageDraw  # noqa: E402

MMIDX = ROOT / ".cache" / "minimap"
MAPCACHE = ROOT / ".cache" / "map"
PORTALS = ROOT / ".cache" / "portals.csv"   # nxmap portal-all 产出（全库传送门表）
PORTAL_RING = 2                             # 传送门圈半径（canvas 像素）
PORTAL_LIFT = 3                             # 传送门上移量：脚底坐标会压在地面线上


def load_portals() -> dict[str, list[tuple[int, int, int]]]:
    """mapid → [(pt, x, y)]，来自 `nxmap portal-all`。缺文件时返回空（只贴黑底不画圈）。"""
    out: dict[str, list[tuple[int, int, int]]] = {}
    if not PORTALS.exists():
        return out
    import csv

    with PORTALS.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            try:
                out.setdefault(r["mapid"], []).append(
                    (int(r["pt"]), int(r["x"]), int(r["y"])))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def compose(png: Path, pts: list[tuple[int, int, int]],
            cx: float, cy: float, scale: float) -> Image.Image:
    """官方 canvas 贴到纯黑底上，再按客户端公式把 pt=2 传送口画成蓝圈。"""
    src = Image.open(png).convert("RGBA")
    canvas = Image.new("RGBA", src.size, (0, 0, 0, 255))
    canvas.alpha_composite(src)
    dr = ImageDraw.Draw(canvas)
    for pt, x, y in pts:
        if pt != 2:
            continue
        px, py = (x + cx) / scale, (y + cy) / scale - PORTAL_LIFT
        dr.ellipse([px - PORTAL_RING, py - PORTAL_RING,
                    px + PORTAL_RING, py + PORTAL_RING],
                   outline=(64, 160, 255, 255), width=1)
    return canvas


def collect(only: list[str] | None, want_all: bool,
            portals_map: dict[str, list[tuple[int, int, int]]] | None = None) -> list[dict]:
    idx = json.loads((MMIDX / "index.json").read_text(encoding="utf-8"))
    rows: list[dict] = []
    for mid, v in sorted(idx.items()):
        if only:                               # 指定 id 时不再过滤来源
            if mid not in only:
                continue
        elif not want_all and not v.get("auto"):
            continue
        png = MMIDX / f"{mid}.png"
        if not png.exists():
            continue
        with Image.open(png) as im:
            cw, ch = im.size
        auto = bool(v.get("auto"))
        if auto and cw <= 1 and ch <= 1:
            continue                       # 兜底图没生成出来，跳过
        try:
            scale = float(2 ** int(v.get("mag") or 0))
        except (TypeError, ValueError, OverflowError):
            scale = 1.0
        cx, cy = float(v.get("centerX") or 0), float(v.get("centerY") or 0)
        ww, wh = round(cw * scale), round(ch * scale)
        portals = None
        if portals_map and mid in portals_map:
            portals = sum(1 for pt, _, _ in portals_map[mid] if pt == 2)
        mjf = MAPCACHE / mid / "map.json"
        if mjf.exists():
            try:
                mj = json.loads(mjf.read_text(encoding="utf-8"))
                mm = mj.get("miniMap") or {}
                ww = int(mm.get("w") or ww)
                wh = int(mm.get("h") or wh)
                if portals is None:
                    portals = sum(1 for p in mj.get("portal", []) if p.get("pt") == 2)
            except Exception:                  # noqa: BLE001
                pass
        sub = "auto" if auto else "official"
        rows.append({
            "id": mid,
            "name": v.get("name") or "",
            "street": v.get("street") or "",
            "src": sub,                    # official = WZ 原生 canvas；auto = 重新生成
            "file": f"{sub}/{mid}.png",
            "cw": cw, "ch": ch,
            "mag": int(v.get("mag") or 0), "scale": scale,
            "centerX": cx, "centerY": cy,
            "worldWidth": ww, "worldHeight": wh,
            "worldBounds": [-cx, -cy, ww - cx, wh - cy],
            "portals": portals,
        })
    return rows


def write_viewer(out: Path, rows: list[dict]) -> None:
    data = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "coord": {
            "scale": "scale = 2^mag",
            "world_to_px": "px = (world + center) / scale",
            "px_to_world": "world = px * scale - center",
            "note": "px 为 canvas 像素坐标（左上原点），world 为地图世界坐标",
        },
        "maps": rows,
    }
    (out / "data.js").write_text(
        "window.MM_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")

    n_official = sum(1 for r in rows if r["src"] == "official")
    n_auto = len(rows) - n_official
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>小地图图鉴 · {len(rows)} 张</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #0e1116; color: #e6edf7;
         font: 13px/1.5 "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; }}
  header {{ position: sticky; top: 0; z-index: 5; padding: 14px 20px 12px;
            background: #0e1116ee; backdrop-filter: blur(8px);
            border-bottom: 1px solid #222933; }}
  h1 {{ margin: 0 0 4px; font-size: 16px; font-weight: 600; letter-spacing: .3px; }}
  .sub {{ color: #8b98a9; font-size: 12px; }}
  .sub code {{ background: #1a1f27; padding: 1px 5px; border-radius: 4px; color: #9fd0ff; }}
  .bar {{ display: flex; gap: 10px; align-items: center; margin-top: 10px; flex-wrap: wrap; }}
  input, select {{ padding: 7px 11px; border-radius: 7px; border: 1px solid #2a323d;
                   background: #151a21; color: #e6edf7; outline: none; }}
  input {{ flex: 1 1 240px; min-width: 160px; }}
  input:focus, select:focus {{ border-color: #3d7dd8; }}
  .hint {{ color: #6b7889; font-size: 12px; white-space: nowrap; }}
  main {{ display: grid; gap: 12px; padding: 16px 20px 10px;
          grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); }}
  .card {{ cursor: pointer; background: #151a21; border: 1px solid #222933;
           border-radius: 10px; overflow: hidden; transition: border-color .15s; }}
  .card:hover {{ border-color: #3d7dd8; }}
  .shot {{ display: grid; place-items: center; min-height: 92px; padding: 10px;
           background:
             linear-gradient(45deg, #0b0e12 25%, transparent 25%, transparent 75%, #0b0e12 75%) 0 0/14px 14px,
             linear-gradient(45deg, #0b0e12 25%, transparent 25%, transparent 75%, #0b0e12 75%) 7px 7px/14px 14px,
             #101419; }}
  .shot img {{ max-width: 100%; image-rendering: pixelated; display: block; }}
  .shot.bad::after {{ content: "图片未随页面加载，请在资源管理器里双击 index.html 打开";
                      color: #6b7889; font-size: 11px; text-align: center; padding: 0 8px; }}
  .meta {{ padding: 8px 11px 10px; border-top: 1px solid #1d232c; }}
  .nm {{ font-size: 12.5px; font-weight: 600; white-space: nowrap; overflow: hidden;
         text-overflow: ellipsis; }}
  .st {{ color: #7c8a9c; font-size: 11px; margin-top: 1px; white-space: nowrap;
         overflow: hidden; text-overflow: ellipsis; }}
  .dim {{ color: #8b98a9; font-size: 10.5px; margin-top: 5px; display: flex; gap: 5px;
          align-items: center; flex-wrap: wrap; }}
  .dim code {{ color: #9fd0ff; }}
  .tag {{ background: #1b2431; border: 1px solid #263244; border-radius: 4px;
          padding: 0 5px; color: #93a7c0; }}
  .tag.auto {{ background: #2a1f12; border-color: #4a3517; color: #d9a44a; }}
  nav {{ display: flex; gap: 8px; align-items: center; justify-content: center;
         padding: 10px 0 30px; flex-wrap: wrap; }}
  nav button {{ padding: 6px 12px; border-radius: 7px; border: 1px solid #2a323d;
                background: #151a21; color: #e6edf7; cursor: pointer; }}
  nav button.cur {{ background: #2b3b52; border-color: #3d7dd8; }}
  nav button:disabled {{ opacity: .4; cursor: default; }}
  .empty {{ color: #6b7889; padding: 40px; text-align: center; grid-column: 1/-1; }}
  #lb {{ position: fixed; inset: 0; background: #000000d9; display: none;
         place-items: center; z-index: 20; cursor: zoom-out; }}
  #lb.on {{ display: grid; }}
  #lb img {{ image-rendering: pixelated; max-width: 92vw; max-height: 86vh;
             background: #101419; border: 1px solid #2a323d; border-radius: 8px;
             padding: 14px; }}
  #lb .cap {{ position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%);
              color: #aebacb; font-size: 13px; background: #151a21cc;
              padding: 5px 14px; border-radius: 7px; }}
</style>
</head>
<body>
<header>
  <h1>小地图图鉴 · {len(rows)} 张</h1>
  <div class="sub">官方 canvas {n_official} 张（WZ 原生，透明底） + 自动生成 {n_auto} 张（源数据退化，黑底只标传送点）。
      坐标映射见 <code>index.json</code>：画布像素 = (世界坐标 + center) / 2<sup>mag</sup></div>
  <div class="bar">
    <input id="q" placeholder="搜索 地图名 / 街道 / id …" autocomplete="off">
    <select id="src">
      <option value="">全部来源</option>
      <option value="official">官方 canvas</option>
      <option value="auto">自动生成</option>
    </select>
    <span class="hint" id="cnt"></span>
  </div>
</header>
<main id="grid"></main>
<div class="empty" id="empty" hidden>没有匹配的地图</div>
<nav id="pager"></nav>
<div id="lb"><img id="lbimg" alt=""><div class="cap" id="lbcap"></div></div>
<script src="data.js"></script>
<script>
const D = window.MM_DATA, PAGE = 120;
const grid = document.getElementById('grid'), pager = document.getElementById('pager');
const q = document.getElementById('q'), src = document.getElementById('src');
const cnt = document.getElementById('cnt'), empty = document.getElementById('empty');
const lb = document.getElementById('lb'), lbimg = document.getElementById('lbimg'),
      lbcap = document.getElementById('lbcap');
let view = D.maps, page = 0;

function card(r) {{
  const el = document.createElement('div');
  el.className = 'card';
  el.innerHTML = `<div class="shot"><img loading="lazy" src="${{r.file}}" alt=""
      onerror="this.parentNode.classList.add('bad'),this.remove()"></div>
    <div class="meta"><div class="nm">${{r.name || '（无名）'}}</div>
    <div class="st">${{r.street || '—'}}</div>
    <div class="dim"><code>${{r.id}}</code><span>${{r.cw}}×${{r.ch}}</span>
      <span class="tag">1/${{2 ** r.mag}}</span>
      <span class="tag">${{r.portals ?? '?'}} 传送</span>
      ${{r.src === 'auto' ? '<span class="tag auto">自动</span>' : ''}}</div></div>`;
  el.onclick = () => {{
    lbimg.src = r.file;
    lbcap.textContent = `${{r.id}} ${{r.name}} ${{r.street}} · ${{r.cw}}×${{r.ch}} · mag=${{r.mag}}（点击任意处关闭）`;
    lb.classList.add('on');
  }};
  return el;
}}

function render() {{
  grid.innerHTML = '';
  const slice = view.slice(page * PAGE, page * PAGE + PAGE);
  for (const r of slice) grid.appendChild(card(r));
  empty.hidden = view.length > 0;
  cnt.textContent = `${{view.length}} / ${{D.maps.length}}`;
  const pages = Math.max(1, Math.ceil(view.length / PAGE));
  pager.innerHTML = '';
  const mk = (t, p, cur, dis) => {{
    const b = document.createElement('button');
    b.textContent = t; if (cur) b.className = 'cur';
    b.disabled = !!dis;
    b.onclick = () => {{ page = p; render(); window.scrollTo(0, 0); }};
    pager.appendChild(b);
  }};
  mk('‹ 上一页', page - 1, false, page === 0);
  const win = [];
  for (let i = 0; i < pages; i++)
    if (i < 2 || i >= pages - 2 || Math.abs(i - page) <= 2) win.push(i);
  let last = -1;
  for (const i of win) {{
    if (i - last > 1) {{ const s = document.createElement('span');
      s.className = 'hint'; s.textContent = '…'; pager.appendChild(s); }}
    mk(String(i + 1), i, i === page, false); last = i;
  }}
  mk('下一页 ›', page + 1, false, page >= pages - 1);
}}

function apply() {{
  const k = q.value.trim().toLowerCase(), s = src.value;
  view = D.maps.filter(r =>
    (!s || r.src === s) &&
    (!k || `${{r.id}} ${{r.name}} ${{r.street}}`.toLowerCase().includes(k)));
  page = 0; render();
}}
q.addEventListener('input', apply);
src.addEventListener('change', apply);
lb.onclick = () => lb.classList.remove('on');
apply();
</script>
</body>
</html>
"""
    (out / "index.html").write_text(html, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="批量导出全库小地图 + 坐标映射表 + 图鉴页")
    ap.add_argument("--out", default=str(ROOT / ".exports" / "minimap"))
    ap.add_argument("--all", action="store_true", help="导出全库（官方 canvas + 自动兜底）")
    ap.add_argument("--only", default="", help="只导这些 id，逗号分隔")
    ap.add_argument("--zoom", type=int, default=0,
                    help="额外输出 N 倍最近邻放大 PNG（0 = 不生成）")
    args = ap.parse_args()

    only = [s.strip() for s in args.only.split(",") if s.strip()] or None
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    for p in out.iterdir():                    # 旧产物一次性清空，避免残留
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    (out / "official").mkdir(exist_ok=True)
    (out / "auto").mkdir(exist_ok=True)

    portals_map = load_portals()
    if not portals_map:
        print("警告：没有 .cache/portals.csv，官方 canvas 只会贴黑底、不画传送点\n"
              "      先生成它：cd .cache/cn && bin/nxmap.exe portal-all ../../.cache/portals.csv")

    rows = collect(only, args.all, portals_map)
    if not rows:
        print("没有可导出的小地图（先让服务预热生成兜底图）")
        return 1

    n_off = n_auto = 0
    n_ring = 0
    for i, r in enumerate(rows):
        src_png = MMIDX / f"{r['id']}.png"
        if r["src"] == "official":
            # 官方 canvas 是透明底：贴黑底 + 画传送门圈
            img = compose(src_png, portals_map.get(r["id"], []),
                          r["centerX"], r["centerY"], r["scale"])
            img.save(out / r["file"])
            n_ring += sum(1 for pt, _, _ in portals_map.get(r["id"], []) if pt == 2)
        else:
            shutil.copy2(src_png, out / r["file"])   # auto 生成图本来就是黑底带圈
        if args.zoom > 0:
            with Image.open(out / r["file"]) as im:
                big = im.convert("RGBA").resize(
                    (im.width * args.zoom, im.height * args.zoom), Image.NEAREST)
            big.save(out / f"{r['id']}@{args.zoom}x.png")
        if r["src"] == "auto":
            n_auto += 1
        else:
            n_off += 1
        if (i + 1) % 2000 == 0:
            print(f"  … {i + 1}/{len(rows)}")

    write_viewer(out, rows)
    (out / "index.json").write_text(
        json.dumps({"generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "count": len(rows), "official": n_off, "auto": n_auto,
                    "zoom": args.zoom,
                    "source": ".cache/minimap/ (nxmap mini-all + 预热兜底)",
                    "method": {"official": "WZ miniMap/canvas + 纯黑底 + pt=2 传送口蓝圈（上移 3px）",
                               "auto": "无背景渲染缩 1/2^mag → 黑底 → 只画 pt=2 传送口（上移 3px）"},
                    "portalRings": n_ring,
                    "portalLift": PORTAL_LIFT,
                    "portalRadius": PORTAL_RING,
                    "coord": {"scale": "scale = 2^mag",
                              "world_to_px": "px = (world + center) / scale",
                              "px_to_world": "world = px * scale - center"},
                    "maps": rows}, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n导出 {len(rows)} 张 → {out}")
    print(f"  official {n_off} + auto {n_auto}；传送门圈 {n_ring} 个（上移 {PORTAL_LIFT}px）")
    print("  index.json + data.js + index.html")
    if args.zoom > 0:
        print(f"  另有 {args.zoom}x 放大 PNG {len(rows)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
