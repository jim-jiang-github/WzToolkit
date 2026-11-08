"""批量导出「自动生成的兜底小地图」为一套可交付的图鉴目录。

数据源：`.cache/minimap/index.json` 里 `auto=1` 的条目——即 WZ 源数据里
`miniMap/canvas` 退化成 1x1 占位、由 `_minimap_fallback_one()` 按客户端
UIMiniMap 几何重新生成的那些图（无背景、黑底、只画 pt=2 传送口）。

产出（默认 .exports/minimap-demo/）：
    <id>.png         原始 canvas 尺寸
    <id>@4x.png      4 倍最近邻放大，便于肉眼查看
    index.json       坐标映射表（世界坐标 <-> 小地图像素）
    index.html       图鉴总览页（搜索 / 点击看大图，可直接双击打开）

用法：
    PYTHONPATH=.pylibs .pyruntime\\python.exe scripts\\export_minimaps.py
    ... --out .exports/minimap-demo --zoom 4 --only 000030001,102030000
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / ".pylibs") not in sys.path:
    sys.path.insert(0, str(ROOT / ".pylibs"))

from PIL import Image  # noqa: E402

MMIDX = ROOT / ".cache" / "minimap"
MAPCACHE = ROOT / ".cache" / "map"


def collect(only: list[str] | None) -> list[dict]:
    idx = json.loads((MMIDX / "index.json").read_text(encoding="utf-8"))
    rows: list[dict] = []
    for mid, v in sorted(idx.items()):
        if not v.get("auto"):
            continue
        if only and mid not in only:
            continue
        png = MMIDX / f"{mid}.png"
        if not png.exists():
            continue
        with Image.open(png) as im:
            cw, ch = im.size
        if cw <= 1 and ch <= 1:
            continue
        mag = int(v.get("mag") or 0)
        scale = float(2 ** mag)
        cx, cy = float(v.get("centerX") or 0), float(v.get("centerY") or 0)
        # 世界尺寸取自 map.json 的 miniMap.w/h（canvas 像素 x 覆盖整张图）
        ww = round(cw * scale)
        wh = round(ch * scale)
        portals = None
        mjf = MAPCACHE / mid / "map.json"
        if mjf.exists():
            try:
                mm = json.loads(mjf.read_text(encoding="utf-8")).get("miniMap") or {}
                ww = int(mm.get("w") or ww)
                wh = int(mm.get("h") or wh)
                portals = sum(1 for p in json.loads(
                    mjf.read_text(encoding="utf-8")).get("portal", []) if p.get("pt") == 2)
            except Exception:                  # noqa: BLE001
                pass
        rows.append({
            "id": mid,
            "name": v.get("name") or "",
            "street": v.get("street") or "",
            "file": f"{mid}.png",
            "file4x": "",                  # 由 main() 按 --zoom 填
            "cw": cw, "ch": ch,
            "mag": mag, "scale": scale,
            "centerX": cx, "centerY": cy,
            "worldWidth": ww, "worldHeight": wh,
            "worldBounds": [-cx, -cy, ww - cx, wh - cy],
            "portals": portals,
        })
    return rows


def write_html(out: Path, rows: list[dict], zoom: int) -> None:
    cards = []
    for r in rows:
        cards.append(f"""    <a class="card" href="{r['file4x']}" target="_blank"
       data-k="{r['id']} {r['name']} {r['street']}">
      <div class="shot"><img src="{r['file4x']}" alt="{r['name']}" loading="lazy"></div>
      <div class="meta">
        <div class="nm">{r['name'] or '（无名）'}</div>
        <div class="st">{r['street'] or '—'}</div>
        <div class="dim"><code>{r['id']}</code> · {r['cw']}×{r['ch']}
          <span class="tag">1/{2 ** r['mag']}</span>
          <span class="tag">{r['portals'] if r['portals'] is not None else '?'} 传送</span></div>
      </div>
    </a>""")
    total_w = sum(r["cw"] for r in rows)
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
  input {{ flex: 1 1 240px; min-width: 160px; padding: 7px 11px; border-radius: 7px;
           border: 1px solid #2a323d; background: #151a21; color: #e6edf7; outline: none; }}
  input:focus {{ border-color: #3d7dd8; }}
  .hint {{ color: #6b7889; font-size: 12px; }}
  main {{ display: grid; gap: 14px; padding: 18px 20px 40px;
          grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); }}
  .card {{ text-decoration: none; color: inherit; background: #151a21;
           border: 1px solid #222933; border-radius: 10px; overflow: hidden;
           transition: border-color .15s, transform .15s; }}
  .card:hover {{ border-color: #3d7dd8; transform: translateY(-2px); }}
  .shot {{ display: grid; place-items: center; min-height: 120px; padding: 12px;
           background:
             linear-gradient(45deg, #0b0e12 25%, transparent 25%, transparent 75%, #0b0e12 75%) 0 0/16px 16px,
             linear-gradient(45deg, #0b0e12 25%, transparent 25%, transparent 75%, #0b0e12 75%) 8px 8px/16px 16px,
             #101419; }}
  .shot img {{ max-width: 100%; image-rendering: pixelated; display: block; }}
  .meta {{ padding: 9px 12px 11px; border-top: 1px solid #1d232c; }}
  .nm {{ font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden;
         text-overflow: ellipsis; }}
  .st {{ color: #7c8a9c; font-size: 11.5px; margin-top: 1px; }}
  .dim {{ color: #8b98a9; font-size: 11px; margin-top: 6px; display: flex; gap: 6px;
          align-items: center; flex-wrap: wrap; }}
  .dim code {{ color: #9fd0ff; }}
  .tag {{ background: #1b2431; border: 1px solid #263244; border-radius: 4px;
          padding: 0 5px; color: #93a7c0; }}
  .empty {{ grid-column: 1/-1; color: #6b7889; padding: 30px; text-align: center; }}
</style>
</head>
<body>
<header>
  <h1>小地图图鉴 · {len(rows)} 张</h1>
  <div class="sub">源数据 canvas 退化为 1×1 的地图，按客户端几何重新生成：无背景 · 黑底 · 只标传送点。
      坐标映射见 <code>index.json</code>：画布像素 = (世界坐标 + center) / 2<sup>mag</sup></div>
  <div class="bar">
    <input id="q" placeholder="搜索 地图名 / 街道 / id …" autocomplete="off">
    <span class="hint" id="cnt">{len(rows)} / {len(rows)}</span>
  </div>
</header>
<main id="grid">
{chr(10).join(cards)}
  <div class="empty" id="empty" hidden>没有匹配的地图</div>
</main>
<script>
const q = document.getElementById('q'), cards = [...document.querySelectorAll('.card')];
const cnt = document.getElementById('cnt'), empty = document.getElementById('empty');
q.addEventListener('input', () => {{
  const k = q.value.trim().toLowerCase();
  let n = 0;
  for (const c of cards) {{
    const hit = !k || c.dataset.k.toLowerCase().includes(k);
    c.hidden = !hit; if (hit) n++;
  }}
  cnt.textContent = n + ' / {len(rows)}';
  empty.hidden = n > 0;
}});
</script>
</body>
</html>
"""
    (out / "index.html").write_text(html, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="批量导出自动兜底小地图 + 映射表 + 图鉴页")
    ap.add_argument("--out", default=str(ROOT / ".exports" / "minimap-demo"))
    ap.add_argument("--zoom", type=int, default=4, help="放大预览倍数（最近邻，默认 4）")
    ap.add_argument("--only", default="", help="只导这些 id，逗号分隔")
    args = ap.parse_args()

    only = [s.strip() for s in args.only.split(",") if s.strip()] or None
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*"):                  # 旧产物一次性覆盖，避免残留
        if old.is_file():
            old.unlink()

    rows = collect(only)
    if not rows:
        print("没有可导出的自动小地图（先让服务预热生成）")
        return 1

    zoom = max(1, args.zoom)
    for r in rows:
        r["file4x"] = f"{r['id']}@{zoom}x.png"
        src = MMIDX / r["file"]
        shutil.copy2(src, out / r["file"])
        with Image.open(src) as im:
            big = im.convert("RGBA").resize(
                (im.width * zoom, im.height * zoom), Image.NEAREST)
        big.save(out / r["file4x"])
        print(f"  {r['id']} {r['name']} {r['cw']}x{r['ch']}")

    payload = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "zoom": zoom,
        "source": ".cache/minimap/index.json (auto=1)",
        "method": ("渲染地图(no_bg) → 缩到 1/2^mag 贴进 (minx+centerX, miny+centerY)/2^mag "
                   "→ 黑底合成 → 只画 pt=2 传送口蓝圈"),
        "coord": {
            "scale": "scale = 2^mag",
            "world_to_px": "px = (world + center) / scale",
            "px_to_world": "world = px * scale - center",
            "note": "px 是 canvas 像素坐标（左上原点）；world 是地图世界坐标",
        },
        "maps": rows,
    }
    (out / "index.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    write_html(out, rows, zoom)

    tot = sum(r["cw"] * r["ch"] for r in rows)
    print(f"\n导出 {len(rows)} 张 → {out}")
    print(f"  原始 PNG {len(rows)} 个 + {zoom}x 预览 {len(rows)} 个 + index.json + index.html")
    print(f"  canvas 总像素 {tot}，平均 {tot // len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
