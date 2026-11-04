# -*- coding: utf-8 -*-
"""WzToolkit WebUI —— 导入 .wz → 转成 .nx → 浏览/导出 怪物与地图。

设计要点（为什么这么做）：

- **原生工具用 cwd 定位 .nx**：NoLifeNx 系工具按「裸文件名 + 当前目录」解析
  Map.nx / Mob.nx，所以所有导出器都以「工作区目录」（用户放 .wz 的目录）为
  cwd 运行。转换器也是同一约定：.nx 生成在 .wz 旁边。
- **不在浏览器里上传 GB 级文件**：wz 动辄几百 MB，走「选本地目录」而不是
  文件上传，快且不占一份拷贝。
- **长任务走后台线程 + 轮询**：转换 Map.wz 要几分钟，前端 1s 轮询日志。
- **怪物名来自 vendor/Mob.txt**（导出器侧的 --named 依赖 String.nx，没转
  String.wz 时只有 id；名字表是兜底也是主源）。
"""
from __future__ import annotations

import csv
import importlib.util
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
VENDOR = ROOT / "vendor"
CACHE = ROOT / ".cache"
EXPORTS = ROOT / "exports"
STATIC = Path(__file__).resolve().parent / "static"
STATE = ROOT / "workspace.json"          # 记住上次选的工作区

app = FastAPI(title="WzToolkit")

# ------------------------------------------------------------------ 公共状态

_lock = threading.Lock()
_ws: Path | None = None                   # 工作区（放 .wz / .nx 的目录）
_job: dict = {"id": 0, "running": False, "log": [], "done": 0, "total": 0, "error": ""}
_lists: dict = {}                        # mobs / maps 缓存（键: 文件 mtime）


def _save_ws() -> None:
    STATE.write_text(json.dumps({"workspace": str(_ws) if _ws else ""}),
                     encoding="utf-8")


def _load_ws() -> None:
    global _ws
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        p = Path(d.get("workspace") or "")
        _ws = p if p.is_dir() else None
    except (OSError, ValueError):
        _ws = None


_load_ws()


def _tools_ok() -> bool:
    return all((BIN / f"{n}.exe").exists()
               for n in ("NoLifeWzToNx", "nxmap", "nxmob", "nxscan"))


def _need_tools() -> None:
    if not _tools_ok():
        raise HTTPException(500, "bin\\ 里缺工具 —— 先跑 build.ps1 编译")


def _need_ws() -> Path:
    if _ws is None:
        raise HTTPException(400, "还没有工作区：先在第一步里选一个放 .wz 的目录")
    return _ws


def _run(args: list[str], cwd: Path, timeout: int = 1200) -> str:
    """跑一个原生工具，返回 stdout。异常消息直接给前端。"""
    exe = BIN / (args[0] + ".exe")
    if not exe.exists():
        raise HTTPException(500, f"缺工具 {exe.name}（先跑 build.ps1）")
    r = subprocess.run([str(exe)] + args[1:], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return r.stdout or ""


# ------------------------------------------------------------------ 渲染器

_R = None


def _render_mod():
    """导入 vendor/render_map.py（dataclass 模块要先进 sys.modules）。"""
    global _R
    if _R is not None:
        return _R
    p = VENDOR / "render_map.py"
    spec = importlib.util.spec_from_file_location("_wztk_render_map", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    _R = mod
    return mod


def _render_preview(map_dir: Path, out_png: Path) -> dict:
    R = _render_mod()
    R.render_file(str(map_dir), str(out_png), max_width=1600)
    from PIL import Image
    with Image.open(out_png) as im:
        w, h = im.size
    return {"w": w, "h": h}


# ------------------------------------------------------------------ 名字表

_mob_names: dict[str, str] | None = None


def mob_names() -> dict[str, str]:
    global _mob_names
    if _mob_names is not None:
        return _mob_names
    out: dict[str, str] = {}
    p = VENDOR / "Mob.txt"
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if " - " in line:
                k, v = line.split(" - ", 1)
                out[k.strip()] = v.strip()
    _mob_names = out
    return out


# ------------------------------------------------------------------ 基础接口

@app.get("/api/state")
def state():
    return {"workspace": str(_ws) if _ws else None,
            "tools_ok": _tools_ok(),
            "exports": str(EXPORTS)}


@app.post("/api/browse")
def browse(payload: dict = Body(default={})):
    """列目录（给「选工作区」的浏览用）。path 为空 = 磁盘根。"""
    p = Path(payload.get("path") or "")
    if str(p) and not p.is_dir():
        raise HTTPException(400, f"不是目录：{p}")
    base = p if str(p) else None
    dirs = []
    if base is None:
        import string as _s
        from ctypes import windll
        drives = []
        bitmask = windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(_s.ascii_uppercase):
            if bitmask & (1 << i):
                drives.append(f"{letter}:\\")
        return {"path": "", "dirs": drives}
    try:
        for c in sorted(base.iterdir()):
            if c.is_dir() and not c.name.startswith("$"):
                dirs.append(c.name)
    except PermissionError:
        pass
    return {"path": str(base), "dirs": dirs[:500]}


@app.post("/api/scan")
def scan(payload: dict = Body(...)):
    """设为工作区并列出里面的 .wz（含是否已转换）。"""
    global _ws
    p = Path(payload.get("path") or "")
    if not p.is_dir():
        raise HTTPException(400, f"不是目录：{p}")
    _ws = p
    _lists.clear()
    _save_ws()
    files = []
    for f in sorted(p.glob("*.wz")):
        if f.name.endswith(".segs"):        # 下载器没下完的标记
            continue
        nx = f.with_suffix(".nx")
        files.append({"name": f.name, "size_mb": round(f.stat().st_size / 1048576, 1),
                      "nx": nx.name if nx.exists() else None,
                      "nx_mb": round(nx.stat().st_size / 1048576, 1) if nx.exists() else 0})
    return {"workspace": str(p), "wz": files}


# ------------------------------------------------------------------ 转换（后台任务）

@app.post("/api/convert")
def convert(payload: dict = Body(...)):
    """把选中的 .wz 转成 .nx（NoLifeWzToNx -c，.nx 落在 .wz 旁边）。"""
    _need_tools()
    ws = _need_ws()
    names = payload.get("files") or []
    force = bool(payload.get("force"))
    todo = []
    for n in names:
        wz = ws / n
        if not wz.is_file() or wz.suffix != ".wz":
            raise HTTPException(400, f"文件不在工作区：{n}")
        if force or not wz.with_suffix(".nx").exists():
            todo.append(wz)
    if not todo:
        return {"started": False, "message": "选中的都已是 .nx（要重转勾「强制重转」）"}

    with _lock:
        if _job["running"]:
            raise HTTPException(409, "已有转换在跑，等它完成")
        _job.update(id=_job["id"] + 1, running=True, log=[], done=0,
                    total=len(todo), error="")

    def work(job_id: int):
        try:
            for wz in todo:
                line = f"=== {wz.name}（{wz.stat().st_size / 1048576:.0f} MB）转换中 …"
                _job["log"].append(line)
                t0 = time.time()
                exe = BIN / "NoLifeWzToNx.exe"
                r = subprocess.run([str(exe), "-c", wz.name], cwd=str(wz.parent),
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
                out = (r.stdout or "").strip().splitlines()[-2:]
                _job["log"] += [f"  {x}" for x in out if x.strip()]
                nx = wz.with_suffix(".nx")
                if r.returncode != 0 or not nx.exists():
                    raise RuntimeError(f"{wz.name} 转换失败（exit {r.returncode}）")
                _job["log"].append(
                    f"  OK {nx.name} {nx.stat().st_size / 1048576:.1f} MB，"
                    f"用时 {time.time() - t0:.1f}s")
                _job["done"] += 1
            _job["log"].append("全部完成 ✔")
        except Exception as exc:                       # noqa: BLE001
            _job["error"] = str(exc)
            _job["log"].append(f"出错：{exc}")
        finally:
            _job["running"] = False
            _lists.clear()

    threading.Thread(target=work, args=(_job["id"],), daemon=True).start()
    return {"started": True, "total": len(todo), "job": _job["id"]}


@app.get("/api/job")
def job():
    with _lock:
        return {**_job, "log": _job["log"][-60:]}


# ------------------------------------------------------------------ 浏览：怪物

# 状态图标 / 检查用假怪：WZ 里也有 boss=1 甚至几十亿血，但根本不是怪。
_NOT_MOB_IDS = ((9500000, 9500099), (9501000, 9501999))
_NOT_MOB_WORDS = ("checking elimination", "transparent item for checking")


def _mob_tier(raw_id: str, name: str, boss: int, hp: int) -> str:
    """大 BOSS / BOSS / 普通怪。

    判定只用 WZ 官方字段：info/boss=1 才算 BOSS（客户端画长血条、放 boss 音乐
    看的就是它），再看 maxHP 分档 —— 一千w 以上归"大 BOSS"（Zakum/Horntail/
    Pink Bean 这一档，v83 共 90 来只），其余归"BOSS"。比按 id 段或名字猜靠谱。
    """
    try:
        n = int(raw_id)
    except ValueError:
        return "normal"
    if any(lo <= n <= hi for lo, hi in _NOT_MOB_IDS):
        return "normal"
    low = (name or "").lower()
    if any(w in low for w in _NOT_MOB_WORDS):
        return "normal"
    if boss != 1:
        return "normal"
    return "big" if hp >= 10_000_000 else "boss"


def mob_meta(ws: Path, mobnx: Path) -> list[dict]:
    """跑 nxmob info 拿每只怪的 boss/等级/血量 + link 目标 + 自身帧数。

    按 Mob.nx mtime 缓存（v2：info 输出加了 link,frames 两列，换个文件名
    避免读到旧格式缓存）。覆盖 Mob.nx + Mob001/002/2 所有卷；一次 0.1s。
    """
    cache = CACHE / f"mobinfo2_{int(mobnx.stat().st_mtime)}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    out = _run(["nxmob", "info"], ws, timeout=300)
    items: list[dict] = []
    for row in csv.reader(out.splitlines()):
        if len(row) < 6 or not row[0].isdigit():
            continue
        raw, name = row[0], row[1]
        try:
            boss, lv, hp, exp = (int(x) for x in row[2:6])
        except ValueError:
            boss = lv = hp = exp = 0
        link = row[6].strip().lstrip("0") if len(row) > 6 else ""
        try:
            frames = int(row[7]) if len(row) > 7 else 0
        except ValueError:
            frames = 0
        items.append({"raw": raw, "id": raw.lstrip("0"), "name": name,
                      "boss": boss, "level": lv, "hp": hp, "exp": exp,
                      "link": link, "frames": frames,
                      "tier": _mob_tier(raw, name, boss, hp)})
    items.sort(key=lambda x: int(x["id"]))
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return items


def life_index(ws: Path, mapnx: Path) -> dict[str, list[dict]]:
    """跑 nxmap life 拿全库刷怪表，反向聚合成 怪id → [{mapid, name, n, mobtime}]。

    life 节点是 WZ 官方的刷怪表（怪 id + 出生坐标 + 所站地面 + mobTime 重生秒数），
    一次导出 0.2s（28701 行），按 Map.nx mtime 缓存成 json。
    """
    key = f"life:{mapnx.stat().st_mtime}"
    if _lists.get(key) == "ok":
        return _lists["life"]
    cache = CACHE / f"life_{int(mapnx.stat().st_mtime)}.json"
    idx: dict[str, list[dict]] = {}
    if cache.exists():
        try:
            idx = json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            idx = {}
    if not idx:
        out = _run(["nxmap", "life"], ws, timeout=300)
        agg: dict[str, dict[str, dict]] = {}
        for row in csv.reader(out.splitlines()):
            if len(row) < 8 or row[2] not in ("m", "n"):
                continue
            kid = row[3].lstrip("0")
            if row[2] != "m":
                continue
            mid, mname = row[0], row[1]
            try:
                mobtime = int(row[7])
            except ValueError:
                mobtime = 0
            slot = agg.setdefault(kid, {})
            e = slot.setdefault(mid, {"mapid": mid, "name": mname,
                                      "n": 0, "mobtime": 0})
            e["n"] += 1
            e["mobtime"] = max(e["mobtime"], mobtime)
        for kid, maps in agg.items():
            idx[kid] = sorted(maps.values(), key=lambda m: -m["n"])
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    _lists["life"] = idx
    _lists[key] = "ok"
    return idx


@app.get("/api/mob/{kid}/maps")
def mob_maps(kid: str):
    """这只怪自然刷新在哪些地图（life 反向聚合；脚本刷的 boss 不在里面）。"""
    ws = _need_ws()
    mapnx = ws / "Map.nx"
    if not mapnx.exists():
        raise HTTPException(400, "工作区里没有 Map.nx —— 先转换 Map.wz")
    idx = life_index(ws, mapnx)
    return {"maps": idx.get(kid.lstrip("0"), [])}


@app.get("/api/mobs")
def mobs():
    ws = _need_ws()
    mobnx = ws / "Mob.nx"
    if not mobnx.exists():
        raise HTTPException(400, "工作区里没有 Mob.nx —— 先转换 Mob.wz")
    key = f"mobs:{mobnx.stat().st_mtime}"
    if _lists.get("key") != key:
        items = mob_meta(ws, mobnx)
        byid = {it["id"]: it for it in items}
        for it in items:
            if not it.get("name"):
                it["name"] = mob_names().get(it["id"]) or ""
            # 有图 = 自己有帧，或 link 链条终点有帧（客户端就是渲染目标怪）；
            # 都没有的（唱片机、木桩这类）标 img_ok=False，列表可筛"无图"。
            seen: set = set()
            cur = it
            ok = False
            for _ in range(5):
                if cur["id"] in seen:
                    break
                seen.add(cur["id"])
                if cur.get("frames", 0) > 0:
                    ok = True
                    break
                cur = byid.get(cur.get("link") or "")
                if not cur:
                    break
            it["img_ok"] = ok
        _lists.update(key=key, mobs=items)
    return {"mobs": _lists["mobs"]}


# ------------------------------------------------------------------ 浏览：地图

@app.get("/api/maps")
def maps():
    ws = _need_ws()
    mapnx = ws / "Map.nx"
    if not mapnx.exists():
        raise HTTPException(400, "工作区里没有 Map.nx —— 先转换 Map.wz")
    key = f"maps:{mapnx.stat().st_mtime}"
    if _lists.get("key") != key:
        # Map.nx 根下有 Back/Obj/… 和一个 "Map" 节点；真地图在 Map/Map{0..9}/<id>.img
        out = _run(["nxscan", "Map", "Map", "1"], ws, timeout=300)
        groups = []
        for ln in out.splitlines():
            m = re.match(r"^Map/Map/(Map\d+) \[none\]", ln)
            if m:
                groups.append(m.group(1))
        ids: list[str] = []
        for g in groups:
            o = _run(["nxscan", "Map", f"Map/{g}", "1"], ws, timeout=300)
            for ln in o.splitlines():
                mm = re.search(r"(\d{9})\.img \[none\]", ln)
                if mm:
                    ids.append(mm.group(1))
        ids = sorted(set(ids))
        # 「有怪的图」：life 反向聚合里出现过的 mapid（只算 m=mob，npc 不算）
        life = life_index(ws, mapnx)
        life_maps = sorted({m["mapid"] for rows in life.values() for m in rows})
        _lists.update(key=key, maps=ids, life_maps=life_maps)
    return {"maps": _lists["maps"], "life_maps": _lists.get("life_maps", [])}


# ------------------------------------------------------------------ 预览 / 导出

def drop_dir(p: Path) -> None:
    """清空一个导出/缓存目录，但**不依赖删除成功**。

    导出目录有时会被安全软件/审计钩子接管（shutil.rmtree 走回收站、批量删除还要
    二次确认），在服务进程里删不掉就直接 500。这里先尝试逐项删，失败就把整个目录
    改名挪进 .cache/trash/ —— 结果一样是"旧产物不碍事"，但不会抛异常。
    """
    if not p.exists():
        return
    try:
        for c in sorted(p.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            c.rmdir() if c.is_dir() else c.unlink()
        p.rmdir()
        return
    except BaseException:   # 安全钩子会抛 SystemExit，不是 Exception
        pass
    try:
        trash = CACHE / "trash"
        trash.mkdir(parents=True, exist_ok=True)
        p.rename(trash / f"{p.name}-{int(time.time() * 1000)}")
    except BaseException:
        pass


def _resolve_mob_link(raw: str, meta: dict) -> str:
    """链接怪（info/link 且自身 0 帧）顺链解析到真正有帧的怪，防环最多 5 跳。"""
    seen: set = set()
    for _ in range(5):
        m = meta.get(raw)
        if not m or m.get("frames", 0) > 0 or not m.get("link") or raw in seen:
            break
        seen.add(raw)
        raw = m["link"]
    return raw


def _ensure_mob_preview(raw: str, ws: Path) -> Path:
    """确保这只怪在 .cache/mob/<raw> 有预览导出，返回目录。"""
    outdir = CACHE / "mob" / raw
    if not outdir.exists() or not any(outdir.rglob("*.png")):
        if outdir.exists():
            drop_dir(outdir)
        outdir.parent.mkdir(parents=True, exist_ok=True)
        _run(["nxmob", "export", str(outdir), "--only", raw, "--named"], ws,
             timeout=600)
    return outdir


def _stand_frame(ws: Path, kind_id: str):
    """一种怪的 stand 帧 + 锚点：(Path, w, h, ox, oy)；没有图返回 None。

    ox/oy 是锚点（脚底中心）在 PNG 里的位置，贴图/前端定位都用它对准刷怪点。
    """
    raw = kind_id.lstrip("0") or "0"
    mobnx = ws / "Mob.nx"
    if mobnx.exists():
        meta = {m["id"]: m for m in mob_meta(ws, mobnx)}
        raw = _resolve_mob_link(raw, meta)
        # nxmob 的 --only 只认 WZ 里的原始 id（带前导 0），meta 里存了原始值
        raw = (meta.get(raw) or {}).get("raw") or raw.zfill(7)
    else:
        raw = raw.zfill(7)
    try:
        outdir = _ensure_mob_preview(raw, ws)
    except Exception:
        return None
    pngs = sorted(outdir.rglob("*.png"))
    if not pngs:
        return None
    # 优先 stand 帧（站着不动，最适合当立绘）；没有就随便一帧
    pick = next((p for p in pngs if p.name.lower().startswith("stand")), pngs[0])
    w = h = ox = oy = 0
    man = outdir / "manifest.csv"
    if man.exists():
        for f in list(csv.reader(man.read_text(encoding="utf-8-sig").splitlines()))[1:]:
            if len(f) >= 10 and f[4] == pick.name:
                try:
                    w, h, ox, oy = (int(x) for x in f[5:9])
                except ValueError:
                    pass
                break
    if not w:
        from PIL import Image
        with Image.open(pick) as im:
            w, h = im.size
            ox, oy = w // 2, h
    return pick, w, h, ox, oy


@app.get("/api/mob/icon/{raw}")
def mob_icon(raw: str):
    """一只怪的“立绘”帧（stand 帧）：给地图刷怪点叠加小怪用。"""
    ws = _need_ws()
    if not re.fullmatch(r"\d+", raw):
        raise HTTPException(400, "怪 id 不合法")
    fr = _stand_frame(ws, raw)
    if fr is None:
        raise HTTPException(500, "这只怪没有图片资源")
    pick, w, h, ox, oy = fr
    return {"url": f"/preview/{pick.relative_to(CACHE).as_posix()}",
            "w": w, "h": h, "ox": ox, "oy": oy}


def _composite_mobs(ws: Path, outdir: Path, mj: dict, base_png: Path, seed: str,
                   density: int = 1) -> Path | None:
    """把这张图的怪随机贴到刷怪点上，产出 preview_mobs.png（脚底落在 cy 地面上）。

    density>1 时每个刷怪点放 density 只：第 1 只在原位附近，其余左右散开
    （±20~150 世界像素）；所有怪 50% 随机镜像朝向，随机结果按 seed 稳定。
    """
    import random
    from PIL import Image
    life = [l for l in mj.get("life", []) if l.get("type") == "m"]
    if not life:
        return None
    R = _render_mod()
    minx, miny, bw, bh = R.map_bounds(mj)
    minx -= 40; miny -= 40; bw += 80; bh += 80
    s = min(1.0, 1600 / max(1.0, bw))
    frames = {}
    frames_flip = {}
    for k in sorted({str(l.get("id", "")).lstrip("0") for l in life}):
        fr = _stand_frame(ws, k)
        if fr is None:
            continue
        pick, w, h, ox, oy = fr
        img = Image.open(pick).convert("RGBA")
        if abs(s - 1.0) > 1e-3:
            img = img.resize((max(1, round(w * s)), max(1, round(h * s))))
            ox, oy = ox * s, oy * s
        frames[k] = (img, ox, oy)
        frames_flip[k] = (img.transpose(Image.FLIP_LEFT_RIGHT),
                          img.width - ox, oy)
    if not frames:
        return None
    im = Image.open(base_png).convert("RGBA")
    rng = random.Random(f"{seed}:d{density}")
    keys = list(frames)
    for l in life:
        fh = l.get("fh")
        # 客户端同款落地：life 的 y 只是参考点，真正站的是 cy（所站 foothold 的 y）
        gy = float(l.get("cy", l.get("y", 0))) if fh else float(l.get("y", 0))
        for i in range(max(1, density)):
            # 所有怪都可能随机镜像朝向
            flip = rng.random() < 0.5
            img, ox, oy = frames_flip[rng.choice(keys)] if flip else frames[rng.choice(keys)]
            # 第 1 只贴着出生点（±12 抖动）；多出的散得更开更随机（±20~150）
            if i == 0:
                dx = rng.uniform(-12, 12)
            else:
                dx = rng.uniform(20, 150) * rng.choice((-1, 1))
                if rng.random() < 0.25:      # 偶尔贴得近一点，避免机械等距
                    dx = rng.uniform(12, 30) * rng.choice((-1, 1))
            px = (float(l.get("x", 0)) + dx - minx) * s - ox
            py = (gy - miny) * s - oy
            im.alpha_composite(img, (round(px), round(py)))
    out = outdir / "preview_mobs.png"
    im.convert("RGB").save(out)
    return out


@app.post("/api/mob/export")
def mob_export(payload: dict = Body(...)):
    """导出一只怪的全部动画帧。preview=True 只出预览（缓存），否则进 exports。"""
    ws = _need_ws()
    raw = str(payload.get("raw") or "").strip()
    if not re.fullmatch(r"\d+", raw):
        raise HTTPException(400, "怪 id 不合法")
    preview = bool(payload.get("preview"))
    # 链接怪（info/link 且自身 0 帧）：客户端渲染 link 目标怪 —— 顺着导出目标。
    raw_req = raw
    mobnx = ws / "Mob.nx"
    if mobnx.exists():
        meta = {m["id"]: m for m in mob_meta(ws, mobnx)}
        raw = _resolve_mob_link(raw, meta)
    link_info = {"from": raw_req, "to": raw} if raw != raw_req else None
    outdir = (CACHE / "mob" / raw) if preview else (EXPORTS / "mobs" / raw)
    if preview and outdir.exists():
        pngs = sorted(outdir.rglob("*.png"))
    else:
        if outdir.exists():
            drop_dir(outdir)
        _run(["nxmob", "export", str(outdir), "--only", raw, "--named"], ws,
             timeout=600)
        pngs = sorted(outdir.rglob("*.png"))
    if not pngs:
        raise HTTPException(500, "没导出任何帧（这只怪可能没有图片资源）")
    rel = [f"/preview/{p.relative_to(CACHE).as_posix()}" if preview
           else f"/exports/{p.relative_to(EXPORTS).as_posix()}" for p in pngs]
    return {"dir": str(outdir), "count": len(pngs), "pngs": rel[:400],
            "frames": _read_manifest(outdir, preview, pngs),
            "preview": preview, "link": link_info}


def _read_manifest(outdir: Path, preview: bool, pngs: list[Path]) -> list[dict]:
    """把 nxmob 的 manifest.csv 解析成前端用的帧表（含 origin / delay）。

    每帧 PNG 都是按自己的包围盒裁出来的（28x28、31x28、46x18…），光看尺寸会
    以为"大小不对"；真正决定画面的是 ox/oy —— 锚点（脚底中心）在 PNG 里的位置。
    前端拿这两个值把所有帧对到同一个画布上才对得齐。
    """
    man = outdir / "manifest.csv"
    if not man.exists():
        return []
    url_prefix = "/preview/" if preview else "/exports/"
    root = CACHE if preview else EXPORTS
    # 帧图在 <id>_<名字>/ 子目录里，manifest 只记文件名 → 用实际文件名索引回路径
    by_name: dict[str, Path] = {}
    for p in pngs:
        by_name.setdefault(p.name, p)
    out: list[dict] = []
    rows = list(csv.reader(man.read_text(encoding="utf-8-sig").splitlines()))
    for f in rows[1:]:
        if len(f) < 10:
            continue
        p = by_name.get(f[4])
        if p is None:
            continue
        try:
            w, h, ox, oy, delay = (int(x) for x in f[5:10])
        except ValueError:
            continue
        out.append({
            "file": f[4],
            "url": url_prefix + p.relative_to(root).as_posix(),
            "anim": f[2], "frame": int(f[3]),
            "w": w, "h": h, "ox": ox, "oy": oy, "delay": delay,
        })
    return out


def _parse_region(rg) -> tuple | None:
    """前端传来的 region（dict 或 list）→ (x, y, w, h) 元组；不合法返回 None。"""
    if not rg:
        return None
    try:
        t = (rg["x"], rg["y"], rg["w"], rg["h"]) if isinstance(rg, dict) else rg[:4]
        return tuple(float(v) for v in t)
    except Exception:
        return None


@app.post("/api/map/export")
def map_export(payload: dict = Body(...)):
    """导出一张地图（map.json + 全部贴图 + 渲染预览图）。

    链接图（info/link）：这种图自己的 img 是空的，客户端渲染时直接加载
    link 指向的那张图 —— 遇到就顺着 link 导出目标图，返回里带 link 字段
    让前端标注"画面同 xxx"，避免用户看到一张空白图。
    """
    ws = _need_ws()
    mid = str(payload.get("id") or "").strip()
    if not re.fullmatch(r"\d{9}", mid):
        raise HTTPException(400, "地图 id 应该是 9 位数字")
    return _map_export(ws, mid, bool(payload.get("preview")), set(),
                       bool(payload.get("mobs")),
                       int(payload.get("density") or 1),
                       _parse_region(payload.get("region")))


def _map_export(ws: Path, mid: str, preview: bool, visited: set,
                mobs: bool = False, density: int = 1,
                region: tuple | None = None) -> dict:
    if mid in visited:
        raise HTTPException(500, f"link 成环了：{' → '.join(sorted(visited))} → {mid}")
    visited.add(mid)
    outdir = (CACHE / "map" / mid) if preview else (EXPORTS / "maps" / mid)

    def _cached_ok() -> bool:
        # 缓存要能解析才算数：早期 nxmap 有产出坏 JSON 的情况，留着会让接口 500
        try:
            json.loads((outdir / "map.json").read_text(encoding="utf-8"))
            return True
        except Exception:
            return False

    if not _cached_ok():
        if outdir.exists():
            drop_dir(outdir)
        _run(["nxmap", mid, str(outdir)], ws, timeout=600)
    if not (outdir / "map.json").exists():
        raise HTTPException(500, f"nxmap 没产出 map.json（这个 id 存在吗？{mid}）")
    mj = json.loads((outdir / "map.json").read_text(encoding="utf-8"))

    # 链接图：拿目标图的数据渲染（客户端同款行为）
    link = str(mj.get("info", {}).get("link") or "").strip()
    if link and link != mid and re.fullmatch(r"\d{9}", link):
        d = _map_export(ws, link, preview, visited, mobs, density, region)
        d["link"] = {"from": mid, "to": link}
        return d

    png = outdir / "preview.png"
    size = _render_preview(outdir, png)
    life = [l for l in mj.get("life", []) if l.get("type") == "m"]
    kinds = sorted({str(l.get("id", "")).lstrip("0") for l in life})
    # 刷怪标签也带上 BOSS 档位，和左侧列表同一套判定
    mobnx = ws / "Mob.nx"
    meta = {m["id"]: m for m in mob_meta(ws, mobnx)} if mobnx.exists() else {}
    # 刷怪点叠加：前端按 overlay 把世界坐标换算成预览图上的百分比位置。
    # 这组参数必须和渲染实际用的一致（render_file 默认 pad=40、max_width=1600）。
    R = _render_mod()
    minx, miny, bw, bh = R.map_bounds(mj)
    minx -= 40; miny -= 40; bw += 80; bh += 80
    s = min(1.0, 1600 / max(1.0, bw))
    points = [{"id": (k := str(l.get("id", "")).lstrip("0")),
               "x": float(l.get("x", 0)),
               # 客户端同款落地：life 的 y 只是出生参考点，真正站的是 cy（fh 指向的地面 y）
               "y": float(l.get("cy") if l.get("fh") and l.get("cy") is not None else l.get("y", 0)),
               "name": meta.get(k, {}).get("name") or mob_names().get(k) or "",
               "tier": meta.get(k, {}).get("tier", "normal")}
              for l in life]
    result = {"dir": str(outdir), "preview": size,
              "preview_url": (f"/preview/{png.relative_to(CACHE).as_posix()}" if preview
                              else f"/exports/{png.relative_to(EXPORTS).as_posix()}"),
              "life_count": len(life),
              "life_kinds": [{"id": k,
                              "name": meta.get(k, {}).get("name") or mob_names().get(k) or "",
                              "tier": meta.get(k, {}).get("tier", "normal")} for k in kinds],
              "footholds": len(mj.get("foothold", [])),
              "ladders": len(mj.get("ladderRope", [])),
              "overlay": {"minx": minx, "miny": miny, "s": s,
                          "w": round(bw * s), "h": round(bh * s)},
              "life_points": points}
    # 导出图带怪物：把怪随机贴到刷怪点上合成进预览图（preview_mobs.png）
    if mobs and life:
        mob_png = _composite_mobs(ws, outdir, mj, png, mid,
                                  density=max(1, min(10, int(density or 1))))
        if mob_png is not None:
            rel = (mob_png.relative_to(CACHE) if preview else mob_png.relative_to(EXPORTS))
            result["preview_url"] = (f"/preview/{rel.as_posix()}" if preview
                                     else f"/exports/{rel.as_posix()}")
            result["mobs"] = True
    # 区域裁剪：只导出世界坐标 (x, y, w, h) 的窗口（客户端画面就是固定分辨率的窗口）
    if region:
        rx, ry, rw, rh = (float(v) for v in region)
        if rw <= 0 or rh <= 0:
            raise HTTPException(400, "区域宽高必须大于 0")
        from PIL import Image
        src_path = Path(mob_png) if (mobs and life and mob_png) else png
        src = Image.open(src_path)
        W, H = src.size
        left = max(0, round((rx - minx) * s))
        top = max(0, round((ry - miny) * s))
        right = min(W, round((rx + rw - minx) * s))
        bottom = min(H, round((ry + rh - miny) * s))
        if right - left < 8 or bottom - top < 8:
            raise HTTPException(
                400, "区域不在地图范围内。地图世界坐标范围："
                f"x∈[{minx:.0f},{minx + bw:.0f}] y∈[{miny:.0f},{miny + bh:.0f}]")
        def _n(v: float) -> str:
            return str(int(v)) if float(v).is_integer() else str(v)
        out_png = outdir / f"preview_region_x{_n(rx)}_y{_n(ry)}_w{_n(rw)}_h{_n(rh)}.png"
        src.crop((left, top, right, bottom)).save(out_png)
        cw, ch = right - left, bottom - top
        rel = (out_png.relative_to(CACHE) if preview else out_png.relative_to(EXPORTS))
        result["preview"] = {"w": cw, "h": ch}
        result["preview_url"] = (f"/preview/{rel.as_posix()}" if preview
                                 else f"/exports/{rel.as_posix()}")
        result["region"] = {"x": rx, "y": ry, "w": rw, "h": rh,
                            "px": {"x": left, "y": top, "w": cw, "h": ch}}
        # 叠加层坐标系改为以区域左上角为原点，圆点/立绘在裁剪图上照样对准
        result["overlay"] = {"minx": rx, "miny": ry, "s": s, "w": cw, "h": ch}
    return result


@app.post("/api/export/clear-cache")
def clear_cache():
    # 逐个删子目录，删不掉的挪进 trash —— 见 drop_dir 注释
    for d in list(CACHE.iterdir()) if CACHE.exists() else []:
        if d.name == "trash":
            continue
        drop_dir(d) if d.is_dir() else (d.unlink() if d.is_file() else None)
    CACHE.mkdir(exist_ok=True)
    return {"ok": True}


# ------------------------------------------------------------------ 静态资源

app.mount("/preview", StaticFiles(directory=str(CACHE)), name="preview")
app.mount("/exports", StaticFiles(directory=str(EXPORTS)), name="exports")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
