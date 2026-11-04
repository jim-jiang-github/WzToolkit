# -*- coding: utf-8 -*-
"""wztool —— WzToolkit 命令行工具（CLI-first，供人类与其他程序/AI 直接调用）。

功能：导入 .wz → 转成 .nx → 列出/导出 怪物动画帧与地图（JSON/贴图/渲染预览）。
所有子命令都支持 --json 机器可读输出；退出码：0 成功，1 业务错误，2 参数错误。

常用示例::

    wztool workspace "D:/MapleStory/nxrun"        # 1. 设置工作区（放 .wz/.nx 的目录）
    wztool convert Mob.wz Map.wz                  # 2. wz → nx（耗时任务，同步执行）
    wztool mobs --json                            # 3. 列出全部怪物（JSON）
    wztool mob-export 0100100                     # 4. 导出一只怪的全部动画帧
    wztool map-export 000050000 --mobs            # 5. 导出地图（预览图带怪物）
    wztool web                                    # 6. 启动 Web 页面（人看，可选）

坐标与文件格式与客户端一致：怪物帧 PNG 带锚点(ox,oy=脚底中心)，
地图 map.json 含刷怪表(life)/地面(foothold)/绳梯(ladderRope)。
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------- 路径与常量

if getattr(sys, "frozen", False):              # PyInstaller 打包后：exe 旁就是项目
    ROOT = Path(sys.executable).resolve().parent
    _ME = getattr(sys, "_MEIPASS", None)        # 打包进去的纯资源走临时解压目录
    VENDOR = Path(_ME) / "vendor" if _ME and (Path(_ME) / "vendor").exists() else ROOT / "vendor"
else:
    ROOT = Path(__file__).resolve().parent
    VENDOR = ROOT / "vendor"

BIN = ROOT / "bin"
CACHE = ROOT / ".cache"
EXPORTS = ROOT / "exports"
STATIC = ROOT / "static"
STATE = ROOT / "workspace.json"                # 记住上次选的工作区

TOOLS = ("NoLifeWzToNx", "nxmap", "nxmob", "nxscan")


class CliError(Exception):
    """业务错误：message 给人/AI 看，code 是建议的退出码。"""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 公共状态

_lock = threading.Lock()
_ws: Path | None = None                        # 工作区（放 .wz / .nx 的目录）
_job: dict = {"id": 0, "running": False, "log": [], "done": 0, "total": 0, "error": ""}
_lists: dict = {}                              # mobs / maps / life 缓存（键: 文件 mtime）


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


def tools_ok() -> bool:
    return all((BIN / f"{n}.exe").exists() for n in TOOLS)


def _need_tools() -> None:
    if not tools_ok():
        raise CliError("bin\\ 里缺工具（NoLifeWzToNx/nxmap/nxmob/nxscan）—— 先跑 build.ps1 编译", 1)


def _need_ws() -> Path:
    if _ws is None:
        raise CliError("还没有设置工作区：先运行 wztool workspace <放 .wz 的目录>", 1)
    return _ws


def _run(args: list[str], cwd: Path, timeout: int = 1200) -> str:
    """跑一个原生工具（以工作区为 cwd，工具按 cwd 定位 .nx），返回 stdout。"""
    exe = BIN / (args[0] + ".exe")
    if not exe.exists():
        raise CliError(f"缺工具 {exe.name}（先跑 build.ps1）", 1)
    r = subprocess.run([str(exe)] + args[1:], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return r.stdout or ""


# ---------------------------------------------------------------- 渲染器

_R = None


def _render_mod():
    """导入 vendor/render_map.py（dataclass 模块要先进 sys.modules）。"""
    global _R
    if _R is not None:
        return _R
    p = VENDOR / "render_map.py"
    if not p.exists():
        raise CliError(f"缺渲染器 {p}", 1)
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


# ---------------------------------------------------------------- 名字表

_mob_names: dict[str, str] | None = None


def mob_names() -> dict[str, str]:
    """怪物中文名兜底表（vendor/Mob.txt，"id - 名字" 每行一条）。"""
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


# ---------------------------------------------------------------- 核心功能
# 每个函数返回 dict（即 --json 的输出 / web API 的响应体），错误抛 CliError。

def c_state() -> dict:
    return {"workspace": str(_ws) if _ws else None,
            "tools_ok": tools_ok(),
            "exports": str(EXPORTS)}


def c_browse(path: str = "") -> dict:
    """列目录（选工作区用）。path 为空 = 磁盘根。"""
    p = Path(path or "")
    if path and not p.is_dir():
        raise CliError(f"不是目录：{p}", 2)
    if not path:
        import string as _s
        from ctypes import windll
        drives = []
        bitmask = windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(_s.ascii_uppercase):
            if bitmask & (1 << i):
                drives.append(f"{letter}:\\")
        return {"path": "", "dirs": drives}
    dirs = []
    try:
        for c in sorted(p.iterdir()):
            if c.is_dir() and not c.name.startswith("$"):
                dirs.append(c.name)
    except PermissionError:
        pass
    return {"path": str(p), "dirs": dirs[:500]}


def c_workspace(path: str) -> dict:
    """设为工作区并列出里面的 .wz（含是否已转 .nx）。"""
    global _ws
    p = Path(path or "")
    if not p.is_dir():
        raise CliError(f"不是目录：{p}", 2)
    _ws = p
    _lists.clear()
    try:
        _save_ws()
    except OSError as e:
        raise CliError(f"工作区已设置但没能写入 {STATE.name}: {e}", 1)
    files = []
    for f in sorted(p.glob("*.wz")):
        if f.name.endswith(".segs"):           # 下载器没下完的标记
            continue
        nx = f.with_suffix(".nx")
        files.append({"name": f.name, "size_mb": round(f.stat().st_size / 1048576, 1),
                      "nx": nx.name if nx.exists() else None,
                      "nx_mb": round(nx.stat().st_size / 1048576, 1) if nx.exists() else 0})
    return {"workspace": str(p), "wz": files}


# 转换（wz→nx）：CLI 同步执行；web 后台线程 + job 轮询。核心都是这个 work()。

def _convert_pick(ws: Path, names: list[str], force: bool) -> list[Path]:
    _need_tools()
    todo = []
    for n in names:
        wz = ws / n
        if not wz.is_file() or wz.suffix != ".wz":
            raise CliError(f"文件不在工作区：{n}", 2)
        if force or not wz.with_suffix(".nx").exists():
            todo.append(wz)
    return todo


def _convert_one(wz: Path, log) -> None:
    log(f"=== {wz.name}（{wz.stat().st_size / 1048576:.0f} MB）转换中 …")
    t0 = time.time()
    exe = BIN / "NoLifeWzToNx.exe"
    r = subprocess.run([str(exe), "-c", wz.name], cwd=str(wz.parent),
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    for x in (r.stdout or "").strip().splitlines()[-2:]:
        if x.strip():
            log(f"  {x}")
    nx = wz.with_suffix(".nx")
    if r.returncode != 0 or not nx.exists():
        raise RuntimeError(f"{wz.name} 转换失败（exit {r.returncode}）")
    log(f"  OK {nx.name} {nx.stat().st_size / 1048576:.1f} MB，用时 {time.time() - t0:.1f}s")


def c_convert(names: list[str], force: bool, log) -> dict:
    """把 .wz 转成 .nx。CLI 同步跑；web 传一个往 _job 里记日志的 log。"""
    ws = _need_ws()
    todo = _convert_pick(ws, names, force)
    if not todo:
        return {"started": False, "total": 0,
                "message": "选中的都已是 .nx（要重转加 --force）"}
    for wz in todo:
        _convert_one(wz, log)
    return {"started": True, "total": len(todo), "done": len(todo)}


def c_job() -> dict:
    with _lock:
        return {**_job, "log": _job["log"][-60:]}


# 怪物元数据 ---------------------------------------------------------------

# 状态图标 / 检查用假怪：WZ 里也有 boss=1 甚至几十亿血，但根本不是怪。
_NOT_MOB_IDS = ((9500000, 9500099), (9501000, 9501999))
_NOT_MOB_WORDS = ("checking elimination", "transparent item for checking")


def _mob_tier(raw_id: str, name: str, boss: int, hp: int) -> str:
    """big(大BOSS) / boss / normal。boss=1 是 WZ 官方标记；maxHP≥1000w 归大 BOSS。"""
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
    """跑 nxmob info 拿每只怪的 boss/等级/血量 + link 目标 + 自身帧数（按 mtime 缓存）。"""
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
    """全库刷怪表，反向聚合 怪id → [{mapid, name, n, mobtime}]（按 Map.nx mtime 缓存）。"""
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
            if len(row) < 8 or row[2] != "m":   # 只算怪物（m），NPC(n) 不算
                continue
            kid = row[3].lstrip("0")
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


def c_mobs() -> dict:
    ws = _need_ws()
    mobnx = ws / "Mob.nx"
    if not mobnx.exists():
        raise CliError("工作区里没有 Mob.nx —— 先转换 Mob.wz", 1)
    key = f"mobs:{mobnx.stat().st_mtime}"
    if _lists.get("key") != key:
        items = mob_meta(ws, mobnx)
        byid = {it["id"]: it for it in items}
        for it in items:
            if not it.get("name"):
                it["name"] = mob_names().get(it["id"]) or ""
            # 有图 = 自己有帧，或 link 链条终点有帧（客户端就是渲染目标怪）
            seen: set = set()
            cur, ok = it, False
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


def c_mob_maps(kid: str) -> dict:
    ws = _need_ws()
    mapnx = ws / "Map.nx"
    if not mapnx.exists():
        raise CliError("工作区里没有 Map.nx —— 先转换 Map.wz", 1)
    idx = life_index(ws, mapnx)
    return {"maps": idx.get(kid.lstrip("0"), [])}


def c_maps() -> dict:
    ws = _need_ws()
    mapnx = ws / "Map.nx"
    if not mapnx.exists():
        raise CliError("工作区里没有 Map.nx —— 先转换 Map.wz", 1)
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


# 导出 ----------------------------------------------------------------------

def drop_dir(p: Path) -> None:
    """清空一个导出/缓存目录，不依赖删除成功：删不掉就整体挪进 .cache/trash/。"""
    if not p.exists():
        return
    try:
        for c in sorted(p.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            c.rmdir() if c.is_dir() else c.unlink()
        p.rmdir()
        return
    except BaseException:
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
    """确保这只怪在 .cache/mob/<raw> 有帧导出，返回目录。"""
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

    ox/oy 是锚点（脚底中心）在 PNG 里的位置，贴图/定位都用它对准刷怪点。
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


def c_mob_icon(raw: str) -> dict:
    """一只怪的「立绘」（stand 帧）：给地图刷怪点叠加小怪用。"""
    ws = _need_ws()
    if not re.fullmatch(r"\d+", raw):
        raise CliError("怪 id 不合法（应为数字）", 2)
    fr = _stand_frame(ws, raw)
    if fr is None:
        raise CliError("这只怪没有图片资源", 1)
    pick, w, h, ox, oy = fr
    return {"path": str(pick),
            "url": f"/preview/{pick.relative_to(CACHE).as_posix()}",
            "w": w, "h": h, "ox": ox, "oy": oy}


def _composite_mobs(ws: Path, outdir: Path, mj: dict, base_png: Path, seed: str,
                   density: int = 1) -> Path | None:
    """把这张图的怪随机贴到地面上，产出 preview_mobs.png（脚底落在 foothold 地面）。

    每个怪在「随机游走区间」内取随机 x：优先用 life 自带 rx0~rx1（客户端巡逻区间），
    没有就按图宽 ±8% 兜底；y 由 foothold 段线性插值得出（同一段里取离出生地面最近
    的，保证贴地不悬空），落点悬空时退回出生点附近。所有怪 50% 随机镜像朝向，
    随机结果按 seed 稳定（同 seed 同布局）。
    """
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
    fhs = [(float(f["x1"]), float(f["y1"]), float(f["x2"]), float(f["y2"]))
           for f in mj.get("foothold", [])]

    def _ground(x: float, y_ref: float) -> float | None:
        """x 处的地面 y：含 x 的 foothold 段里取离 y_ref 最近的一条（线性插值）。"""
        best_d = None
        best_y = None
        for x1, y1, x2, y2 in fhs:
            lo, hi = (x1, x2) if x1 <= x2 else (x2, x1)
            if hi > lo and lo <= x <= hi:
                y = y1 + (y2 - y1) * (x - x1) / (x2 - x1)
                d = abs(y - y_ref)
                if best_d is None or d < best_d:
                    best_d, best_y = d, y
        return best_y

    for l in life:
        bx = float(l.get("x", 0))
        # 客户端同款落地：life 的 y 只是参考点，真正站的是 cy（所站 foothold 的 y）
        base_y = float(l.get("cy", l.get("y", 0))) if l.get("fh") else float(l.get("y", 0))
        # 随机游走区间：客户端 rx0~rx1（巡逻范围），缺省按图宽 ±8% 兜底
        rx0, rx1 = l.get("rx0"), l.get("rx1")
        if rx0 is not None and rx1 is not None and float(rx1) > float(rx0):
            lo, hi = float(rx0), float(rx1)
        else:
            spread = max(120.0, bw * 0.08)
            lo, hi = bx - spread, bx + spread
        for _ in range(max(1, density)):
            # 所有怪都可能随机镜像朝向
            flip = rng.random() < 0.5
            img, ox, oy = frames_flip[rng.choice(keys)] if flip else frames[rng.choice(keys)]
            x = rng.uniform(lo, hi)
            gy = _ground(x, base_y)
            if gy is None:            # 落点下面没有地面（悬崖外）→ 退回出生点附近
                x = bx + rng.uniform(-15, 15)
                gy = _ground(x, base_y)
                if gy is None:
                    gy = base_y
            px = (x - minx) * s - ox
            py = (gy - miny) * s - oy
            im.alpha_composite(img, (round(px), round(py)))
    out = outdir / "preview_mobs.png"
    im.convert("RGB").save(out)
    return out


def _read_manifest(outdir: Path, root: Path, url_prefix: str, pngs: list[Path]) -> list[dict]:
    """把 nxmob 的 manifest.csv 解析成帧表（含 origin/锚点/延迟）。

    每帧 PNG 都按自己的包围盒裁出来（28x28、31x28…），画面位置由 ox/oy 决定。
    """
    man = outdir / "manifest.csv"
    if not man.exists():
        return []
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
        out.append({"file": f[4],
                    "path": str(p),
                    "url": url_prefix + p.relative_to(root).as_posix(),
                    "anim": f[2], "frame": int(f[3]),
                    "w": w, "h": h, "ox": ox, "oy": oy, "delay": delay})
    return out


def c_mob_export(raw: str, preview: bool = False) -> dict:
    """导出一只怪的全部动画帧 PNG（逐帧，含锚点 manifest）。

    preview=True 只出预览（.cache，重复调用秒回）；默认导出到 exports/mobs/。
    链接怪（info/link）自动顺链导出目标怪。
    """
    ws = _need_ws()
    raw = str(raw).strip()
    if not re.fullmatch(r"\d+", raw):
        raise CliError("怪 id 不合法（应为数字）", 2)
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
        outdir.parent.mkdir(parents=True, exist_ok=True)
        _run(["nxmob", "export", str(outdir), "--only", raw, "--named"], ws,
             timeout=600)
        pngs = sorted(outdir.rglob("*.png"))
    if not pngs:
        raise CliError("没导出任何帧（这只怪可能没有图片资源）", 1)
    root = CACHE if preview else EXPORTS
    prefix = "/preview/" if preview else "/exports/"
    return {"dir": str(outdir), "count": len(pngs),
            "pngs": [prefix + p.relative_to(root).as_posix() for p in pngs[:400]],
            "png_paths": [str(p) for p in pngs],
            "frames": _read_manifest(outdir, root, prefix, pngs),
            "preview": preview, "link": link_info}


def c_map_export(mid: str, preview: bool = False, mobs: bool = False,
                 density: int = 1, region: tuple | None = None,
                 seed: str | None = None,
                 _visited: set | None = None) -> dict:
    """导出一张地图：map.json（life/foothold/ladderRope）+ 全部贴图 + 渲染预览图。

    链接图（info/link，自己的 img 是空的）自动顺链导出目标图，返回带 link 字段。
    mobs=True 时预览图把怪随机画到地面上（preview_mobs.png，沿 rx 巡逻区间散布、
    foothold 落地）；seed 缺省每次随机，传同值可复现同一布局。
    life_points 是刷怪点（x, y 已按客户端规则落到 cy 地面），overlay 是
    世界坐标→预览图的变换（minx/miny/s/w/h），两者配合可在任何客户端复现点位。
    foothold_segs 是全部地面段（前端随机摆怪用它做贴地插值）。
    """
    ws = _need_ws()
    mid = str(mid).strip()
    if not re.fullmatch(r"\d{9}", mid):
        raise CliError("地图 id 应该是 9 位数字", 2)
    visited = _visited if _visited is not None else set()
    if mid in visited:
        raise CliError(f"link 成环了：{' → '.join(sorted(visited))} → {mid}", 1)
    visited.add(mid)
    root = CACHE if preview else EXPORTS
    outdir = (root / "map" / mid) if preview else (EXPORTS / "maps" / mid)

    def _cached_ok() -> bool:
        # 缓存要能解析才算数：早期 nxmap 有产出坏 JSON 的情况
        try:
            json.loads((outdir / "map.json").read_text(encoding="utf-8"))
            return True
        except Exception:
            return False

    if not _cached_ok():
        if outdir.exists():
            drop_dir(outdir)
        outdir.parent.mkdir(parents=True, exist_ok=True)
        _run(["nxmap", mid, str(outdir)], ws, timeout=600)
    if not (outdir / "map.json").exists():
        raise CliError(f"nxmap 没产出 map.json（这个 id 存在吗？{mid}）", 1)
    mj = json.loads((outdir / "map.json").read_text(encoding="utf-8"))

    # 链接图：拿目标图的数据渲染（客户端同款行为）
    link = str(mj.get("info", {}).get("link") or "").strip()
    if link and link != mid and re.fullmatch(r"\d{9}", link):
        d = c_map_export(link, preview, mobs, density, region, seed, visited)
        d["link"] = {"from": mid, "to": link}
        return d

    return _map_export_inner(ws, mid, preview, mobs, density, region, visited,
                             seed=seed, outdir=outdir, mj=mj)


def _map_export_inner(ws: Path, mid: str, preview: bool, mobs: bool, density: int,
                      region: tuple | None, visited: set, seed: str | None = None,
                      outdir: Path | None = None, mj: dict | None = None) -> dict:
    root = CACHE if preview else EXPORTS
    if outdir is None:
        outdir = (root / "map" / mid) if preview else (EXPORTS / "maps" / mid)
    if mj is None:
        mj = json.loads((outdir / "map.json").read_text(encoding="utf-8"))
    png = outdir / "preview.png"
    size = _render_preview(outdir, png)
    life = [l for l in mj.get("life", []) if l.get("type") == "m"]
    kinds = sorted({str(l.get("id", "")).lstrip("0") for l in life})
    mobnx = ws / "Mob.nx"
    meta = {m["id"]: m for m in mob_meta(ws, mobnx)} if mobnx.exists() else {}
    # 刷怪点叠加：overlay 必须和渲染实际用的一致（render_file pad=40、max_width=1600）
    R = _render_mod()
    minx, miny, bw, bh = R.map_bounds(mj)
    minx -= 40; miny -= 40; bw += 80; bh += 80
    s = min(1.0, 1600 / max(1.0, bw))
    points = [{"id": (k := str(l.get("id", "")).lstrip("0")),
               "x": float(l.get("x", 0)),
               # 客户端同款落地：y 只是出生参考点，真正站的是 cy（fh 指向的地面 y）
               "y": float(l.get("cy") if l.get("fh") and l.get("cy") is not None else l.get("y", 0)),
               # rx0/rx1 是客户端给这只怪划定的巡逻区间（随机摆怪用）
               "rx0": (float(l["rx0"]) if l.get("rx0") is not None else None),
               "rx1": (float(l["rx1"]) if l.get("rx1") is not None else None),
               "name": meta.get(k, {}).get("name") or mob_names().get(k) or "",
               "tier": meta.get(k, {}).get("tier", "normal")}
              for l in life]
    result = {"dir": str(outdir), "preview": size,
              "preview_path": str(png),
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
              "life_points": points,
              # 全部地面段 [x1,y1,x2,y2]：前端随机摆怪时做贴地插值
              "foothold_segs": [[f["x1"], f["y1"], f["x2"], f["y2"]]
                                for f in mj.get("foothold", [])]}
    # 导出图带怪物：把怪沿地面随机散布合成进预览图（preview_mobs.png）
    if mobs and life:
        mob_png = _composite_mobs(ws, outdir, mj, png,
                                  str(seed) if seed else f"{mid}:{random.randrange(1 << 30):x}",
                                  density=max(1, min(10, int(density or 1))))
        if mob_png is not None:
            rel = (mob_png.relative_to(CACHE) if preview else mob_png.relative_to(EXPORTS))
            result["preview_path"] = str(mob_png)
            result["preview_url"] = (f"/preview/{rel.as_posix()}" if preview
                                     else f"/exports/{rel.as_posix()}")
            result["mobs"] = True
    # 区域裁剪：只导出世界坐标 (x, y, w, h) 的窗口（客户端画面就是固定分辨率的窗口）
    if region:
        rx, ry, rw, rh = (float(v) for v in region)
        if rw <= 0 or rh <= 0:
            raise CliError("区域宽高必须大于 0", 2)
        from PIL import Image
        src = Image.open(Path(result["preview_path"]))
        W, H = src.size
        left = max(0, round((rx - minx) * s))
        top = max(0, round((ry - miny) * s))
        right = min(W, round((rx + rw - minx) * s))
        bottom = min(H, round((ry + rh - miny) * s))
        if right - left < 8 or bottom - top < 8:
            raise CliError(
                "区域不在地图范围内。地图世界坐标范围："
                f"x∈[{minx:.0f},{minx + bw:.0f}] y∈[{miny:.0f},{miny + bh:.0f}]", 2)
        def _n(v: float) -> str:
            return str(int(v)) if float(v).is_integer() else str(v)
        out_png = outdir / f"preview_region_x{_n(rx)}_y{_n(ry)}_w{_n(rw)}_h{_n(rh)}.png"
        src.crop((left, top, right, bottom)).save(out_png)
        cw, ch = right - left, bottom - top
        rel = (out_png.relative_to(CACHE) if preview else out_png.relative_to(EXPORTS))
        result["preview"] = {"w": cw, "h": ch}
        result["preview_path"] = str(out_png)
        result["preview_url"] = (f"/preview/{rel.as_posix()}" if preview
                                 else f"/exports/{rel.as_posix()}")
        result["region"] = {"x": rx, "y": ry, "w": rw, "h": rh,
                            "px": {"x": left, "y": top, "w": cw, "h": ch}}
        # 叠加层坐标系改为以区域左上角为原点，圆点/立绘在裁剪图上照样对准
        result["overlay"] = {"minx": rx, "miny": ry, "s": s, "w": cw, "h": ch}
    return result


def c_map_info(mid: str, _visited: set | None = None) -> dict:
    """查地图元信息，**不渲染**：世界坐标包围盒 / 预览缩放 / 刷怪统计。

    先知道「地图多大」，再决定导哪个窗口——比 map-export 快一个量级
    （只跑 nxmap 解析 map.json，不合成贴图不出图）。解析结果进
    .cache/map/<id>/（与预览共用缓存，之后 map-export 直接复用）。
    链接图自动转向目标图，返回带 link 字段。
    """
    ws = _need_ws()
    mid = str(mid).strip()
    if not re.fullmatch(r"\d{9}", mid):
        raise CliError("地图 id 应该是 9 位数字", 2)
    visited = _visited if _visited is not None else set()
    if mid in visited:
        raise CliError(f"link 成环了：{' → '.join(sorted(visited))} → {mid}", 1)
    visited.add(mid)
    outdir = CACHE / "map" / mid
    if not (outdir / "map.json").exists():
        if outdir.exists():
            drop_dir(outdir)
        outdir.parent.mkdir(parents=True, exist_ok=True)
        _run(["nxmap", mid, str(outdir)], ws, timeout=600)
    if not (outdir / "map.json").exists():
        raise CliError(f"nxmap 没产出 map.json（这个 id 存在吗？{mid}）", 1)
    mj = json.loads((outdir / "map.json").read_text(encoding="utf-8"))
    link = str(mj.get("info", {}).get("link") or "").strip()
    if link and link != mid and re.fullmatch(r"\d{9}", link):
        d = c_map_info(link, visited)
        d["link"] = {"from": mid, "to": link}
        return d
    R = _render_mod()
    cx, cy, cw, chh = R.map_bounds(mj)          # 内容包围盒（不含 pad）
    minx, miny = cx - 40, cy - 40
    bw, bh = cw + 80, chh + 80                   # 渲染画布窗口（pad=40，与导出一致）
    s = min(1.0, 1600 / max(1.0, bw))
    life = [l for l in mj.get("life", []) if l.get("type") == "m"]
    mobnx = ws / "Mob.nx"
    meta = {m["id"]: m for m in mob_meta(ws, mobnx)} if mobnx.exists() else {}
    kinds = sorted({str(l.get("id", "")).lstrip("0") for l in life})
    return {"id": mid,
            "bounds": {"x": cx, "y": cy, "w": cw, "h": chh},
            "world": {"x": minx, "y": miny, "w": bw, "h": bh},
            "scale": s,
            "preview_px": {"w": round(bw * s), "h": round(bh * s)},
            "life_count": len(life),
            "life_kinds": [{"id": k,
                            "name": meta.get(k, {}).get("name") or mob_names().get(k) or "",
                            "tier": meta.get(k, {}).get("tier", "normal")} for k in kinds],
            "footholds": len(mj.get("foothold", [])),
            "ladders": len(mj.get("ladderRope", []))}


def c_clear_cache() -> dict:
    # 逐个删子目录，删不掉的挪进 trash —— 见 drop_dir 注释
    for d in list(CACHE.iterdir()) if CACHE.exists() else []:
        if d.name == "trash":
            continue
        drop_dir(d) if d.is_dir() else (d.unlink() if d.is_file() else None)
    CACHE.mkdir(exist_ok=True)
    _lists.clear()
    return {"ok": True}


# ---------------------------------------------------------------- CLI 出口

def _out(data: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        # 人类可读摘要：一层 key→值，列表给长度
        for k, v in data.items():
            if isinstance(v, list):
                n = len(v)
                print(f"{k}: {n} 项" + (f"  （示例: {v[0]}）" if v and not isinstance(v[0], dict) else ""))
            elif isinstance(v, dict):
                print(f"{k}: {json.dumps(v, ensure_ascii=False)[:200]}")
            else:
                print(f"{k}: {v}")


def _pick(obj: dict, keys: list[str]) -> dict:
    return {k: obj[k] for k in keys if k in obj}


# ---------------------------------------------------------------- web 适配层
# 同一套 API 约定（wztool web 供页面/本地程序调用；自动化请优先用子命令）。
# stdlib http.server，无第三方依赖。

import http.server
import urllib.parse


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "wztool"

    def log_message(self, fmt, *args):       # 静默访问日志
        pass

    # ---- 基础 ----
    def _send_json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, p: Path, mime: str) -> None:
        if not p.is_file():
            self._send_json(404, {"detail": "not found"})
            return
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _api(self, fn, *a, **kw):
        try:
            self._send_json(200, fn(*a, **kw))
        except CliError as e:
            self._send_json(400 if e.code == 2 else 500, {"detail": str(e)})
        except Exception as e:               # noqa: BLE001
            self._send_json(500, {"detail": str(e)})

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---- 路由 ----
    # POST-only 路由集合：GET 打过来回 405（与旧 FastAPI 行为一致），别的才 404
    _POST_ONLY = {"/api/browse", "/api/scan", "/api/convert", "/api/mob/export",
                   "/api/map/export", "/api/export/clear-cache"}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        if path == "/" or path == "/index.html":
            self._send_file(STATIC / "cli.html", "text/html; charset=utf-8")
        elif path == "/api/state":
            self._api(c_state)
        elif path == "/api/job":
            self._api(c_job)
        elif path == "/api/mobs":
            self._api(c_mobs)
        elif path.startswith("/api/mob/icon/"):
            self._api(c_mob_icon, path.rsplit("/", 1)[-1])
        elif path.startswith("/api/mob/") and path.endswith("/maps"):
            self._api(c_mob_maps, path[len("/api/mob/"):-len("/maps")])
        elif path == "/api/maps":
            self._api(c_maps)
        elif path.startswith("/preview/"):
            self._send_file(CACHE / urllib.parse.unquote(path[len("/preview/"):]),
                            "image/png")
        elif path.startswith("/exports/"):
            self._send_file(EXPORTS / urllib.parse.unquote(path[len("/exports/"):]),
                            "image/png")
        elif path.startswith("/static/"):
            mime = {"png": "image/png", "css": "text/css", "js": "text/javascript",
                    "html": "text/html", "txt": "text/plain"}.get(path.rsplit(".", 1)[-1], "application/octet-stream")
            self._send_file(STATIC / urllib.parse.unquote(path[len("/static/"):]), mime)
        elif path in self._POST_ONLY:
            self._send_json(405, {"detail": f"Method Not Allowed: {path} 是 POST 接口"})
        else:
            self._send_json(404, {"detail": f"no route: {path}"})

    def do_POST(self):
        path = self.path.split("?")[0]
        b = self._body()
        if path == "/api/browse":
            self._api(c_browse, b.get("path") or "")
        elif path == "/api/scan":
            if not b.get("path"):
                self._send_json(400, {"detail": "path 必填"})
                return
            self._api(c_workspace, b["path"])
        elif path == "/api/convert":
            self._api(_web_convert, b.get("files") or [], bool(b.get("force")))
        elif path == "/api/mob/export":
            self._api(c_mob_export, str(b.get("raw") or ""), bool(b.get("preview")))
        elif path == "/api/map/export":
            rg = b.get("region")
            region = None
            if rg:
                try:
                    t = (rg["x"], rg["y"], rg["w"], rg["h"]) if isinstance(rg, dict) else rg[:4]
                    region = tuple(float(v) for v in t)
                except Exception:
                    region = None
            self._api(c_map_export, str(b.get("id") or ""),
                      bool(b.get("preview")), bool(b.get("mobs")),
                      int(b.get("density") or 1), region,
                      str(b["seed"]) if b.get("seed") else None)
        elif path == "/api/export/clear-cache":
            self._api(c_clear_cache)
        elif path in ("/api/state", "/api/job", "/api/mobs", "/api/maps") \
                or path.startswith("/api/mob/icon/") \
                or (path.startswith("/api/mob/") and path.endswith("/maps")):
            self._send_json(405, {"detail": f"Method Not Allowed: {path} 是 GET 接口"})
        else:
            self._send_json(404, {"detail": f"no route: {path}"})


def _web_convert(names: list[str], force: bool) -> dict:
    """web 版转换：后台线程跑，前端轮询 /api/job（和页面现有约定一致）。"""
    _need_tools()
    ws = _need_ws()
    todo = _convert_pick(ws, names, force)
    if not todo:
        return {"started": False, "message": "选中的都已是 .nx（要重转勾「强制重转」）"}
    with _lock:
        if _job["running"]:
            raise CliError("已有转换在跑，等它完成", 1)
        _job.update(id=_job["id"] + 1, running=True, log=[], done=0,
                    total=len(todo), error="")

    def work():
        try:
            for wz in todo:
                _convert_one(wz, lambda s: _job["log"].append(s))
                _job["done"] += 1
            _job["log"].append("全部完成 ✔")
        except Exception as exc:              # noqa: BLE001
            _job["error"] = str(exc)
            _job["log"].append(f"出错：{exc}")
        finally:
            _job["running"] = False
            _lists.clear()

    threading.Thread(target=work, daemon=True).start()
    return {"started": True, "total": len(todo), "job": _job["id"]}


def cmd_web(args) -> int:
    port = args.port
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"wztool web 已启动: {url}  （CLI 版页面；Ctrl+C 退出）")
    if not args.no_open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


# ---------------------------------------------------------------- 子命令实现

def _log_print(s: str) -> None:
    print(s, flush=True)


def cmd_state(a) -> int:
    _out(c_state(), a.json)
    return 0


def cmd_browse(a) -> int:
    _out(c_browse(a.path), a.json)
    return 0


def cmd_workspace(a) -> int:
    _out(c_workspace(a.path), a.json)
    return 0


def cmd_convert(a) -> int:
    d = c_convert(a.files, a.force, _log_print)
    _out(d, a.json)
    return 0


def cmd_job(a) -> int:
    _out(c_job(), a.json)
    return 0


def cmd_mobs(a) -> int:
    d = c_mobs()
    if a.json or not a.filter:
        _out(d, a.json)
        return 0
    items = d["mobs"]
    if a.filter == "boss":
        items = [m for m in items if m["tier"] in ("boss", "big")]
    elif a.filter == "big":
        items = [m for m in items if m["tier"] == "big"]
    if a.name:
        print("id,name,tier,level,hp,boss,link,frames")
        for m in items:
            print(f"{m['id']},{m['name']},{m['tier']},{m['level']},{m['hp']},{m['boss']},{m['link']},{m['frames']}")
    else:
        for m in items:
            print(f"{m['id']}\t{m['name']}\t{m['tier']}\tLv{m['level']}\tHP {m['hp']}")
    print(f"# 共 {len(items)} 只", file=sys.stderr)
    return 0


def cmd_mob_maps(a) -> int:
    _out(c_mob_maps(a.id), a.json)
    return 0


def cmd_maps(a) -> int:
    d = c_maps()
    if a.json or not a.filter:
        _out(d, a.json)
        return 0
    ids = d["maps"]
    life = set(d["life_maps"])
    if a.filter == "mob":
        ids = [i for i in ids if i in life]
    elif a.filter == "empty":
        ids = [i for i in ids if i not in life]
    for i in ids:
        print(i)
    print(f"# 共 {len(ids)} 张（{len(life)} 张有怪）", file=sys.stderr)
    return 0


def cmd_map_info(a) -> int:
    d = c_map_info(a.id)
    if a.json:
        _out(d, a.json)
        return 0
    link = f"（链接图 → 目标 {d['link']['to']}）" if d.get("link") else ""
    b, w = d["bounds"], d["world"]
    print(f"地图 {d['id']}: 内容 {b['w']:.0f}×{b['h']:.0f} @ ({b['x']:.0f},{b['y']:.0f})"
          f" · 可导窗口 {w['w']:.0f}×{w['h']:.0f} @ ({w['x']:.0f},{w['y']:.0f}){link}")
    print(f"预览: {d['preview_px']['w']}×{d['preview_px']['h']} px"
          f"（缩放 {d['scale']:.3f}）· 刷怪 {d['life_count']} · "
          f"地面 {d['footholds']} 段 · 绳梯 {d['ladders']} 段")
    return 0


def cmd_mob_icon(a) -> int:
    _out(c_mob_icon(a.id), a.json)
    return 0


def cmd_mob_export(a) -> int:
    d = c_mob_export(a.id, preview=a.preview)
    if a.json:
        _out(d, True)
    else:
        link = f"（链接怪 → 画面同 {d['link']['to']}）" if d.get("link") else ""
        print(f"已导出 {d['count']} 帧 → {d['dir']} {link}")
    return 0


def cmd_map_export(a) -> int:
    region = None
    if getattr(a, "region", None):
        try:
            parts = [float(v) for v in str(a.region).replace("，", ",").split(",")]
            if len(parts) != 4:
                raise ValueError
            region = tuple(parts)
        except ValueError:
            print("错误: --region 应为 X,Y,W,H（如 850,-900,1366,768）", file=sys.stderr)
            return 2
    d = c_map_export(a.id, preview=a.preview, mobs=a.mobs, density=a.density,
                     region=region, seed=getattr(a, "seed", None))
    if a.json:
        _out(d, True)
    else:
        link = f"（链接图 → 画面同 {d['link']['to']}）" if d.get("link") else ""
        p = d["preview"]
        mobs = " · 已合成怪物" if d.get("mobs") else ""
        print(f"地图 {a.id}: {p['w']}×{p['h']} · 刷怪 {d['life_count']} · "
              f"地面 {d['footholds']} 段 · 绳梯 {d['ladders']} 段{mobs} {link}")
        print(f"目录: {d['dir']}")
        print(f"预览: {d['preview_path']}")
    return 0


def cmd_clear_cache(a) -> int:
    _out(c_clear_cache(), a.json)
    return 0


def cmd_mob_frames(a) -> int:
    """列出一只怪有哪些动画（stand/move/jump/hit/die1…）与帧数。"""
    d = c_mob_export(a.id, preview=True)
    anims: dict[str, int] = {}
    for f in d.get("frames", []):
        anims[f["anim"]] = max(anims.get(f["anim"], 0), f["frame"] + 1)
    data = {"id": a.id, "dir": d["dir"], "total_frames": d["count"],
            "link": d.get("link"), "anims": anims}
    _out(data, a.json)
    return 0


# ---------------------------------------------------------------- 参数与入口

def _j(p: argparse.ArgumentParser):
    p.add_argument("--json", action="store_true", help="输出完整 JSON（机器可读；默认是人类摘要）")


def build_parser() -> argparse.ArgumentParser:
    epilog = """\
典型调用流程（AI/程序视角）:
  1. wztool state --json                      # 看有没有设置工作区
  2. wztool workspace "D:/path/to/nxrun"       # 设置工作区（放 .wz/.nx 的目录）
  3. wztool convert Mob.wz Map.wz String.wz    # wz → nx（需要 bin\\ 里的原生工具）
  4. wztool mobs --json                        # 怪物列表（含 tier/level/hp/link/frames）
  5. wztool maps --json                        # 地图列表（含 life_maps=有怪的图）
  6. wztool mob-export 0100100                 # → exports/mobs/0100100/（PNG 逐帧 + manifest.csv）
  7. wztool map-export 000050000 --mobs        # → exports/maps/000050000/（map.json + 贴图 + 预览）

约定:
  - 所有 id 兼容带/不带前导 0（"0100100" 与 "100100" 等价）。
  - 链接怪/链接图（WZ info/link 机制）自动顺链解析，返回 JSON 里有 link 字段。
  - 怪物帧 PNG 的 ox/oy 是锚点（脚底中心）；map.json 的 life[].cy 是怪站的地面 y。
  - --json 永远输出完整数据；不加 --json 时列表类命令只打摘要/表头，省 token。
"""
    ap = argparse.ArgumentParser(
        prog="wztool",
        description="WzToolkit CLI —— 冒险岛 .wz 资源导入/转换/浏览/导出（供人类与 AI/程序调用）",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")

    p = sub.add_parser("state", help="查看当前状态（工作区 / 工具是否就绪 / 导出目录）")
    _j(p); p.set_defaults(fn=cmd_state)

    p = sub.add_parser("workspace", help="设置工作区（放 .wz/.nx 的目录）并列出里面的 .wz",
                       epilog="示例: wztool workspace D:/MapleStory/nxrun")
    p.add_argument("path", help="目录绝对路径")
    _j(p); p.set_defaults(fn=cmd_workspace)

    p = sub.add_parser("browse", help="列出目录内容（path 省略 = 磁盘根）")
    p.add_argument("path", nargs="?", default="")
    _j(p); p.set_defaults(fn=cmd_browse)

    p = sub.add_parser("convert", help="把 .wz 转成 .nx（同步执行，逐个打印进度）",
                       epilog="示例: wztool convert Mob.wz Map.wz --force")
    p.add_argument("files", nargs="+", help=".wz 文件名（相对工作区）")
    p.add_argument("--force", action="store_true", help="已转过的也重转")
    _j(p); p.set_defaults(fn=cmd_convert)

    p = sub.add_parser("job", help="查询后台任务进度（web 模式下 convert 用）")
    _j(p); p.set_defaults(fn=cmd_job)

    p = sub.add_parser("mobs", help="列出全部怪物（tier=big/boss/normal，含等级血量）",
                       epilog="不加 --json 时只打印表格式列表；--name 加中文名列。")
    p.add_argument("--filter", choices=["all", "boss", "big"], default="",
                   help="按档位筛：boss=BOSS+大BOSS，big=只看大BOSS")
    p.add_argument("--name", action="store_true", help="非 JSON 输出带名字列")
    _j(p); p.set_defaults(fn=cmd_mobs)

    p = sub.add_parser("mob-maps", help="这只怪自然刷新在哪些地图（life 反向聚合）")
    p.add_argument("id", help="怪 id（带不带前导 0 都行）")
    _j(p); p.set_defaults(fn=cmd_mob_maps)

    p = sub.add_parser("maps", help="列出全部地图 id（life_maps=有怪自然刷新的图）")
    p.add_argument("--filter", choices=["all", "mob", "empty"], default="",
                   help="mob=只看有怪的图，empty=只看无怪图")
    _j(p); p.set_defaults(fn=cmd_maps)

    p = sub.add_parser("map-info", help="查地图元信息（不渲染：尺寸/世界坐标/刷怪统计）",
                       epilog="比 map-export 快一个量级（只解析 map.json，不出图）。\n"
                              "bounds=内容包围盒；world=加 40px pad 的可导窗口（--region 用它算）；\n"
                              "scale<1 表示导出预览会被缩放（1366×768 窗口可能裁不满）。")
    p.add_argument("id", help="9 位地图 id（链接图自动转向目标图）")
    _j(p); p.set_defaults(fn=cmd_map_info)

    p = sub.add_parser("mob-frames", help="列出一只怪的动画列表与帧数（stand/move/die1…）")
    p.add_argument("id", help="怪 id")
    _j(p); p.set_defaults(fn=cmd_mob_frames)

    p = sub.add_parser("mob-export", help="导出一只怪的全部动画帧 PNG + manifest.csv",
                       epilog="输出: exports/mobs/<id>/（--preview 时进 .cache/mob/<id>/）。\n"
                              "manifest.csv 列: mob,anim,frame,file,w,h,ox,oy,delay；\n"
                              "ox/oy=锚点(脚底中心)，把各帧对到同一画布用它。")
    p.add_argument("id", help="怪 id（链接怪自动转向目标怪）")
    p.add_argument("--preview", action="store_true", help="只出预览缓存，不进 exports")
    _j(p); p.set_defaults(fn=cmd_mob_export)

    p = sub.add_parser("mob-icon", help="取一只怪的 stand 帧（路径/尺寸/锚点）",
                       epilog="给刷怪点叠怪、做图鉴头像用；ox/oy=脚底中心锚点。")
    p.add_argument("id", help="怪 id")
    _j(p); p.set_defaults(fn=cmd_mob_icon)

    p = sub.add_parser("map-export", help="导出一张地图（map.json + 贴图 + 渲染预览图）",
                       epilog="输出: exports/maps/<id>/（--preview 时进 .cache/map/<id>/）。\n"
                              "map.json: life(刷怪表)/foothold(地面)/ladderRope(绳梯)/portal。\n"
                              "--mobs: 预览图直接把怪随机画上去（preview_mobs.png）。\n"
                              "--json 里的 life_points+overlay 可在任何客户端复现刷怪点。")
    p.add_argument("id", help="9 位地图 id（链接图自动转向目标图）")
    p.add_argument("--preview", action="store_true", help="只出预览缓存，不进 exports")
    p.add_argument("--mobs", action="store_true",
                   help="预览图合成怪物（沿地面随机散布、贴地不悬空；默认每次随机，--seed 可复现）")
    p.add_argument("--seed", metavar="S", help="配合 --mobs：固定随机种子（同 seed 同布局；缺省每次随机）")
    p.add_argument("--density", type=int, default=1, metavar="N",
                   help="配合 --mobs：怪物密度倍数（1-10，默认 1；每个刷怪点放 N 只，左右散开）")
    p.add_argument("--region", metavar="X,Y,W,H",
                   help="只导出世界坐标区域。含负数时必须用 = 形式，"
                        "如 --region=850,-900,1366,768"
                        "（客户端画面就是固定分辨率窗口，常见 1366×768 / 1024×768 / 800×600）")
    _j(p); p.set_defaults(fn=cmd_map_export)

    p = sub.add_parser("clear-cache", help="清空 .cache 预览缓存（exports 不动）")
    _j(p); p.set_defaults(fn=cmd_clear_cache)

    p = sub.add_parser("web", help="启动本地 Web 页面（浏览器界面，人看；程序调用请用上面的子命令）",
                       epilog="页面与 CLI 共用同一套导出逻辑；端口默认 8000。")
    p.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    p.set_defaults(fn=cmd_web)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        ap.print_help()
        return 0
    try:
        return args.fn(args)
    except CliError as e:
        print(f"错误: {e}", file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        print("\n中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
