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


def _render_preview(map_dir: Path, out_png: Path, no_bg: bool = False) -> dict:
    """按 **真实尺寸（1:1）** 渲染地图 PNG（客户端渲染就是 1:1，缩放由 UI 负责）。

    缓存：PNG 旁边写一个同名的 `.meta` 记录渲染参数。早期版本渲染的是缩到 1600 宽的
    小图，没有 meta 就当作过期重渲（否则会一直命中旧的小图）。
    """
    from PIL import Image
    meta = out_png.with_suffix(".meta")
    stamp = f"native-1x no_bg={int(bool(no_bg))}\n"
    fresh = out_png.exists() and meta.exists() and meta.read_text() == stamp
    if not fresh:
        import tempfile
        R = _render_mod()
        out_png.parent.mkdir(parents=True, exist_ok=True)
        # 多进程并发导同一张图时都会来渲染：先写临时文件，再原子替换，
        # 避免互相把对方的半成品 PNG 读进来/覆盖掉
        fd, tmp = tempfile.mkstemp(suffix=".png", dir=out_png.parent)
        os.close(fd)
        try:
            os.unlink(tmp)
            R.render_file(str(map_dir), str(tmp), max_width=None, no_bg=no_bg)
            os.replace(tmp, out_png)
            meta.write_text(stamp)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    import time as _t
    for _ in range(50):           # 对方刚 replace 完的一瞬可能撞上 FileNotFoundError
        try:
            with Image.open(out_png) as im:
                return {"w": im.width, "h": im.height}
        except FileNotFoundError:
            _t.sleep(0.2)
    with Image.open(out_png) as im:
        return {"w": im.width, "h": im.height}


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


def _mob_anim_frames(ws: Path, kind_id: str) -> dict:
    """一种怪的全部动画帧，按动作分组：{action: [(path, w, h, ox, oy), ...]}。

    每帧 PNG 都按自己的包围盒裁出，ox/oy 是该帧锚点（脚底基准）；
    stand/move/attack1/hit1/die1/jump/fly 等动作都在 manifest 里。没图返回 {}。
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
        return {}
    by_name = {p.name: p for p in outdir.rglob("*.png")}
    anims: dict[str, list] = {}
    man = outdir / "manifest.csv"
    if man.exists():
        for f in list(csv.reader(man.read_text(encoding="utf-8-sig").splitlines()))[1:]:
            if len(f) < 10 or f[4] not in by_name:
                continue
            try:
                w, h, ox, oy = (int(x) for x in f[5:9])
            except ValueError:
                continue
            anims.setdefault(f[2], []).append((by_name[f[4]], w, h, ox, oy))
    if not anims and by_name:          # 兜底：没有 manifest 就按文件名前缀分组
        from PIL import Image
        for name, p in sorted(by_name.items()):
            with Image.open(p) as im:
                w, h = im.size
            anims.setdefault(name.split("_")[0], []).append((p, w, h, w // 2, h))
    return anims


# 各动作的抽取权重：站/走/飞最常见，攻击次之，受击/死亡/跳跃偏少
ACT_WEIGHTS = (("stand", 3), ("move", 3), ("walk", 3), ("fly", 3),
               ("attack", 2), ("regen", 2),
               ("hit", 1), ("die", 1), ("jump", 1))


def _act_weight(act: str) -> float:
    for pre, w in ACT_WEIGHTS:
        if act.startswith(pre):
            return float(w)
    return 1.0


def _stand_frame(ws: Path, kind_id: str):
    """一种怪的 stand 帧 + 锚点：(Path, w, h, ox, oy)；没有图返回 None。

    ox/oy 是锚点（脚底中心）在 PNG 里的位置，贴图/定位都用它对准刷怪点。
    """
    anims = _mob_anim_frames(ws, kind_id)
    for act in ("stand", "move", "fly"):
        if anims.get(act):
            return anims[act][0]
    for frames in anims.values():
        return frames[0]
    return None


def c_mob_icon(raw: str) -> dict:
    """一只怪的「立绘」：stand 帧给地图叠加用；anims 给多种动作状态用。

    anims = {动作名: [{url,w,h,ox,oy} 每帧]}——站/走/攻击/受击/死亡等全动作帧表，
    页面叠加随机挑状态，和导出合成（_composite_mobs）同款规则。
    """
    ws = _need_ws()
    if not re.fullmatch(r"\d+", raw):
        raise CliError("怪 id 不合法（应为数字）", 2)
    anims_raw = _mob_anim_frames(ws, raw)
    if not anims_raw:
        raise CliError("这只怪没有图片资源", 1)
    anims = {act: [{"url": f"/preview/{p.relative_to(CACHE).as_posix()}",
                    "w": w, "h": h, "ox": ox, "oy": oy}
                   for p, w, h, ox, oy in frs]
             for act, frs in anims_raw.items()}
    pick, w, h, ox, oy = (anims_raw.get("stand") or
                          next(iter(anims_raw.values())))[0]
    return {"path": str(pick),
            "url": f"/preview/{pick.relative_to(CACHE).as_posix()}",
            "w": w, "h": h, "ox": ox, "oy": oy,
            "anims": anims}


def _composite_mobs(ws: Path, outdir: Path, mj: dict, base_png: Path, seed: str,
                   density: int = 1, no_boss: bool = False,
                   no_bg: bool = False):
    """把这张图的怪随机贴到地面上，产出 preview_mobs.png（脚底落在 foothold 地面）。

    返回 (png路径, 摆放列表)：places 每项 {kind, x, y, w, h} 是该怪在预览图上的
    精确像素框（x,y=左上角），YOLO 标注直接用它当 ground truth。

    no_boss=True 时只放大 BOSS 以外的怪（tier=big 不出现，高阶怪 boss/normal
    照常参与合成）
    （挂机升级场景不打 Boss；只刷 Boss 的图返回空结果）。

    每个怪在「随机游走区间」内取随机 x：优先用 life 自带 rx0~rx1（客户端巡逻区间），
    没有就按图宽 ±8% 兜底；y 由 foothold 段线性插值得出（同一段里取离出生地面最近
    的，保证贴地不悬空），落点悬空时退回出生点附近。动作状态也随机：按 ACT_WEIGHTS
    加权抽动作（站/走/飞多、攻击次之、受击/死亡/跳跃少），动作内随机抽帧，所有怪
    50% 随机镜像朝向，随机结果按 seed 稳定（同 seed 同布局）。
    """
    from PIL import Image
    life = [l for l in mj.get("life", []) if l.get("type") == "m"]
    if no_boss and life:
        mobnx = ws / "Mob.nx"
        meta = {m["id"]: m for m in mob_meta(ws, mobnx)} if mobnx.exists() else {}
        life = [l for l in life
                if meta.get(str(l.get("id", "")).lstrip("0"), {})
                .get("tier", "normal") != "big"]
    if not life:
        return None, []
    R = _render_mod()
    minx, miny, bw, bh = R.map_bounds(mj)
    minx -= 40; miny -= 40; bw += 80; bh += 80
    s = 1.0                  # 渲染改成 1:1（真实尺寸），不再缩到 1600 宽
    frames = {}          # kind -> {action: [(img, ox, oy), ...]}（含缩放）
    frames_flip = {}
    for k in sorted({str(l.get("id", "")).lstrip("0") for l in life}):
        ka, kf = {}, {}
        for act, frs in _mob_anim_frames(ws, k).items():
            for pick, w, h, ox, oy in frs:
                img = Image.open(pick).convert("RGBA")
                if abs(s - 1.0) > 1e-3:
                    img = img.resize((max(1, round(w * s)), max(1, round(h * s))))
                    ox, oy = ox * s, oy * s
                ka.setdefault(act, []).append((img, ox, oy))
                kf.setdefault(act, []).append(
                    (img.transpose(Image.FLIP_LEFT_RIGHT), img.width - ox, oy))
        if ka:
            frames[k] = ka
            frames_flip[k] = kf
    if not frames:
        return None, []
    im = Image.open(base_png).convert("RGBA")
    rng = random.Random(f"{seed}:d{density}")
    keys = list(frames)
    fhs = [(float(f["x1"]), float(f["y1"]), float(f["x2"]), float(f["y2"]))
           for f in mj.get("foothold", [])]
    places = []                          # 摆放记录：YOLO ground truth

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
            kind = rng.choice(keys)
            anim = frames_flip[kind] if flip else frames[kind]
            # 随机动作状态：按 ACT_WEIGHTS 加权抽（站/走/飞多，攻击次之，
            # 受击/死亡/跳跃少），再在动作里随机抽一帧
            acts = list(anim)
            tot = sum(_act_weight(a) for a in acts)
            r = rng.random() * tot
            act = acts[-1]
            for a in acts:
                r -= _act_weight(a)
                if r <= 0:
                    act = a
                    break
            img, ox, oy = rng.choice(anim[act])
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
            places.append({"kind": kind, "state": act,
                           "x": round(px), "y": round(py),
                           "w": img.width, "h": img.height})
    out = outdir / ("preview_mobs_nobg.png" if no_bg else "preview_mobs.png")
    (im if no_bg else im.convert("RGB")).save(out)   # no_bg 保留透明通道
    return out, places


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
                 density: int = 1, view: tuple | None = None,
                 yolo: bool = False, seed: str | None = None,
                 no_boss: bool = False,
                 _visited: set | None = None,
                 no_bg: bool = False) -> dict:
    """导出一张地图：map.json（life/foothold/ladderRope）+ 全部贴图 + 渲染预览图。

    链接图（info/link，自己的 img 是空的）自动顺链导出目标图，返回带 link 字段。
    mobs=True 时预览图把怪随机画到地面上（preview_mobs.png，沿 rx 巡逻区间散布、
    foothold 落地）；seed 缺省每次随机，传同值可复现同一布局。
    no_boss=True 时合成怪/标注不放大 BOSS（tier=big；高阶怪 boss/normal 照常
    出现）——挂机升级用；只刷大 BOSS 的图会得到没有怪的合成结果。
    yolo=True 时额外产出 YOLO 训练集（exports/yolo/：images + labels +
    classes.txt/dataset.yaml；每只怪一个框、每段绳/梯一个框、每段地面一个框
    ——平地标 ground、坡度≥0.2 的标 slope），
    蕴含 mobs=True。
    view=(x, y) 是导出窗口（VIEW_W×VIEW_H = 客户端画面 1366×768）的左上角世界坐标；
    缺省取地图正中。导出文件统一是 1366×768，窗口超出地图边界的地方补空白。
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
        # 缓存要能解析才算数：早期 nxmap 有产出坏 JSON 的情况；
        # 另外要求带 miniMap 键——旧版 nxmap 不导小地图，命中旧缓存就永远出不来
        try:
            mj = json.loads((outdir / "map.json").read_text(encoding="utf-8"))
            return "miniMap" in mj
        except Exception:
            return False

    if not _cached_ok():
        outdir.parent.mkdir(parents=True, exist_ok=True)
        # 覆盖式刷新，不整目录重删：nxmap 会重写 map.json 与引用到的贴图，
        # 旧版本缓存的遗留 PNG 留着无害（渲染只按 map.json 引用取图），
        # 而且几百个文件的整目录删除在部分环境会被批量删除保护拦下
        _run(["nxmap", mid, str(outdir)], ws, timeout=600)
    if not (outdir / "map.json").exists():
        raise CliError(f"nxmap 没产出 map.json（这个 id 存在吗？{mid}）", 1)
    mj = json.loads((outdir / "map.json").read_text(encoding="utf-8"))

    # 链接图：拿目标图的数据渲染（客户端同款行为）
    link = str(mj.get("info", {}).get("link") or "").strip()
    if link and link != mid and re.fullmatch(r"\d{9}", link):
        d = c_map_export(link, preview, mobs, density, view, yolo, seed,
                         no_boss, visited, no_bg=no_bg)
        d["link"] = {"from": mid, "to": link}
        return d

    return _map_export_inner(ws, mid, preview, mobs, density, view, visited,
                             yolo=yolo, seed=seed, outdir=outdir, mj=mj,
                             no_boss=no_boss, no_bg=no_bg)


def _minimap_payload(outdir: Path, root: Path, mj: dict, meta: dict) -> dict | None:
    """把 nxmap 导出的 miniMap 数据整理成前端可直接渲染的小地图。

    客户端 UIMiniMap 的坐标规则（UIMiniMap.cpp）：
        scale = 2^mag；画布像素 = (世界坐标 + centerX/centerY) / scale
    传送门只画 pt=2 的（update_static_markers 同款）；怪/NPC 用 cy 落地，
    与 life_points 一致。坐标换算在这里做完，前端只管按百分比摆点。
    """
    mm = mj.get("miniMap")
    if not (mm and mm.get("img")):
        return None
    canvas = outdir / "img" / str(mm["img"])
    if not canvas.exists():
        return None
    try:
        scale = 2.0 ** float(mm.get("mag", 0))
    except (TypeError, ValueError, OverflowError):
        scale = 1.0
    cx, cy = float(mm.get("centerX", 0)), float(mm.get("centerY", 0))

    def px(x, y) -> dict:
        return {"x": round((float(x) + cx) / scale, 1),
                "y": round((float(y) + cy) / scale, 1)}

    prefix = "/preview/" if root is CACHE else "/exports/"
    portals = [{"pn": p.get("pn", ""),
                **px(p.get("x", 0), p.get("y", 0)),
                "tm": p.get("tm", 0), "tmn": p.get("tmn", "")}
               for p in mj.get("portal", []) if p.get("pt") == 2]
    npcs, mobs = [], []
    for l in mj.get("life", []):
        t = l.get("type")
        if t not in ("m", "n"):
            continue
        y = l.get("cy") if l.get("fh") and l.get("cy") is not None else l.get("y", 0)
        k = str(l.get("id", "")).lstrip("0")
        q = px(l.get("x", 0), y)
        if t == "m":
            mobs.append({**q, "id": k,
                         "name": meta.get(k, {}).get("name") or mob_names().get(k) or "",
                         "tier": meta.get(k, {}).get("tier", "normal")})
        else:
            npcs.append({**q, "id": k})
    return {"url": prefix + canvas.relative_to(root).as_posix(),
            "img_file": str(canvas),
            "w": mm.get("w"), "h": mm.get("h"),
            "cw": mm.get("cw"), "ch": mm.get("ch"),
            "center_x": mm.get("centerX"), "center_y": mm.get("centerY"),
            "mag": mm.get("mag"), "scale": scale,
            "portals": portals, "npcs": npcs, "mobs": mobs}


VIEW_W, VIEW_H = 1366, 768      # 客户端画面窗口，也是唯一的导出尺寸


def _view_origin(view: tuple | None, minx: float, miny: float,
                 bw: float, bh: float) -> tuple[int, int]:
    """导出窗口左上角的世界坐标：按传入值夹在地图内，缺省取地图正中。

    地图比窗口还小就居中（窗口四周留空），拖动/传值都不会把窗口推出地图。
    """
    if view:
        vx, vy = float(view[0]), float(view[1])
    else:
        vx = minx + (bw - VIEW_W) / 2
        vy = miny + (bh - VIEW_H) / 2
    vx = minx + (bw - VIEW_W) / 2 if bw <= VIEW_W else min(max(vx, minx), minx + bw - VIEW_W)
    vy = miny + (bh - VIEW_H) / 2 if bh <= VIEW_H else min(max(vy, miny), miny + bh - VIEW_H)
    return round(vx), round(vy)


def _map_export_inner(ws: Path, mid: str, preview: bool, mobs: bool, density: int,
                      view: tuple | None, visited: set, yolo: bool = False,
                      seed: str | None = None,
                      outdir: Path | None = None, mj: dict | None = None,
                      no_boss: bool = False, no_bg: bool = False) -> dict:
    root = CACHE if preview else EXPORTS
    if outdir is None:
        outdir = (root / "map" / mid) if preview else (EXPORTS / "maps" / mid)
    if mj is None:
        mj = json.loads((outdir / "map.json").read_text(encoding="utf-8"))
    # 无背景版单独一个文件名：预览图是按文件是否存在来复用的，
    # 两种版本共用一个名字会互相命中缓存
    png = outdir / ("preview_nobg.png" if no_bg else "preview.png")
    size = _render_preview(outdir, png, no_bg=no_bg)
    life = [l for l in mj.get("life", []) if l.get("type") == "m"]
    kinds = sorted({str(l.get("id", "")).lstrip("0") for l in life})
    mobnx = ws / "Mob.nx"
    meta = {m["id"]: m for m in mob_meta(ws, mobnx)} if mobnx.exists() else {}
    # 刷怪点叠加：overlay 必须和渲染实际用的一致（render_file pad=40、1:1 不缩放）
    R = _render_mod()
    minx, miny, bw, bh = R.map_bounds(mj)
    minx -= 40; miny -= 40; bw += 80; bh += 80
    s = 1.0                      # 1:1 真实尺寸
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
              "no_bg": bool(no_bg),
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
                                for f in mj.get("foothold", [])],
              # 绳梯段 [x,y1,y2,l]：l=1 梯子 / 0 绳子（都是垂直的，x 单值）
              "ladder_rope": [[lr["x"], lr["y1"], lr["y2"], lr.get("l", 0)]
                              for lr in mj.get("ladderRope", [])]}
    # 导出图带怪物：把怪沿地面随机散布合成进预览图（preview_mobs.png）
    # yolo 但不 mobs = 负样本：图里一只怪都没有（绳梯照常标注）
    use_seed = str(seed) if seed else f"{mid}:{random.randrange(1 << 30):x}"
    places: list[dict] = []
    if mobs and life:
        mob_png, places = _composite_mobs(ws, outdir, mj, png, use_seed,
                                          density=max(1, min(10, int(density or 1))),
                                          no_boss=no_boss, no_bg=no_bg)
        if mob_png is not None:
            rel = (mob_png.relative_to(CACHE) if preview else mob_png.relative_to(EXPORTS))
            result["preview_path"] = str(mob_png)
            result["preview_url"] = (f"/preview/{rel.as_posix()}" if preview
                                     else f"/exports/{rel.as_posix()}")
            result["mobs"] = True
        elif yolo:
            raise CliError("这张图有刷怪点但没找到任何怪的图片，无法做 YOLO 标注", 1)
    # 导出 = 客户端那个固定 1366×768 的画面窗口（预览模式输出整张 1:1 地图，供页面叠窗口用）
    if not preview:
        from PIL import Image
        src = Image.open(Path(result["preview_path"])).convert("RGBA")
        vx, vy = _view_origin(view, minx, miny, bw, bh)
        left, top = round(vx - minx), round(vy - miny)
        canvas = Image.new("RGBA", (VIEW_W, VIEW_H),
                           (0, 0, 0, 0) if no_bg else (16, 18, 24, 255))
        # 窗口可以超出地图边界（小图/边缘），只贴交集，其余留空
        sx0, sy0 = max(0, left), max(0, top)
        sx1 = min(src.width, left + VIEW_W)
        sy1 = min(src.height, top + VIEW_H)
        if sx1 > sx0 and sy1 > sy0:
            canvas.paste(src.crop((sx0, sy0, sx1, sy1)), (sx0 - left, sy0 - top))
        out_png = outdir / ("window_nobg.png" if no_bg else "window.png")
        canvas.convert("RGBA" if no_bg else "RGB").save(out_png)
        rel = (out_png.relative_to(CACHE) if preview else out_png.relative_to(EXPORTS))
        result["preview"] = {"w": VIEW_W, "h": VIEW_H}
        result["preview_path"] = str(out_png)
        result["preview_url"] = (f"/preview/{rel.as_posix()}" if preview
                                 else f"/exports/{rel.as_posix()}")
        result["window"] = {"x": vx, "y": vy, "w": VIEW_W, "h": VIEW_H,
                            "px": {"x": left, "y": top}}
        # 叠加层坐标系改成以窗口左上角为原点，圆点/立绘在窗口图上照样对准
        result["overlay"] = {"minx": vx, "miny": vy, "s": 1.0,
                             "w": VIEW_W, "h": VIEW_H}

    # ---------------------------------------------------------------- YOLO 数据集
    # 每只怪一个框（合成时记录的精确 ground truth）+ 每段绳/梯一个框
    # + 每段地面(foothold)一个框。
    # 产出 exports/yolo/{images,labels}/ + classes.txt + dataset.yaml，
    # 类别表跨导出累积（classes.json 注册表），怪的类别名 = id_英文名。
    if yolo:
        import shutil
        W, H = result["preview"]["w"], result["preview"]["h"]
        # 导出图是 1366×768 窗口，标注框要减去窗口左上角在整图上的像素偏移
        offx, offy = (result["window"]["px"]["x"], result["window"]["px"]["y"]) \
            if result.get("window") else (0, 0)

        def _mob_cls(k: str, state: str = "") -> str:
            nm = meta.get(k, {}).get("name") or mob_names().get(k) or ""
            base = f"{k}_{re.sub(r'[^0-9A-Za-z_]+', '_', nm).strip('_')}" if nm else k
            # 类别名带上动作状态：YOLO 一套检测就同时认「是哪种怪 + 什么状态」
            return f"{base}__{state}" if state else base

        # 类别注册表（跨导出累积，id 稳定）；批量导出是多进程并发的，
        # 注册表读写要拿文件锁，否则并发分配类别 id 会互相覆盖错乱
        ydir = EXPORTS / "yolo"
        (ydir / "images").mkdir(parents=True, exist_ok=True)
        (ydir / "labels").mkdir(parents=True, exist_ok=True)
        reg_file = ydir / "classes.json"
        import time as _time
        lock = ydir / ".classes.lock"
        deadline = _time.time() + 300
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if _time.time() - lock.stat().st_mtime > 300:
                        lock.unlink()      # 陈旧锁（进程崩了没清）：清掉重试
                except FileNotFoundError:
                    pass
                if _time.time() > deadline:
                    break                  # 等不到就继续，绝不死锁
                _time.sleep(0.05)
        try:
            reg: dict[str, int] = {}
            if reg_file.exists():
                try:
                    reg = json.loads(reg_file.read_text(encoding="utf-8"))
                except Exception:
                    reg = {}
            cls_names = {_mob_cls(p["kind"], p.get("state", "stand")) for p in places} \
                | {"rope", "ladder", "ground", "slope"}
            for nm in sorted(cls_names):
                if nm not in reg:
                    reg[nm] = len(reg)

            def _box(x: float, y: float, w: float, h: float) -> tuple | None:
                """预览图像素框 → 最终图上的归一化 YOLO 框；完全在外头的丢弃。"""
                x, y = x - offx, y - offy
                x0, y0 = max(0.0, x), max(0.0, y)
                x1, y1 = min(float(W), x + w), min(float(H), y + h)
                if x1 - x0 < 2 or y1 - y0 < 2:      # 被裁得只剩边角的不算目标
                    return None
                return (((x0 + x1) / 2) / W, ((y0 + y1) / 2) / H,
                        (x1 - x0) / W, (y1 - y0) / H)

            lines = []
            state_cnt: dict[str, int] = {}
            for p in places:
                st = p.get("state", "stand")
                state_cnt[st] = state_cnt.get(st, 0) + 1
                b = _box(p["x"], p["y"], p["w"], p["h"])
                if b:
                    lines.append(f"{reg[_mob_cls(p['kind'], st)]} "
                                 + " ".join(f"{v:.6f}" for v in b))
            for x, y1, y2, l in result["ladder_rope"]:
                b = _box((x - 16 - minx) * s, (y1 - miny) * s, 32 * s, (y2 - y1) * s)
                if b:
                    lines.append(f"{reg['ladder' if l else 'rope']} " + " ".join(f"{v:.6f}" for v in b))
            # 地面（foothold）：每段一个框，从站立线往下盖 32 世界像素——
            # 平台贴图主体在线的下方、怪的脚踩在线上，往下扩既能框住可视平台、
            # 又不会和怪框打架；窄于 8 世界像素的竖直连接段是平台侧壁不可辨识，跳过。
            # 坡度 |dy|/|dx| ≥ 0.2（约 11°）的段标 slope 而非 ground：挂机工具
            # 把 ground 当纯平地用（恒定 Y），斜坡若混进去会走上去回不来。
            for fx1, fy1, fx2, fy2 in result.get("foothold_segs", []):
                if abs(fx2 - fx1) < 8:
                    continue
                top = min(fy1, fy2)
                bot = max(max(fy1, fy2), top + 32)
                b = _box((min(fx1, fx2) - minx) * s, (top - miny) * s,
                         abs(fx2 - fx1) * s, (bot - top) * s)
                if b:
                    steep = abs(fy2 - fy1) / abs(fx2 - fx1) >= 0.2
                    lines.append(f"{reg['slope' if steep else 'ground']} "
                                 + " ".join(f"{v:.6f}" for v in b))
            stem = f"{mid}_{re.sub(r'[^0-9A-Za-z_]+', '_', use_seed)}"
            img_out = ydir / "images" / f"{stem}.png"
            lbl_out = ydir / "labels" / f"{stem}.txt"
            shutil.copyfile(result["preview_path"], img_out)
            lbl_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
            reg_file.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
            # classes.txt（darknet 风格）+ dataset.yaml（ultralytics 用），每次导出全量重写
            inv = sorted(reg.items(), key=lambda kv: kv[1])
            (ydir / "classes.txt").write_text(
                "\n".join(nm for nm, _ in inv) + "\n", encoding="utf-8")
            names_yaml = "\n".join(f"  {i}: {nm}" for nm, i in inv)
            (ydir / "dataset.yaml").write_text(
                f"path: {ydir.resolve().as_posix()}\ntrain: images\nval: images\n"
                f"names:\n{names_yaml}\n", encoding="utf-8")
            result["yolo"] = {"image": str(img_out), "labels": str(lbl_out),
                              "classes": str(ydir / "classes.txt"),
                              "dataset": str(ydir / "dataset.yaml"),
                              "boxes": len(lines),
                              "states": state_cnt}
        finally:
            lock.unlink(missing_ok=True)
    # 游戏内小地图（miniMap canvas + 客户端同款标记换算），Web UI / AI 都能用
    result["name"] = mj.get("name", "")
    result["street_name"] = mj.get("streetName", "")
    result["minimap"] = _minimap_payload(outdir, root, mj, meta)
    return result


def _minimap_composite(export: dict, out: Path, zoom: int = 2) -> Path:
    """把 minimap canvas 放大 zoom 倍，按客户端规则画上标记，存成 PNG。

    标记配色沿用地图详情的约定：传送门=蓝圈、NPC=黄点、
    刷怪点 红=普通怪 / 橙=BOSS / 品红=大BOSS。
    """
    from PIL import Image, ImageDraw
    mm = export.get("minimap")
    if not mm:
        raise CliError("这张图没有 miniMap（hideMinimap=1 或该版本数据缺失）", 1)
    src = Image.open(mm["img_file"]).convert("RGBA")
    img = src.resize((src.width * zoom, src.height * zoom), Image.NEAREST)
    d = ImageDraw.Draw(img)
    dot = max(2, zoom)                      # 实心点半径
    ring = dot + zoom                       # 传送门圈半径
    for n in mm.get("npcs", []):
        x, y = n["x"] * zoom, n["y"] * zoom
        d.ellipse([x - dot, y - dot, x + dot, y + dot],
                  fill=(255, 221, 0, 255), outline=(90, 70, 0, 255))
    tier_color = {"normal": (255, 64, 64, 255),
                  "boss": (255, 140, 0, 255),
                  "big": (255, 0, 255, 255)}
    for m in mm.get("mobs", []):
        x, y = m["x"] * zoom, m["y"] * zoom
        c = tier_color.get(m.get("tier"), tier_color["normal"])
        d.ellipse([x - dot, y - dot, x + dot, y + dot], fill=c)
    for p in mm.get("portals", []):
        x, y = p["x"] * zoom, p["y"] * zoom
        d.ellipse([x - ring, y - ring, x + ring, y + ring],
                  outline=(64, 160, 255, 255), width=max(1, zoom // 2 + 1))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out)
    return out


def c_map_minimap(mid: str, out: str = "", zoom: int = 2,
                  preview: bool = False) -> dict:
    """渲染一张地图的游戏内小地图（canvas + 传送门/刷怪点标记）→ PNG。

    数据走 map-export 的缓存链路（.cache/map/<id>/，二次调用秒回）。
    """
    d = c_map_export(mid, preview=preview)
    target = Path(out) if out else Path(d["dir"]) / "minimap.png"
    mm = d.get("minimap") or {}
    z = max(1, min(8, int(zoom or 2)))
    png = _minimap_composite(d, target, z)
    return {"map": mid,
            "name": d.get("name", ""), "street_name": d.get("street_name", ""),
            "image": str(png),
            "w": mm.get("cw", 0) * z,
            "h": mm.get("ch", 0) * z,
            "zoom": z,
            "portals": len(mm.get("portals", [])),
            "npcs": len(mm.get("npcs", [])),
            "mobs": len(mm.get("mobs", []))}


def cmd_map_minimap(a) -> int:
    d = c_map_minimap(a.id, out=a.out, zoom=a.zoom, preview=a.preview)
    if a.json:
        _out(d, True)
    else:
        nm = f"{d['street_name']} · {d['name']}" if d["name"] else d["map"]
        print(f"小地图 {d['map']}（{nm}）: {d['w']}×{d['h']}（canvas×{d['zoom']}）"
              f" · 传送门 {d['portals']} · NPC {d['npcs']} · 刷怪点 {d['mobs']}")
        print(f"图片: {d['image']}")
    return 0


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
    stale = False
    if (outdir / "map.json").exists():
        # 旧版 nxmap 的缓存没有 miniMap/name 字段 → 视为过期重导一次
        try:
            stale = "miniMap" not in json.loads(
                (outdir / "map.json").read_text(encoding="utf-8"))
        except Exception:
            stale = True
    if stale or not (outdir / "map.json").exists():
        # 同 map-export：覆盖式刷新，不做整目录删除
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
    s = 1.0                  # 渲染改成 1:1（真实尺寸），不再缩到 1600 宽
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
            "ladders": len(mj.get("ladderRope", [])),
            # 地面段 [x1,y1,x2,y2]：可站立区域（含斜坡），导航/跳跃规划用
            "foothold_segs": [[f["x1"], f["y1"], f["x2"], f["y2"]]
                              for f in mj.get("foothold", [])],
            # 绳梯段 [x,y1,y2,l]：l=1 梯子 / 0 绳子
            "ladder_rope": [[lr["x"], lr["y1"], lr["y2"], lr.get("l", 0)]
                            for lr in mj.get("ladderRope", [])]}


def c_probe_batch(ids: list[str], win_w: int = 1366, win_h: int = 768,
                  no_boss: bool = False) -> list[dict]:
    """批量探测：一次进程内算完多张图的「能不能导 1366×768 窗口」。

    批量脚本别逐张调 map-info（每次都得起一个 onefile exe，杀毒+解包开销
    会把并发拖死）——用这个一次全查：读 stdin 的 id 列表，输出 JSON 数组。
    usable=True 表示这张图够大、不缩放、有刷怪，可以随机取窗口导出。
    no_boss=True 时刷怪判定只数非大 BOSS 的怪：只刷大 BOSS（tier=big）的图
    usable=False；高阶怪（tier=boss）算可用怪。
    """
    ws = _need_ws()
    R = _render_mod()
    mobnx = ws / "Mob.nx"
    tier = {m["id"]: m.get("tier", "normal")
            for m in mob_meta(ws, mobnx)} if mobnx.exists() else {}
    out = []
    for mid in (str(i).strip() for i in ids):
        rec: dict = {"id": mid}
        try:
            if not re.fullmatch(r"\d{9}", mid):
                raise CliError("地图 id 应该是 9 位数字", 2)
            visited = set()
            d_mid = mid
            mj = None
            for _ in range(5):                  # 链接图顺链（防环 4 跳）
                if d_mid in visited:
                    raise CliError("link 成环", 1)
                visited.add(d_mid)
                outdir = CACHE / "map" / d_mid
                if not (outdir / "map.json").exists():
                    if outdir.exists():
                        drop_dir(outdir)
                    outdir.parent.mkdir(parents=True, exist_ok=True)
                    _run(["nxmap", d_mid, str(outdir)], ws, timeout=600)
                if not (outdir / "map.json").exists():
                    raise CliError(f"nxmap 没产出 map.json（{d_mid}）", 1)
                mj = json.loads((outdir / "map.json").read_text(encoding="utf-8"))
                link = str(mj.get("info", {}).get("link") or "").strip()
                if link and link != d_mid and re.fullmatch(r"\d{9}", link):
                    d_mid = link
                    continue
                break
            cx, cy, cw, chh = R.map_bounds(mj)
            minx, miny = cx - 40, cy - 40
            bw, bh = cw + 80, chh + 80
            s = 1.0                  # 渲染改成 1:1（真实尺寸），不再缩到 1600 宽
            life = [l for l in mj.get("life", []) if l.get("type") == "m"]
            small = [l for l in life
                     if tier.get(str(l.get("id", "")).lstrip("0"), "normal") != "big"]
            rec.update({"world": {"x": minx, "y": miny, "w": bw, "h": bh},
                        "scale": round(s, 4), "life_count": len(life),
                        "small_count": len(small),
                        "usable": bool((small if no_boss else life)
                                       and s >= 0.999
                                       and bw >= win_w and bh >= win_h)})
            if no_boss and len(small) < len(life):
                rec["boss_filtered"] = len(life) - len(small)
            if d_mid != mid:
                rec["link_to"] = d_mid
        except CliError as e:
            rec["usable"] = False
            rec["error"] = str(e)
        except Exception as e:                   # 单张坏图不拖垮整批
            rec["usable"] = False
            rec["error"] = f"{type(e).__name__}: {e}"
        out.append(rec)
    return out


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
            vp = b.get("view")      # 导出窗口左上角世界坐标 {x, y}；缺省地图正中
            view = None
            if isinstance(vp, dict) and "x" in vp and "y" in vp:
                try:
                    view = (float(vp["x"]), float(vp["y"]))
                except (TypeError, ValueError):
                    view = None
            self._api(c_map_export, str(b.get("id") or ""),
                      bool(b.get("preview")), bool(b.get("mobs")),
                      int(b.get("density") or 1), view,
                      bool(b.get("yolo")),
                      str(b["seed"]) if b.get("seed") else None,
                      bool(b.get("no_boss")), no_bg=bool(b.get("no_bg")))
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


def cmd_probe(a) -> int:
    """批量探测（stdin 一行一个 id）：usable=True 才能随机导 1366×768 窗口。"""
    ids = [ln.strip() for ln in sys.stdin if ln.strip()]
    recs = c_probe_batch(ids, a.w, a.h, no_boss=a.no_boss)
    print(json.dumps(recs, ensure_ascii=False))
    ok = sum(1 for r in recs if r.get("usable"))
    print(f"# 探测 {len(recs)} 张，可用 {ok} 张", file=sys.stderr)
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
    view = None
    if getattr(a, "view", None):
        try:
            parts = [float(v) for v in str(a.view).replace("，", ",").split(",")]
            if len(parts) != 2:
                raise ValueError
            view = tuple(parts)
        except ValueError:
            print("错误: --view 应为 X,Y（导出窗口左上角世界坐标，如 850,-900）",
                  file=sys.stderr)
            return 2
    d = c_map_export(a.id, preview=a.preview, mobs=a.mobs, density=a.density,
                     view=view, yolo=bool(getattr(a, "yolo", False)),
                     seed=getattr(a, "seed", None),
                     no_boss=bool(getattr(a, "no_boss", False)),
                     no_bg=bool(getattr(a, "no_bg", False)))
    if a.json:
        _out(d, True)
    else:
        link = f"（链接图 → 画面同 {d['link']['to']}）" if d.get("link") else ""
        p = d["preview"]
        mobs = " · 已合成怪物" if d.get("mobs") else ""
        win = d.get("window")
        where = f" · 窗口 ({win['x']},{win['y']})" if win else " · 整图 1:1"
        print(f"地图 {a.id}: {p['w']}×{p['h']}{where} · 刷怪 {d['life_count']} · "
              f"地面 {d['footholds']} 段 · 绳梯 {d['ladders']} 段{mobs} {link}")
        print(f"目录: {d['dir']}")
        print(f"预览: {d['preview_path']}")
        if d.get("yolo"):
            y = d["yolo"]
            st = " · ".join(f"{k}×{v}" for k, v in sorted(y.get("states", {}).items()))
            print(f"YOLO: {y['boxes']} 个框 · 图 {y['image']}")
            print(f"      标注 {y['labels']} · 类别 {y['classes']} · 数据集 {y['dataset']}")
            if st:
                print(f"      状态分布: {st}")
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
                              "bounds=内容包围盒；world=加 40px pad 的世界范围（导出窗口就在这里走）。\n"
                              "导出图固定 1366×768（客户端画面），--view 选窗口位置。")
    p.add_argument("id", help="9 位地图 id（链接图自动转向目标图）")
    _j(p); p.set_defaults(fn=cmd_map_info)

    p = sub.add_parser("mob-frames", help="列出一只怪的动画列表与帧数（stand/move/die1…）")
    p.add_argument("id", help="怪 id")
    _j(p); p.set_defaults(fn=cmd_mob_frames)

    p = sub.add_parser("probe", help="批量探测地图可导窗口（stdin 一行一个 id，输出 JSON 数组）",
                       epilog="批量脚本专用：一次进程内查完全部地图，比逐张 map-info 快几十倍。\n"
                              "usable=True 表示够大、不缩放、有刷怪，可随机导 W×H 窗口。")
    p.add_argument("--w", type=int, default=1366, help="窗口宽（默认 1366）")
    p.add_argument("--h", type=int, default=768, help="窗口高（默认 768）")
    p.add_argument("--no-boss", action="store_true",
                   help="刷怪判定只数非大BOSS的怪：只刷大BOSS(tier=big)的图 usable=False（挂机升级场景；高阶怪算可用）")
    _j(p); p.set_defaults(fn=cmd_probe)

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
                   help="预览图合成怪物（沿地面随机散布、贴地不悬空；动作状态也随机：站/走/攻击/受击/死亡等，默认每次随机，--seed 可复现）")
    p.add_argument("--yolo", action="store_true",
                   help="产出 YOLO 训练集（exports/yolo/：images+labels+classes.txt"
                        "+dataset.yaml；每只怪一个框（类别=id_名字__动作状态）、"
                        "每段绳/梯一个框、每段地面(foothold)一个框——"
                        "平地=ground、坡度≥0.2(约11°)=slope 两类分开；"
                        "配 --mobs 出正样本，不带 --mobs 出负样本（没怪，绳梯地面照常标注））")
    p.add_argument("--seed", metavar="S", help="配合 --mobs：固定随机种子（同 seed 同布局；缺省每次随机）")
    p.add_argument("--no-boss", action="store_true",
                   help="配合 --mobs/--yolo：不合成/标注大BOSS（tier=big，HP≥1000万；高阶怪照常出现，挂机升级不打大BOSS）；"
                        "只刷大BOSS的图会得到没有怪的图")
    p.add_argument("--density", type=int, default=1, metavar="N",
                   help="配合 --mobs：怪物密度倍数（1-10，默认 1；每个刷怪点放 N 只，左右散开）")
    p.add_argument("--no-bg", action="store_true",
                   help="不画背景（天幕/云/山等 back 层全跳过，画布透明），只出地形+建筑+物件；"
                        "输出 window_nobg.png")
    p.add_argument("--view", metavar="X,Y",
                   help="导出窗口左上角的世界坐标（默认地图正中）。导出图固定 1366×768 —— "
                        "和客户端画面同尺寸，只此一种；含负数时必须用 = 形式，如 --view=-1500,300")
    _j(p); p.set_defaults(fn=cmd_map_export)

    p = sub.add_parser("map-minimap",
                       help="渲染游戏内小地图（miniMap canvas + 标记）→ PNG",
                       epilog="与客户端 UIMiniMap 同款算法：WZ 预烘焙的 miniMap/canvas\n"
                              "按 2^mag 换算坐标，叠上传送门(蓝圈)/NPC(黄点)/刷怪点(红橙品红)。\n"
                              "数据走 map-export 缓存（.cache/map/<id>/），二次调用秒回。")
    p.add_argument("id", help="9 位地图 id（链接图自动转向目标图）")
    p.add_argument("--zoom", type=int, default=2, metavar="N",
                   help="canvas 放大倍数（1-8，默认 2；1x=WZ 原始尺寸）")
    p.add_argument("--out", default="", metavar="PATH", help="输出 PNG 路径（默认 exports/maps/<id>/minimap.png）")
    p.add_argument("--preview", action="store_true", help="数据只进预览缓存，不落 exports")
    _j(p); p.set_defaults(fn=cmd_map_minimap)

    p = sub.add_parser("clear-cache", help="清空 .cache 预览缓存（exports 不动）")
    _j(p); p.set_defaults(fn=cmd_clear_cache)

    p = sub.add_parser("web", help="启动本地 Web 页面（浏览器界面，人看；程序调用请用上面的子命令）",
                       epilog="页面与 CLI 共用同一套导出逻辑；端口默认 8000。")
    p.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    p.set_defaults(fn=cmd_web)

    return ap


def main(argv: list[str] | None = None) -> int:
    # stdout 重定向/管道时 Windows 默认用本地 GBK，怪名里有韩文等字符会直接
    # UnicodeEncodeError 崩掉——统一改成 UTF-8（批量脚本都按 UTF-8 解码）
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
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
