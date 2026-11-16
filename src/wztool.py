# -*- coding: utf-8 -*-
"""wztool —— WzToolkit 命令行工具（CLI-first，供人类与其他程序/AI 直接调用）。

功能：导入 .wz → 转成 .nx → 浏览导出怪物素材（逐帧动画 PNG + 锚点 manifest）与地图
（map.json / 贴图 / 渲染预览 / 客户端画面窗口 / 小地图 / 截图找图）。
不含训练集、挂机之类的功能。
所有子命令都支持 --json 机器可读输出；退出码：0 成功，1 业务错误，2 参数错误。

常用示例::

    wztool workspace "D:/MapleStory/nxrun"        # 1. 设置工作区（放 .wz/.nx 的目录；
                                                  #    也可以直接选国服客户端目录
                                                  #    如 ...\\冒险岛online\\mxdclassic，
                                                  #    会自动定位旁边 mxd\\Data）
    wztool convert Map.wz Mob.wz String.wz        # 2. wz → nx（耗时任务，同步执行；
                                                  #    国服模式自动做全量转换到 .cache\\cn）
    wztool mobs --json                            # 3. 列出全部怪物（JSON）
    wztool mob-export 0100100                     # 4. 导出一只怪的全部动画帧
    wztool maps --json                            # 5. 列出全部地图（JSON）
    wztool map-export 000050000 --mobs            # 6. 导出地图（预览图带怪物）
    wztool map-minimap 000050000                  # 7. 渲染游戏内小地图
    wztool web                                    # 8. 启动 Web 页面（人看，可选；
                                                  #    双击 wztool.exe 不带参数 = 这条）

坐标与文件格式与客户端一致：怪物帧 PNG 带锚点(ox,oy=脚底中心)，
地图 map.json 含刷怪表(life)/地面(foothold)/绳梯(ladderRope)。
"""
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import subprocess
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 图片找图只用到 cv2 + numpy；缺了别拦着其它功能，调用时再报错
try:
    import cv2
    import numpy as np
except ImportError:                            # pragma: no cover
    cv2 = None
    np = None

# ---------------------------------------------------------------- 路径与常量

if getattr(sys, "frozen", False):
    # PyInstaller 打包后：exe 可以在项目根，也可以放在 bin\ 里
    # （bin\wztool.exe 和 nxmap.exe 这些原生工具放一起）。往上找带 vendor\ / webui\
    # 的那层当项目根，这样只有一份判断、其它路径（bin\.cache\.exports）全都对。
    _exe_dir = Path(sys.executable).resolve().parent
    _ME = getattr(sys, "_MEIPASS", None)        # 打包进去的纯资源走临时解压目录
    ROOT = _exe_dir
    for _cand in (_exe_dir, _exe_dir.parent):
        if (_cand / "vendor").exists() or (_cand / "webui").exists():
            ROOT = _cand
            break
    VENDOR = Path(_ME) / "vendor" if _ME and (Path(_ME) / "vendor").exists() else ROOT / "vendor"
else:
    # 源码模式：本文件在 src\ 里，往上找带 vendor\ / webui\ 的那层当项目根
    ROOT = Path(__file__).resolve().parent
    for _cand in (ROOT, *ROOT.parents):
        if (_cand / "vendor").exists() or (_cand / "webui").exists():
            ROOT = _cand
            break
    VENDOR = ROOT / "vendor"

BIN = ROOT / "bin"
CACHE = ROOT / ".cache"
EXPORTS = ROOT / ".exports"
WEBUI = ROOT / "webui"                          # Web UI 页面目录（cli.html）
if not WEBUI.exists() and getattr(sys, "frozen", False):   # 举例：exe 单独丢在别处
    WEBUI = Path(getattr(sys, "_MEIPASS", ROOT)) / "webui"
STATE = ROOT / "workspace.json"                # 记住上次选的工作区
CN_NX = CACHE / "cn"                           # 国服官方数据转换出的 .nx（写安装目录没权限）

TOOLS = ("NoLifeWzToNx", "nxmap", "nxmob", "nxscan", "nxchar")


class CliError(Exception):
    """业务错误：message 给人/AI 看，code 是建议的退出码。"""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 公共状态

_lock = threading.Lock()
_ws: Path | None = None                        # 工作区（放 .wz / .nx 的目录，或国服客户端目录）
_job: dict = {"id": 0, "running": False, "log": [], "done": 0, "total": 0,
             "error": "", "stage": ""}
_lists: dict = {}                              # mobs / maps / life 缓存（键: 文件 mtime）
_BOSS_REF: dict | None = None                  # boss 参照表懒加载
_MOB_STATS: dict | None = None                 # 服务端怪物数值表懒加载
_UNSET = object()                              # 国服探测哨兵：None=确认不是，对象=还没探测
_cn_data: Path | None | object = _UNSET


def _save_ws() -> None:
    STATE.write_text(json.dumps({"workspace": str(_ws) if _ws else ""}),
                     encoding="utf-8")


def _load_ws() -> None:
    global _ws
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        raw = (d.get("workspace") or "").strip()
        # 注意：Path("") 会变成 Path(".")（当前目录）——空值必须在这里挡掉
        p = Path(raw) if raw and raw != "." else None
        _ws = p if (p and p.is_dir()) else None
    except (OSError, ValueError):
        _ws = None


_load_ws()


def tools_ok() -> bool:
    return all((BIN / f"{n}.exe").exists() for n in TOOLS)


def _need_tools() -> None:
    if not tools_ok():
        raise CliError("bin\\ 里缺原生工具（NoLifeWzToNx/nxmap/nxmob/nxscan/nxchar）—— "
                       "在项目目录跑一次 scripts\\build.bat，它会把缺的编出来", 1)


# ---------------------------------------------------------------- 国服官方数据源

def _cn_detect(p: Path | None) -> Path | None:
    """给定用户选的目录，找出国服官方客户端的 Data 目录（没有则 None）。

    认这几种选法：mxd\\Data 本身、mxd 根、mxdclassic（位图在旁边 mxd\\Data）、
    冒险岛online 根。判定标志：Map/Map/Map0/Map0_000.wz 存在。
    """
    if p is None:
        return None

    def ok(d: Path) -> bool:
        return (d / "Map" / "Map" / "Map0" / "Map0_000.wz").is_file()

    cands = [p / "Data", p]
    up = p
    for _ in range(3):                          # 向上找几层（覆盖 mxdclassic / 安装根）
        up = up.parent
        if up == up.parent:
            break
        cands += [up / "mxd" / "Data", up / "Data"]
    for c in cands:
        if ok(c):
            return c
    return None


def _cn_mode() -> Path | None:
    """当前工作区是不是国服官方数据源；是则返回 Data 目录（带缓存）。"""
    global _cn_data
    if _cn_data is _UNSET:
        _cn_data = _cn_detect(_ws)
    return _cn_data if isinstance(_cn_data, Path) else None


def _nx_dir() -> Path:
    """原生工具的运行目录（.nx 所在处）。

    classic 模式 = 工作区（用户自己放 .wz 转出来的 .nx）；
    国服模式 = .cache\\cn（从安装目录转换出来的成品，安装目录只读）。
    """
    cn = _cn_mode()
    if cn is not None:
        return CN_NX
    return _need_ws()


def _need_ws() -> Path:
    if _ws is None:
        raise CliError("还没有设置工作区：先运行 wztool workspace <放 .wz 的目录>", 1)
    return _ws


def _run(args: list[str], cwd: Path, timeout: int = 1200) -> str:
    """跑一个原生工具（以工作区为 cwd，工具按 cwd 定位 .nx），返回 stdout。
    非零退出码 = 失败，把 stderr 尾巴带进异常（否则上层只会报含糊的"没有数据"）。"""
    exe = BIN / (args[0] + ".exe")
    if not exe.exists():
        raise CliError(f"缺原生工具 {exe.name}（跑一次 scripts\\build.bat 会自动编译）", 1)
    r = subprocess.run([str(exe)] + args[1:], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if r.returncode != 0:
        tail = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-5:])
        raise CliError(f"{exe.name} {' '.join(args[1:3])} 失败（exit {r.returncode}）\n{tail}", 1)
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
    # v2 = 渲染器改成客户端顺序（同层 obj 先画、tile 后画）；
    # v3 = 背景平铺去掉「拉伸补缝」，对齐客户端 MapBackgrounds 的原始尺寸平铺；
    # v4 = 同层 tile/obj 按 z 归并（tile 的 z 改从瓦片图节点取）
    # v5 = 撤销 v4 的归并：照客户端 TilesObjs::draw 同层 obj 全画完再画 tile
    #      （z 归并会让 L3/L5 的物件盖住地面瓦片，岩路III/军营实测）
    meta = out_png.with_suffix(".meta")
    stamp = f"native-1x-v5 no_bg={int(bool(no_bg))}\n"
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
    cn = _cn_mode()
    return {"workspace": str(_ws) if _ws else None,
            "cn": {"data": str(cn), "nx_dir": str(CN_NX)} if cn else None,
            "cls": _cls_state(),
            "dmg": _dmg_state(),
            "tools_ok": tools_ok(),
            "exports": str(EXPORTS),
            "ui": c_ui()["ui"]}


def c_ui() -> dict:
    """前端版本戳 = cli.html 的 mtime。页面定时比对，发现新版自动刷新
    （否则重新打包后，开着的旧标签页会一直停在老界面上）。"""
    try:
        return {"ui": int((WEBUI / "cli.html").stat().st_mtime)}
    except OSError:
        return {"ui": 0}


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


def c_pickdir(initial: str = "") -> dict:
    """弹 Windows **现代**「选择文件夹」对话框（IFileOpenDialog + FOS_PICKFOLDERS，
    跟资源管理器/「另存为」同款：左侧导航栏、地址栏、搜索框、可新建文件夹）。
    initial 为初始定位目录；取消返回 {"path": ""}。

    全用 ctypes 直调 COM，零第三方依赖（PyInstaller 不用带 tkinter）。
    两个 64 位坑必须显式声明类型，否则指针被截成 32 位：
      - GetForegroundWindow 的返回值（HWND）
      - COM 接口方法（一律按 c_void_p 传 this + 手工走虚表）
    """
    import ctypes
    from ctypes import wintypes

    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32
    HR = ctypes.c_long                     # HRESULT（32 位有符号）

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    ole32.CLSIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(GUID)]
    ole32.CLSIDFromString.restype = HR
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    user32.GetForegroundWindow.restype = wintypes.HWND

    def _guid(lit: str) -> GUID:
        g = GUID()
        ole32.CLSIDFromString(lit, ctypes.byref(g))
        return g

    def _vt(obj, idx: int, restype, *argtypes):
        """取 COM 对象虚表第 idx 项，包成可调用函数（第一个参数固定为 this）。"""
        vt = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vt[idx])

    clsid = _guid("{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}")     # CLSID_FileOpenDialog
    iid = _guid("{D57C7288-D4AD-4768-BE02-9D969532D960}")       # IID_IFileOpenDialog
    iid_item = _guid("{43826D1E-E718-42EE-BC55-A1E261C37BFE}")  # IID_IShellItem

    hr_init = ole32.CoInitializeEx(None, 0x2)                   # COINIT_APARTMENTTHREADED
    try:
        dlg = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1,
                                    ctypes.byref(iid), ctypes.byref(dlg))
        if hr < 0 or not dlg:
            return _pickdir_legacy()                            # 极老系统才走这里
        release = _vt(dlg, 2, ctypes.c_ulong)                   # IUnknown::Release
        try:
            get_opt = _vt(dlg, 10, HR, ctypes.POINTER(ctypes.c_uint))   # GetOptions
            set_opt = _vt(dlg, 9, HR, ctypes.c_uint)                    # SetOptions
            set_title = _vt(dlg, 17, HR, ctypes.c_wchar_p)              # SetTitle
            set_folder = _vt(dlg, 12, HR, ctypes.c_void_p)              # SetFolder
            show = _vt(dlg, 3, HR, wintypes.HWND)                       # IModalWindow::Show
            get_result = _vt(dlg, 20, HR, ctypes.POINTER(ctypes.c_void_p))

            opt = ctypes.c_uint(0)
            if get_opt(dlg, ctypes.byref(opt)) >= 0:
                # NOCHANGEDIR(0x8)|PICKFOLDERS(0x20)|FORCEFILESYSTEM(0x40)|PATHMUSTEXIST(0x800)
                set_opt(dlg, opt.value | 0x8 | 0x20 | 0x40 | 0x800)
            set_title(dlg, "选择工作区 —— 游戏客户端目录（mxdclassic / 冒险岛online）")

            if initial:                                          # 打开时定位到上次的目录
                psi = ctypes.c_void_p()
                shell32.SHCreateItemFromParsingName.restype = HR
                shell32.SHCreateItemFromParsingName.argtypes = [
                    ctypes.c_wchar_p, ctypes.c_void_p,
                    ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
                if shell32.SHCreateItemFromParsingName(
                        initial, None, ctypes.byref(iid_item), ctypes.byref(psi)) >= 0 and psi:
                    set_folder(dlg, psi)
                    _vt(psi, 2, ctypes.c_ulong)(psi)             # Release

            if show(dlg, user32.GetForegroundWindow()) < 0:       # 用户点了取消
                return {"path": ""}
            item = ctypes.c_void_p()
            if get_result(dlg, ctypes.byref(item)) < 0 or not item:
                return {"path": ""}
            try:
                get_name = _vt(item, 5, HR, ctypes.c_uint,
                               ctypes.POINTER(ctypes.c_wchar_p))  # IShellItem
                buf = ctypes.c_wchar_p()
                if get_name(item, 0x80058000, ctypes.byref(buf)) < 0:   # SIGDN_FILESYSPATH
                    return {"path": ""}
                out = buf.value or ""
                ole32.CoTaskMemFree(ctypes.cast(buf, ctypes.c_void_p))
            finally:
                _vt(item, 2, ctypes.c_ulong)(item)               # Release
            p = Path(out)
            return {"path": str(p)} if p.is_absolute() and p.is_dir() else {"path": ""}
        finally:
            release(dlg)
    finally:
        if hr_init >= 0:
            ole32.CoUninitialize()


def _pickdir_legacy() -> dict:
    """兜底：老式 SHBrowseForFolder（仅在 IFileOpenDialog 创建失败时使用）。"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32
    ole32.CoInitializeEx(None, 0x2)
    try:
        class BROWSEINFO(ctypes.Structure):
            _fields_ = [("hwndOwner", wintypes.HWND),
                        ("pidlRoot", ctypes.c_void_p),
                        ("pszDisplayName", ctypes.c_wchar_p),
                        ("lpszTitle", ctypes.c_wchar_p),
                        ("ulFlags", ctypes.c_uint),
                        ("lpfn", ctypes.c_void_p),
                        ("lParam", ctypes.c_void_p),
                        ("iImage", ctypes.c_int)]
        shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
        shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFO)]
        shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        user32.GetForegroundWindow.restype = wintypes.HWND
        bi = BROWSEINFO()
        bi.hwndOwner = user32.GetForegroundWindow()
        bi.lpszTitle = "选择工作区：游戏客户端目录（mxdclassic / 冒险岛online）"
        disp = ctypes.create_unicode_buffer(wintypes.MAX_PATH)  # 必须留引用，否则被 GC
        bi.pszDisplayName = ctypes.cast(disp, ctypes.c_wchar_p)
        bi.ulFlags = 0x1 | 0x40
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return {"path": ""}
        out = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        ok = shell32.SHGetPathFromIDListW(pidl, ctypes.cast(out, ctypes.c_wchar_p))
        ole32.CoTaskMemFree(pidl)
        p = Path(out.value) if ok else None
        return {"path": str(p)} if (p and p.is_absolute() and p.is_dir()) else {"path": ""}
    finally:
        ole32.CoUninitialize()


def c_workspace(path: str) -> dict:
    """设为工作区。classic 模式列里面的 .wz（含是否已转 .nx）；
    选的是国服官方客户端目录时自动切国服模式（数据源 = 自动定位的 Data 目录）。"""
    global _ws, _cn_data
    p = Path(path or "")
    if not p.is_dir():
        raise CliError(f"不是目录：{p}", 2)
    _ws = p
    _cn_data = _UNSET
    _lists.clear()
    try:
        _save_ws()
    except OSError as e:
        raise CliError(f"工作区已设置但没能写入 {STATE.name}: {e}", 1)
    cn = _cn_mode()
    if cn is not None:
        map_nx = CN_NX / "Map.nx"
        mob_nx = CN_NX / "Mob.nx"
        char_nx = CN_NX / "Character.nx"
        skill_nx = CN_NX / "Skill.nx"

        def gb(f: Path) -> float:
            return round(f.stat().st_size / 1073741824, 1) if f.exists() else 0
        return {"workspace": str(p),
                "cn": {"data": str(cn),
                       "nx_dir": str(CN_NX),
                       "map_nx": map_nx.exists(),
                       "map_nx_mb": round(map_nx.stat().st_size / 1048576) if map_nx.exists() else 0,
                       "string_nx": (CN_NX / "String.nx").exists(),
                       "mob_nx": mob_nx.exists(),
                       "mob_nx_gb": gb(mob_nx),
                       "char_nx": char_nx.exists(),
                       "char_nx_gb": gb(char_nx),
                       "skill_nx": skill_nx.exists(),
                       "skill_nx_gb": gb(skill_nx)},
                # 怀旧服小地图库状态（工作区= mxdclassic / 安装根时能定位 Unity 数据包）
                "cls": _cls_state(),
                # 伤害数字库状态（Effect/BasicEff.img 的 No* 数字集）
                "dmg": _dmg_state()}
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
    """把 .wz 转成 .nx。CLI 同步跑；web 传一个往 _job 里记日志的 log。

    工作区是国服官方数据源时忽略 names，直接做国服全量转换（Map.nx + String.nx）。
    """
    if _cn_mode() is not None:
        return c_convert_cn(log)
    ws = _need_ws()
    todo = _convert_pick(ws, names, force)
    if not todo:
        return {"started": False, "total": 0,
                "message": "选中的都已是 .nx（要重转加 --force）"}
    for wz in todo:
        _convert_one(wz, log)
    return {"started": True, "total": len(todo), "done": len(todo)}


# 国服官方客户端转换（.cache\cn\）-------------------------------------------

def _cn_wz2nx(work: Path, src: str, out: str, mounts: list[tuple[str, str]],
              prefix: str, only: str | None, log, timeout: int = 14400,
              sub=None) -> None:
    """调 NoLifeWzToNx 转一个国服 WZ 卷（work 是 ASCII 可见的卷根），产物写到 out。

    prefix 为空 = 不加 --prefix（img 直接落在 .nx 根，Mob.nx 用这种经典布局）。
    sub(text, size) 可选：流式刷新点——wz2nx 每进入一个阶段（解析输入/写入位图…）
    报一次，另有线程每 2 秒轮询产物大小报"已写 X MB"，让单项转换几十分钟里
    进度条也能动（wz2nx 本身不打百分比）。"""
    exe = BIN / "NoLifeWzToNx.exe"
    cmd = [str(exe), "-c", src]
    if prefix:
        cmd += ["--prefix", prefix]
    for name, path in mounts:
        cmd += ["--mount", f"{name}={path}"]
    if only:
        cmd += ["--only", only]
    cmd += ["--out", out]
    log(f"  转换 {src}（挂载 {len(mounts)} 个卷）…")
    t0 = time.time()
    outp = Path(out)
    if sub is None:
        sub = lambda text, size: None        # noqa: E731
    stop = threading.Event()
    state = {"stage": ""}

    def poll_size() -> None:
        # 产物 .nx 边写边长（先节点/字符串，位图持续 append）——大小就是最诚实的活动信号
        while not stop.wait(2.0):
            if time.time() - t0 > timeout:       # 原 subprocess.run 的超时语义
                try:
                    p.kill()
                except BaseException:
                    pass
                return
            try:
                sz = outp.stat().st_size if outp.exists() else 0
            except OSError:
                sz = 0
            txt = f"已写 {sz / 1048576:.0f} MB"
            if state["stage"]:
                txt = f"{state['stage']} · {txt}"
            sub(txt, sz)

    threading.Thread(target=poll_size, daemon=True).start()
    p = None
    STAGES = {"Parsing input": "解析输入", "Parsing uol": "解析 UOL",
              "Parsing source": "解析源卷", "Parsing _outlink": "解析 outlink",
              "Parsing _inlink": "解析 inlink", "Opening output": "打开输出",
              "Writing nodes": "写入节点", "Writing strings": "写入字符串",
              "Writing audio": "写入音频", "Writing bitmaps": "写入位图"}
    try:
        p = subprocess.Popen(cmd, cwd=str(work), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, bufsize=0)
        seen: set[str] = set()
        buf = b""
        while True:
            chunk = p.stdout.read(256) if p.stdout else b""
            if not chunk:
                break
            buf += chunk
            for k, cn in STAGES.items():
                if k.encode() in buf and cn not in seen:
                    seen.add(cn)
                    state["stage"] = cn
                    sub(cn, -1)          # 阶段细节走 sub 追加到进度条文案，不再刷日志占屏
            if len(buf) > 8192:
                buf = buf[-4096:]
        rc = p.wait()
    finally:
        stop.set()
    if rc != 0 or not outp.exists():
        tail = "\n".join(buf.decode("utf-8", "replace").strip().splitlines()[-5:])
        raise RuntimeError(f"{src} 转换失败（exit {rc}）\n{tail}")
    log(f"  OK {outp.name} {outp.stat().st_size / 1048576:.0f} MB，"
        f"用时 {time.time() - t0:.0f}s")


def _cn_workdir(data: Path) -> Path:
    """wz2nx 用 ANSI 接口打开文件，中文安装路径打不开——
    非 ASCII 时建一个 ASCII 目录联接（.cache\\cn\\data）指过去再转。"""
    try:
        str(data).encode("ascii")
        return data
    except UnicodeEncodeError:
        link = CN_NX / "data"
        if not link.exists():
            link.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(data)],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if not link.exists():
                raise CliError(f"建目录联接失败：{r.stderr.strip()}", 1)
        return link


def c_convert_cn(log, only: list[str] | None = None, prog=None) -> dict:
    """国服官方客户端 → .cache\\cn\\：Map.nx（10 个地图分片 + Back/Tile/Obj 全量位图）
    + String.nx（地图名等中文表）+ Mob.nx（全部怪物素材——怪物的 canvas 分片本身
    就带完整帧结构）+ Character.nx（人物光板/头发/脸/装备部位，供 nxchar 用）。
    产物是经典布局，nxmap/nxmob/nxscan/nxchar 直接可用。
    Map.nx 要解压并重压约 9 GB 位图，一次大约 10~40 分钟（做完永久缓存）。

    only 非空 = 只转这几项（按需转换，用户点哪行的「转换」就转哪个）。
    prog(stage, done) 可选：每开始一项回调一次（done = 已完成的项数），
    供 WebUI 进度条实时显示当前在转哪个，不然 Map.nx 一转几十分钟进度条不动。"""
    data = _cn_mode()
    if data is None:
        raise CliError("不是国服官方数据源", 1)
    _need_tools()
    CN_NX.mkdir(parents=True, exist_ok=True)
    work = _cn_workdir(data)
    map_nx = CN_NX / "Map.nx"
    str_nx = CN_NX / "String.nx"
    mob_nx = CN_NX / "Mob.nx"
    char_nx = CN_NX / "Character.nx"
    skill_nx = CN_NX / "Skill.nx"
    todo = []
    for name, path in (("String.nx", str_nx), ("Map.nx", map_nx), ("Mob.nx", mob_nx),
                       ("Character.nx", char_nx), ("Skill.nx", skill_nx)):
        if only and name not in only:
            continue
        if not path.exists():
            todo.append(name)
    if not todo:
        return {"started": False, "total": 0,
                "message": "国服数据已转换完成（.cache\\cn）"}

    # 单项内部进度：wz2nx 不打百分比，用"产物已写大小 / 该项预估大小"估一个，
    # 估算不必精确（目的只是让进度条在几十分钟里持续动起来）
    EST_MB = {"String.nx": 35, "Map.nx": 21267, "Mob.nx": 8100,
              "Character.nx": 1900, "Skill.nx": 15000}
    cur = {"name": ""}

    def _start(name: str) -> None:
        cur["name"] = name
        if prog:
            prog(f"转换 {name}（第 {todo.index(name) + 1}/{len(todo)} 项）",
                 todo.index(name), 0.0)

    def _sub(text: str, size: int) -> None:
        if not prog or not cur["name"]:
            return
        est = EST_MB.get(cur["name"], 1000) * 1048576
        frac = min(0.98, size / est) if size > 0 else 0.0
        prog(f"转换 {cur['name']}（第 {todo.index(cur['name']) + 1}/{len(todo)} 项）· {text}",
             todo.index(cur["name"]), frac)

    if "String.nx" in todo:
        _start("String.nx")
        _cn_wz2nx(work, "String/String_000.wz", str(str_nx), [], "String", None, log,
                  sub=_sub)
    if "Map.nx" in todo:
        _start("Map.nx")
        # 地图分卷在 Map/Map/Map{n}/（两层 Map）——旧代码写成 Map/Map{n}/ 找不到卷，
        # 9 个分卷全部静默挂成空目录，Map.nx 里只剩 Map0 的 87 张平铺图
        m = [("Map/Map/Map0/_Canvas", "Map/Map/Map0/_Canvas")]
        for n in range(1, 10):
            m += [(f"Map/Map{n}", f"Map/Map/Map{n}"),
                  (f"Map/Map/Map{n}/_Canvas", f"Map/Map/Map{n}/_Canvas")]
        m += [("Map/Back", "Map/Back/Back_000.wz"), ("Map/Back/_Canvas", "Map/Back/_Canvas"),
              ("Map/Tile", "Map/Tile/Tile_000.wz"), ("Map/Tile/_Canvas", "Map/Tile/_Canvas"),
              ("Map/Obj", "Map/Obj/Obj_000.wz"), ("Map/Obj/_Canvas", "Map/Obj/_Canvas")]
        log("  Map.nx 是全量转换（含全部物件/背景位图），大约 10~40 分钟，请耐心等 …")
        _cn_wz2nx(data, "Map/Map/Map0/Map0_000.wz", str(map_nx), m, "Map", None, log,
                  sub=_sub)
    if "Mob.nx" in todo:
        _start("Mob.nx")
        # 普通怪的 WZ 卷是空的（Mob.wz 只索引事件系统），全部怪物都在 6 个
        # canvas 分片里（结构+位图一体、无外链）。基卷选 000，其余 5 片以空名
        # 挂载（= 并进 .nx 根），合成经典布局 Mob/<id>.img，nxmob 直接可用。
        m = [("", f"Mob/_Canvas/_Canvas_00{n}.wz") for n in range(1, 6)]
        log("  Mob.nx 全量转换（约 10 GB 位图，15 分钟上下）…")
        _cn_wz2nx(work, "Mob/_Canvas/_Canvas_000.wz", str(mob_nx), m, "", None, log,
                  sub=_sub)
    if "Character.nx" in todo:
        _start("Character.nx")
        # 人物：基卷 Character_000.wz 是光板身体/头（000020XX / 000120XX）的结构，
        # 位图在同级 _Canvas；每个部位目录（Hair/Face/Coat/…）各有一份结构卷
        # <P>_000.wz + <P>/_Canvas 位图卷。全部以空名/部位名挂到 .nx 根，得到
        # 经典布局 Character/<id>.img 与 Character/<P>/<id>.img，nxchar 直接可用。
        m = [("_Canvas", "Character/_Canvas/_Canvas_000.wz")]
        for p in ("Hair", "Face", "Cap", "Coat", "Pants", "Longcoat",
                  "Shoes", "Glove", "Cape", "Shield", "Weapon"):
            m += [(p, f"Character/{p}/{p}_000.wz"),
                  (f"{p}/_Canvas", f"Character/{p}/_Canvas/_Canvas_000.wz")]
        log("  Character.nx 全量转换（约 1.8 GB 位图，10 分钟上下）…")
        _cn_wz2nx(work, "Character/Character_000.wz", str(char_nx), m, "", None, log,
                  sub=_sub)
    if "Skill.nx" in todo:
        _start("Skill.nx")
        # 技能：结构与位图都在 8 个 canvas 分片里（Skill.wz 本体是空壳）。基卷选
        # 000，其余 7 片空名挂载并进 .nx 根；MobSkill/Dragon/AbyssExpedition/
        # Roguelike 子卷用命名挂载（独立子树）——空名 merge 多卷时 wz2nx 的加密
        # key 推断会互相干扰（"I give up"），命名子树实测稳定。产物根下直接是
        # <职业>.img，nxskill 直接可用。
        m = [("", f"Skill/_Canvas/_Canvas_00{n}.wz") for n in range(1, 8)]
        m += [("MobSkill", "Skill/MobSkill/_Canvas/_Canvas_000.wz"),
              ("Dragon", "Skill/Dragon/_Canvas/_Canvas_000.wz"),
              ("Abyss", "Skill/AbyssExpedition/Skill/_Canvas/_Canvas_000.wz"),
              ("AbyssArchive", "Skill/AbyssExpedition/Skill/skill_archive/_Canvas/_Canvas_000.wz"),
              ("Roguelike", "Skill/Roguelike/Skill/_Canvas/_Canvas_000.wz"),
              ("RoguelikeRed", "Skill/Roguelike/Skill/Redmoon/_Canvas/_Canvas_000.wz"),
              ("RoguelikeBuff", "Skill/Roguelike/Buff/_Canvas/_Canvas_000.wz"),
              ("RoguelikeBuffRed", "Skill/Roguelike/Buff/Redmoon/_Canvas/_Canvas_000.wz")]
        log("  Skill.nx 全量转换（约 15 GB 位图，30 分钟上下）…")
        _cn_wz2nx(work, "Skill/_Canvas/_Canvas_000.wz", str(skill_nx), m, "", None, log,
                  sub=_sub, timeout=7200)
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


def _boss_ref() -> dict:
    """静态参照表：国际服数据里 boss=1 的怪 id → [hp, level]（src/mob_boss_ref.json）。

    国服数值数据在加密的 Mob_00000.ms 里读不出来（boss/level/hp 全是 -1），
    BOSS 档位靠这张表按 id 回退。id 语义各服通用，命中即可信。
    """
    global _BOSS_REF
    if _BOSS_REF is None:
        # 源码跑在 src/ 旁；PyInstaller onefile 解包到 sys._MEIPASS 根
        cands = [Path(__file__).parent / "mob_boss_ref.json"]
        me = getattr(sys, "_MEIPASS", "")
        if me:
            cands.append(Path(me) / "mob_boss_ref.json")
        _BOSS_REF = {}
        for c in cands:
            try:
                _BOSS_REF = json.loads(c.read_text(encoding="utf-8"))
                break
            except Exception:
                continue
    return _BOSS_REF


def _mob_stat_table() -> dict[str, list]:
    """服务端怪物数值表 vendor/mobstats.json：id → [level, hp, exp, boss]。

    国服客户端 NX 里 Mob 只有贴图，等级/血量/经验全在服务端
    （MapleLifeFactory.java:142 从 wz/<id>/info 读 level）。
    scripts/gen-mobstats.py 从服务端 Odin imgdir XML 提取生成，随仓库分发。
    """
    global _MOB_STATS
    if _MOB_STATS is None:
        _MOB_STATS = {}
        try:
            _MOB_STATS = json.loads((VENDOR / "mobstats.json").read_text(encoding="utf-8"))
        except Exception:
            pass
    return _MOB_STATS


def _apply_mob_stats(items: list[dict]) -> None:
    """用服务端数值表补齐 nxmob 读不到的 level/hp/exp/boss（比 boss 参照表全）。"""
    st = _mob_stat_table()
    if not st:
        return
    for it in items:
        row = st.get(it["id"])
        if not row:
            continue
        lv, hp, exp, boss = row
        if lv > 0 and (it.get("level") or 0) <= 0:
            it["level"] = lv
        if hp > 0 and (it.get("hp") or 0) <= 0:
            it["hp"] = hp
        if exp > 0 and (it.get("exp") or 0) <= 0:
            it["exp"] = exp
        if boss and not it.get("boss"):
            it["boss"] = 1
        it["tier"] = _mob_tier(it["raw"], it["name"], it["boss"], it["hp"])


def _apply_boss_ref(items: list[dict]) -> None:
    """数据源读不到数值（hp<=0）的怪，按国际服参照表补 boss 档位/血量/等级。

    mobstats 表（服务端数值）已先补过一轮，这里只兜底表外的怪；
    已有数值的怪（数据源自带或 mobstats 已补）一律不动。
    """
    ref = _boss_ref()
    if not ref:
        return
    for it in items:
        if (it.get("hp") or 0) > 0:
            continue                      # 已有数值，不动
        hit = ref.get(it["id"])
        if not hit:
            continue
        it["boss"] = 1
        it["hp"] = hit[0]
        if (it.get("level") or 0) <= 0:
            it["level"] = hit[1]
        it["tier"] = _mob_tier(it["raw"], it["name"], it["boss"], it["hp"])


def mob_meta(mobnx: Path) -> list[dict]:
    """跑 nxmob info 拿每只怪的 boss/等级/血量 + link 目标 + 自身帧数（按 mtime 缓存）。"""
    cache = CACHE / f"mobinfo2_{int(mobnx.stat().st_mtime)}.json"
    items: list[dict] | None = None
    if cache.exists():
        try:
            items = json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            items = None
    if items is None:
        out = _run(["nxmob", "info"], _nx_dir(), timeout=300)
        items = []
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
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    # 参照表回退放在缓存之后：缓存里存的是原始值，补丁每次都生效
    _apply_mob_stats(items)   # 服务端数值表（level/hp/exp/boss，最全）
    _apply_boss_ref(items)    # 表外的 boss 再按国际服参照表兜底
    items.sort(key=lambda x: int(x["id"]))
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
        out = _run(["nxmap", "life"], mapnx.parent, timeout=900)
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
    """全部怪物列表（图鉴/素材浏览用）：id/name/tier/level/hp/link/frames/img_ok。"""
    _need_ws()
    mobnx = _nx_dir() / "Mob.nx"
    if not mobnx.exists():
        hint = "国服数据源还没转换 Mob.nx（转换完会出现在 .cache\\cn）" \
            if _cn_mode() is not None else "先转换 Mob.wz"
        raise CliError(f"没有 Mob.nx —— {hint}", 1)
    key = f"mobs:{mobnx.stat().st_mtime}"
    if _lists.get("key") != key:
        items = mob_meta(mobnx)
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
    """这只怪自然刷新在哪些地图（life 反向聚合）。"""
    mapnx = _nx_dir() / "Map.nx"
    if not mapnx.exists():
        raise CliError("没有 Map.nx —— 先转换地图数据", 1)
    idx = life_index(mapnx.parent, mapnx)
    return {"maps": idx.get(kid.lstrip("0"), [])}


def c_mobmaps() -> dict:
    """有怪的地图清单（life 索引再反转）：[{id,name,mobs:[怪id]}]，战斗页限定地图抽怪。"""
    mapnx = _nx_dir() / "Map.nx"
    if not mapnx.exists():
        raise CliError("没有 Map.nx —— 先转换地图数据", 1)
    k2 = f"mobmaps:{int(mapnx.stat().st_mtime)}"
    if _lists.get("mobmaps_key") != k2:
        idx = life_index(mapnx.parent, mapnx)
        maps: dict[str, dict] = {}
        for kid, rows in idx.items():
            for m in rows:
                e = maps.setdefault(m["mapid"], {"id": m["mapid"], "name": m["name"],
                                                 "mobs": set(), "n": 0})
                e["mobs"].add(kid)
                e["n"] += m["n"]
        out = sorted(maps.values(), key=lambda m: int(m["id"]) if m["id"].isdigit() else 0)
        for e in out:
            e["mobs"] = sorted(e["mobs"], key=lambda s: int(s) if s.isdigit() else 0)
        _lists.update(mobmaps_key=k2, mobmaps=out)
    return {"maps": _lists.get("mobmaps", [])}


def c_maps() -> dict:
    mapnx = _nx_dir() / "Map.nx"
    if not mapnx.exists():
        if _cn_mode() is not None:
            raise CliError("国服数据还没转换：先在工作区卡片点「转换」生成 .cache\\cn\\Map.nx", 1)
        raise CliError("工作区里没有 Map.nx —— 先转换 Map.wz", 1)
    key = f"maps:{mapnx.stat().st_mtime}"
    if _lists.get("key") != key:
        # 全量国服 Map.nx：Map0 的图平铺在 Map/ 下（Map//<id>.img），
        # Map1..Map9 的图在 Map/<shard>/<id>.img；一次 depth=1 扫描全部拿到。
        out = _run(["nxscan", "Map", "", "1"], mapnx.parent, timeout=600)
        ids: list[str] = []
        for ln in out.splitlines():
            m = re.match(r"^Map/+(?:Map\d+/)?(\d{9})\.img \[", ln)
            if m:
                ids.append(m.group(1))
        ids = sorted(set(ids))
        # 「有怪的图」：life 反向聚合里出现过的 mapid（只算 m=mob，npc 不算）
        life = life_index(mapnx.parent, mapnx)
        life_maps = sorted({m["mapid"] for rows in life.values() for m in rows})
        # 每张图的推荐等级区间（列表行徽标 + 排序参考）
        _lists.update(key=key, maps=ids, life_maps=life_maps, mob_lv=_map_lv_table(life))
    return {"maps": _lists["maps"], "life_maps": _lists.get("life_maps", []),
            "mob_lv": _lists.get("mob_lv", {})}


# 图片找图 --------------------------------------------------------------------
# 粘贴一张游戏截图（或直接截小地图），和全库地图的 miniMap/canvas 做模板匹配，
# 找出它对应哪张地图。索引由 `nxmap mini-all` 一次建好（.cache/minimap/）。

MMIDX = CACHE / "minimap"
MMAUTO = MMIDX / "auto.meta.json"      # 兜底小地图的完成戳（绑 Map.nx mtime）
_mmauto_lock = threading.Lock()
_mmauto: dict = {"running": False, "done": 0, "total": 0}
_mmjob: dict = {"running": False, "error": "", "done": 0, "total": 0}
_mm_lock = threading.Lock()


def _minimap_ready() -> bool:
    mapnx = _nx_dir() / "Map.nx"
    if not mapnx.exists():
        raise CliError("没有 Map.nx —— 先转换地图数据", 1)
    meta = MMIDX / "index.meta.json"
    if not (MMIDX / "index.json").exists() or not meta.exists():
        return False
    try:
        return json.loads(meta.read_text(encoding="utf-8")).get("mtime") \
            == mapnx.stat().st_mtime
    except (OSError, ValueError):
        return False


def _minimap_build() -> None:
    """后台线程：mini-all 全库导小地图（直接写 .cache/minimap，旧目录整体挪进 trash）。

    这一步是「一次性解压」：把 Map.nx 里 14000+ 张 canvas 全部落成 PNG 目录，
    之后所有找图都只读这个目录 + 签名索引，不再碰 23GB 的 Map.nx。
    用非阻塞锁防重入（服务启动预热、转换完自动预热、首次搜索兜底可能同时来）。
    """
    if not _mm_lock.acquire(blocking=False):
        return
    nxdir = _nx_dir()
    # 进度条分母：优先用旧索引的张数（14449）；全新安装时退而用全库传送门表的
    # 行数估（略大于真实张数，进度停在 ~95% 再跳满，总比无限转圈强）。
    total = 0
    try:
        total = len(json.loads((MMIDX / "index.json").read_text(encoding="utf-8")))
    except Exception:                          # noqa: BLE001
        try:
            with open(CACHE / "portals.csv", encoding="utf-8") as f:
                total = sum(1 for _ in f) - 1
        except Exception:                       # noqa: BLE001
            total = 0
    prog = MMIDX / "_progress"
    _mmjob.update(running=True, error="", done=0, total=total, stage="清理旧索引")
    try:
        # 旧目录整体改名扔进 .cache/trash（目录 rename 是元数据操作，1.4 万文件也是秒级），
        # 绝不逐文件删——实测 19 个/秒还会被批量删除保护拦下。
        if MMIDX.exists():
            drop_dir(MMIDX)
            _mmjob["stage"] = "清理旧索引"
        MMIDX.mkdir(parents=True, exist_ok=True)
        _mmjob["stage"] = "解压小地图"
        # 进度不走进程输出：Windows 下管道是全缓冲的，进度会憋到进程退出才吐出来
        # （std::cerr / fprintf+fflush / os.read 全试过一样）。nxmap 自己写计数文件。
        proc = subprocess.Popen(
            [str(BIN / "nxmap.exe"), "mini-all", str(MMIDX)], cwd=str(nxdir),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while proc.poll() is None:
            try:
                _mmjob["done"] = int(prog.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                pass
            time.sleep(0.3)
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"nxmap mini-all 失败（exit {rc}）")
        if _mmjob["total"] > 0:
            _mmjob["done"] = _mmjob["total"]        # 收尾：进度条先补满再进签名阶段
        (MMIDX / "index.meta.json").write_text(
            json.dumps({"mtime": (nxdir / "Map.nx").stat().st_mtime}), encoding="utf-8")
        if total <= 0:
            _mmjob["total"] = _mmjob["done"]
        _mmjob["error"] = ""
        # 顺手把识别索引也建好（每张小地图压成 64×64 签名），
        # 这样第一次「找图」不用再等一次全库解码
        _thumbs_build(progress=True)
        _mmjob["stage"] = ""
    except Exception as exc:                   # noqa: BLE001
        _mmjob["error"] = str(exc)
    finally:
        try:
            prog.unlink()
        except OSError:
            pass
        _mmjob["running"] = False
        _mm_lock.release()


def minimap_warmup(delay: float = 0.0) -> None:
    """后台预热小地图库：数据源在、索引不在/过期就悄悄建好（不阻塞请求）。

    以前是等用户第一次点「找图」才在请求线程里开始解压，他要干等一分钟；
    现在服务启动后、数据转换完成后都主动预热一次，用户点的时候一般已经好了。
    """
    def run() -> None:
        if delay > 0:
            time.sleep(delay)
        try:
            if not _minimap_ready():
                _minimap_build()               # 全库索引不在/过期：先建
            if _minimap_fallback_stale():
                _minimap_fallback_build()      # 退化 canvas 的兜底图：补建
        except CliError:                       # 没有 Map.nx：还没转换，不用建
            return
        except Exception:                      # noqa: BLE001
            return

    threading.Thread(target=run, daemon=True).start()


# 退化 canvas 的兜底小地图 -----------------------------------------------------
# WZ 里约 0.4%（63/14449）的图 miniMap/canvas 是 1×1 占位（老彩虹岛屋内景等），
# 客户端进这些图同样只有名字条。给找图索引和 HUD 补一张自动生成的：
#   canvas 尺寸 = miniMap w/h ÷ 2^mag，内容 = 无背景渲染图缩到 1/2^mag 后
#   贴在 (minx+centerX, miny+centerY) / 2^mag 处 —— canvas 覆盖的世界矩形是
#   [-centerX, -centerX+w] × [-centerY, -centerY+h]（UIMiniMap.cpp 的
#   (世界+center)/2^mag 反推），标记只画 pt=2 传送口，黑底。


def _minimap_fallback_stale() -> bool:
    """兜底小地图：没建过 / Map.nx 换过 / 全库索引还没建，都不用做或重做。"""
    try:
        mapnx = _nx_dir() / "Map.nx"
        if not mapnx.exists() or not (MMIDX / "index.json").exists():
            return False
        if not MMAUTO.exists():
            return True
        return json.loads(MMAUTO.read_text(encoding="utf-8")).get("mtime") \
            != mapnx.stat().st_mtime
    except Exception:                          # noqa: BLE001
        return False


def _minimap_fallback_one(mid: str, tmp: Path) -> bool:
    """一张图的兜底小地图 → MMIDX/<id>.png。成功返回 True。"""
    from PIL import Image, ImageDraw
    outdir = tmp / mid
    _run(["nxmap", mid, str(outdir)], _nx_dir(), timeout=300)
    mjp = outdir / "map.json"
    if not mjp.exists():
        return False
    mj = json.loads(mjp.read_text(encoding="utf-8"))
    mm = mj.get("miniMap") or {}
    if not mm.get("w") or not mm.get("h"):
        return False
    R = _render_mod()
    minx, miny, bw, bh = R.map_bounds(mj)
    if bw <= 0 or bh <= 0:
        return False
    pv = R.render_file(str(outdir), str(outdir / "nobg.png"),
                       no_bg=True, max_width=None, pad=40)
    scale = 2.0 ** float(mm.get("mag", 0) or 0)
    cx, cy = float(mm.get("centerX", 0) or 0), float(mm.get("centerY", 0) or 0)
    cw, ch = max(1, round(mm["w"] / scale)), max(1, round(mm["h"] / scale))
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 255))       # 黑底
    pad = (pv.width - round(bw)) / 2.0          # 渲染器四周的 pad（默认 40）
    if pad < 0 or pv.width <= 2 * pad or pv.height <= 2 * pad:
        return False
    content = pv.crop((round(pad), round(pad),
                       round(pad) + round(bw), round(pad) + round(bh)))
    small = content.resize((max(1, round(bw / scale)), max(1, round(bh / scale))),
                           Image.LANCZOS)
    canvas.alpha_composite(small, (round((minx + cx) / scale),
                                   round((miny + cy) / scale)))
    dr = ImageDraw.Draw(canvas)
    for p in mj.get("portal", []):
        if p.get("pt") != 2:
            continue
        # 传送门坐标是脚底点，直接画会正压在地面线上、和地形边缘糊在一起：
        # 统一上移 3px（用户 2026-11-08 定的观感）。
        x, y = (p["x"] + cx) / scale, (p["y"] + cy) / scale - 3
        dr.ellipse([x - 2, y - 2, x + 2, y + 2], outline=(64, 160, 255, 255), width=1)
    canvas.save(MMIDX / f"{mid}.png")
    return True


def _minimap_fallback_build() -> None:
    """全库扫 index.json 里 cw≤1 的退化 canvas，逐张生成兜底小地图。

    慢活（每张 = nxmap 全量导出 + 无背景渲染，几十秒到几分钟的总耗时）：
    后台线程跑，单张失败就跳过（payload 里兜底图不存在时回落到 1×1 原图）。
    全部做完写 auto.meta.json 戳 + 重建识别签名（把 1×1 的全 0 签名换掉）。
    """
    if not _mmauto_lock.acquire(blocking=False):
        return
    tmp = CACHE / "minimap-auto.building"
    try:
        if tmp.exists():
            drop_dir(tmp)
        tmp.mkdir(parents=True)
        index = json.loads((MMIDX / "index.json").read_text(encoding="utf-8"))
        deg = [mid for mid, e in index.items() if (e.get("cw") or 0) <= 1]
        _mmauto.update(running=True, done=0, total=len(deg))
        done = 0
        for i, mid in enumerate(deg):
            try:
                if _minimap_fallback_one(mid, tmp):
                    index[mid]["auto"] = 1
                    done += 1
            except Exception:                  # noqa: BLE001
                pass                           # 单张失败不影响其它
            _mmauto["done"] = i + 1
        (MMIDX / "index.json").write_text(json.dumps(index), encoding="utf-8")
        MMAUTO.write_text(
            json.dumps({"mtime": (_nx_dir() / "Map.nx").stat().st_mtime,
                        "auto": done, "total": len(deg)}), encoding="utf-8")
        _thumbs_build()                        # 全 0 签名换成真图签名
    except Exception as exc:                   # noqa: BLE001
        _mmjob["error"] = str(exc)
    finally:
        _mmauto.update(running=False)
        _mmauto_lock.release()


# 小地图识别索引 --------------------------------------------------------------
# index.json 是「目录」：map id → 小地图 PNG 路径；真搜索时逐张 imread 14451 张
# 要 10 秒以上（冷缓存更久）。所以在目录之上再叠一层「识别索引」：
# 每张 PNG 压成 64×64 灰度、按自身均值/标准差归一化，存成 int8 矩阵
# （14451×64×64 ≈ 59MB）。搜索时先做一次矩阵乘打分粗筛，只有候选才读原图
# 做精确模板匹配 —— 索引常驻内存，每次搜索 1 秒内。
THUMB_PX = 64
THUMBS_NPZ = MMIDX / "thumbs.npz"
_thumbs: dict = {"state": None, "building": False}


def _thumbs_stale() -> bool:
    """签名索引不存在 / 与当前小地图索引不同源（Map.nx 换过）。"""
    mapnx = _nx_dir() / "Map.nx"
    if not mapnx.exists() or not THUMBS_NPZ.exists():
        return True
    try:
        with np.load(THUMBS_NPZ, allow_pickle=False) as z:
            return float(z["mtime"]) != mapnx.stat().st_mtime
    except Exception:
        return True


def _thumb_one(mid: str):
    """一张小地图 → 64×64 归一化 int8 签名（纯色图留全 0：能进候选但分数为 0）。"""
    g = cv2.imread(str(MMIDX / f"{mid}.png"), cv2.IMREAD_GRAYSCALE)
    if g is None:
        return None
    t = cv2.resize(g, (THUMB_PX, THUMB_PX),
                   interpolation=cv2.INTER_AREA).astype(np.float32)
    t -= t.mean()
    std = float(t.std())
    if std >= 1e-3:
        t = np.clip(t / std * 127.0, -127, 127)
    else:
        t = np.zeros_like(t)
    return t.astype(np.int8)


def _thumbs_build(progress: bool = False) -> None:
    """把每张小地图压成 64×64 归一化签名，存 thumbs.npz（约 59MB）。

    cv2.imread 解码时会放 GIL，8 线程并行比串行快 4 倍左右
    （14449 张：12.4s → 3s 上下）。
    """
    if cv2 is None or np is None:
        return
    if _thumbs["building"]:
        return
    _thumbs["building"] = True
    try:
        index = json.loads((MMIDX / "index.json").read_text(encoding="utf-8"))
        mapnx = _nx_dir() / "Map.nx"
        mids = sorted(index.keys())
        if progress:                       # 14k 张解码要一分钟，不能让进度条空转
            _mmjob["stage"] = "生成找图签名"
            _mmjob["done"] = 0
            _mmjob["total"] = len(mids)
        with ThreadPoolExecutor(max_workers=8) as ex:
            rows = []
            for i, t in enumerate(ex.map(_thumb_one, mids)):
                rows.append(t)
                if progress and (i % 200) == 0:
                    _mmjob["done"] = i + 1
        arr = np.zeros((len(mids), THUMB_PX, THUMB_PX), dtype=np.int8)
        kept: list[str] = []
        for mid, t in zip(mids, rows):
            if t is None:
                continue
            arr[len(kept)] = t
            kept.append(mid)
        tmp = MMIDX / "thumbs.npz.tmp"
        with open(tmp, "wb") as f:
            np.savez_compressed(f, mids=np.array(kept), thumbs=arr[:len(kept)],
                                mtime=np.float64(mapnx.stat().st_mtime))
        os.replace(tmp, THUMBS_NPZ)
        if progress:
            _mmjob["done"] = _mmjob["total"]
        _thumbs["state"] = None        # 让下一次 _thumbs_load 重新读盘
    except Exception:                  # noqa: BLE001
        pass                           # 建不出来就退回「全库逐张扫」的老路径
    finally:
        _thumbs["building"] = False


def _thumbs_load():
    """返回 (mids, float32 签名矩阵)；未就绪返回 None（后台建，下次再用）。"""
    if cv2 is None or np is None:
        return None
    try:
        map_mtime = (_nx_dir() / "Map.nx").stat().st_mtime
    except OSError:
        return None
    st = _thumbs["state"]
    if st is not None and st["mtime"] == map_mtime:
        return st["mids"], st["arr"]
    if _thumbs_stale():
        if not _thumbs["building"]:
            threading.Thread(target=_thumbs_build, daemon=True).start()
        return None
    try:
        with np.load(THUMBS_NPZ, allow_pickle=False) as z:
            st = {"mtime": float(z["mtime"]), "mids": z["mids"],
                  "arr": z["thumbs"].astype(np.float32)}
    except Exception:                  # noqa: BLE001
        return None
    _thumbs["state"] = st
    return st["mids"], st["arr"]


def c_map_imgindex() -> dict:
    return {"building": _mmjob["running"], "ready": _minimap_ready(),
            "thumbs": bool(_thumbs["state"] is not None) or not _thumbs_stale(),
            "error": _mmjob["error"],
            "auto": {"running": _mmauto["running"], "done": _mmauto["done"],
                     "total": _mmauto["total"]}}


def _mm_score_one(qg, cg) -> tuple[float, float] | None:
    """查询图 vs 一张 canvas：先按面积比把 canvas 缩到和查询同尺度，再双向匹配。

    为什么不再用「尺寸比例打折」：截图可能是被 DPI/窗口缩放过的，比 canvas 大；
    旧算法对这种图乘 0.667 惩罚，反而让长得像的别的图排前面——实测放大 1.5×
    的截图会直接搜不到正确地图。现在用 f = sqrt(查询面积/canvas 面积) 把 canvas
    归一到查询尺度，两边尺寸自然吻合，无需惩罚；长宽比不一致的候选会落到「一边
    大一边小」的空中区间，直接判 None 淘汰。
    """
    qh, qw = qg.shape
    ch, cw = cg.shape
    if qh < 2 or qw < 2 or ch < 2 or cw < 2:
        return None
    f = math.sqrt((qw * qh) / float(cw * ch))
    nw = max(1, int(round(cw * f)))
    nh = max(1, int(round(ch * f)))
    if nw < 2 or nh < 2:
        return None
    c2 = cv2.resize(cg, (nw, nh),
                    interpolation=cv2.INTER_AREA if f < 1 else cv2.INTER_LINEAR)
    if nh <= qh and nw <= qw:              # canvas 落在截图里（整窗截图 / 缩小的画布）
        raw = float(cv2.matchTemplate(qg, c2, cv2.TM_CCOEFF_NORMED).max())
    elif nh >= qh and nw >= qw:            # 截的是画布的一部分（小地图被窗口裁掉）
        raw = float(cv2.matchTemplate(c2, qg, cv2.TM_CCOEFF_NORMED).max())
    else:
        return None                        # 长宽比差太多，不是同一张图
    return raw, raw


def c_map_imgsearch(image: str) -> dict:
    """image = base64（可带 data:image/...;base64, 前缀）的截图，返回 top 匹配。"""
    if cv2 is None or np is None:
        raise CliError("缺依赖：先跑 python scripts\\deps.py（依赖装在 .pylibs\\ 里）", 1)
    if not _minimap_ready():
        # 小地图库还没解出来（正常情况启动时就预热过了）：后台补建，这次让前端轮询
        minimap_warmup()
        return {"building": True}
    raw_b64 = image.split(",", 1)[-1] if image.startswith("data:") else image
    buf = np.frombuffer(base64.b64decode(raw_b64), dtype=np.uint8)
    q = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if q is None:
        raise CliError("图片解析失败 —— 确认粘贴的是截图", 2)
    qg = cv2.cvtColor(q, cv2.COLOR_BGR2GRAY)
    index = json.loads((MMIDX / "index.json").read_text(encoding="utf-8"))

    # 第一级：识别索引粗筛。每条候选自带 64×64 归一化签名，
    # 一次矩阵乘就打完 14451 张的分（毫秒级），只留前 500 进精匹配。
    th = _thumbs_load()
    if th is None:
        return {"building": True}      # 索引还在建，前端轮询
    mids, arr = th
    q64 = cv2.resize(qg, (THUMB_PX, THUMB_PX),
                     interpolation=cv2.INTER_AREA).astype(np.float32)
    q64 -= q64.mean()
    qn = float(q64.std())
    if qn < 1e-3:
        raise CliError("截图几乎是纯色（没有可识别的地形），换一张小地图截图", 2)
    q64 /= qn
    coarse = arr.reshape(len(mids), -1) @ q64.reshape(-1)
    top = np.argsort(coarse)[::-1][:500]
    cand_mids = [str(mids[i]) for i in top]

    # 第二级：只对候选解密原图做同尺度双向模板匹配（多线程并行解码+匹配）
    def scan(mid: str):
        cg = cv2.imread(str(MMIDX / f"{mid}.png"), cv2.IMREAD_GRAYSCALE)
        if cg is None:
            return None
        r = _mm_score_one(qg, cg)
        if not r:
            return None
        f = math.sqrt((qg.shape[1] * qg.shape[0]) / float(cg.shape[1] * cg.shape[0]))
        return [r[0], r[1], mid, round(f, 2)]

    with ThreadPoolExecutor(max_workers=8) as ex:
        cand = [c for c in ex.map(scan, cand_mids) if c]
    cand.sort(reverse=True)

    out = []
    for score, raw, mid, s in cand[:9]:
        meta = index[mid]
        out.append({"id": mid, "score": round(score, 3), "scale": s,
                    "name": meta.get("name") or "", "street": meta.get("street") or "",
                    "w": meta.get("cw"), "h": meta.get("ch"),
                    "thumb": f"/preview/minimap/{mid}.png"})
    return {"building": False, "results": out}


# 怀旧服（mxdclassic）小地图库 -------------------------------------------------
# 用户玩的怀旧服是 Unity 重制客户端，数据在 mxdclassic\...\StreamingAssets\aa\w\*.bundle。
# 游戏内左上角小地图 = 包里 spritesheet atlas 上的 miniMap/canvas 素材缩放显示
# （2026-11-09 用「勇士部落东入口」实测：canvas 101040000 与游戏画面 0.767 强匹配）。
# 库 = 把 canvas 按 alpha 裁剪提取成透明 PNG（.cache\minimap-cls\），找图走多尺度匹配
# （游戏里显示倍率不固定，和现代版的单尺度匹配不同）。
# 地图名是本地就有的：json_a2909* 包的 TextAsset「Map」（204KB JSON，1838 张，
# streetName/mapName/mapDesc/ObjectName）——早先「名字由服务器下发」的判断是错的。

CLS_MMIDX = CACHE / "minimap-cls"
CLS_THUMBS_NPZ = CLS_MMIDX / "thumbs.npz"
CLS_NAMES = CLS_MMIDX / "names.json"          # 地图名表缓存（带 json 包 mtime 戳）
_clsjob: dict = {"running": False, "error": "", "done": 0, "total": 0, "stage": ""}
_cls_lock = threading.Lock()
_cls_thumbs: dict = {"state": None, "building": False}
_cls_names_state: dict = {"state": None}


def _cls_w_dir() -> Path | None:
    """怀旧服客户端的 Addressables 目录（…\\aa），工作区选 mxdclassic / 安装根都行。"""
    if _ws is None:
        return None

    def ok(p: Path) -> bool:
        w = p / "w"
        if not w.is_dir():
            return False
        return next(w.glob("spritesheet_*.bundle"), None) is not None

    rel = Path("Maplestory_Classic_Data") / "StreamingAssets" / "aa"
    cands = [_ws / rel, _ws / "mxdclassic" / rel]
    up = _ws
    for _ in range(3):                      # 覆盖选安装根 / mxd 的情况
        up = up.parent
        if up == up.parent:
            break
        cands.append(up / "mxdclassic" / rel)
    for c in cands:
        if ok(c):
            return c
    return None


def _cls_ready() -> bool:
    """库建好了且来源 bundle 没变（客户端更新后自动过期）。"""
    try:
        meta = json.loads((CLS_MMIDX / "index.meta.json").read_text(encoding="utf-8"))
        b = Path(meta["bundle"])
        return (CLS_MMIDX / "index.json").is_file() and CLS_THUMBS_NPZ.is_file() \
            and b.is_file() and b.stat().st_mtime == meta.get("mtime") \
            and b.stat().st_size == meta.get("size")
    except (OSError, ValueError, KeyError):
        return False


def _cls_state() -> dict:
    w = _cls_w_dir()
    n = 0
    if w is not None and _cls_ready():
        try:
            n = len(json.loads((CLS_MMIDX / "index.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            n = 0
    return {"dir": str(w) if w else None, "ready": _cls_ready(), "count": n,
            "building": _clsjob["running"]}


def _cls_names() -> dict:
    """怀旧服地图名表：去掉前导零的 mapid → {street, name, desc}。

    名字在本地 json_a2909* 包的 TextAsset「Map」里（204KB JSON，1838 张）。
    包名哈希会随客户端更新变，所以扫 json_*.bundle 找 TextAsset 名叫 Map 的那个；
    结果按所有 json 包的最新 mtime 打戳缓存到 .cache\\minimap-cls\\names.json。
    """
    w = _cls_w_dir()
    if w is None:
        return {}
    try:
        stamp = max(p.stat().st_mtime for p in (w / "w").glob("json_*.bundle"))
    except ValueError:
        return {}
    st = _cls_names_state["state"]
    if st is not None and st["stamp"] == stamp:
        return st["maps"]
    try:
        raw = json.loads(CLS_NAMES.read_text(encoding="utf-8"))
        if raw.get("stamp") == stamp:
            maps = raw.get("maps") or {}
            _cls_names_state["state"] = {"stamp": stamp, "maps": maps}
            return maps
    except (OSError, ValueError):
        pass
    try:
        import UnityPy
    except ImportError:
        return {}
    for b in sorted((w / "w").glob("json_*.bundle")):
        try:
            env = UnityPy.load(str(b))
        except Exception:                    # noqa: BLE001
            continue
        for o in env.objects:
            if o.type.name != "TextAsset":
                continue
            try:
                d = o.read()
            except Exception:                # noqa: BLE001
                continue
            if getattr(d, "m_Name", "") != "Map":
                continue
            s = d.m_Script
            if isinstance(s, str):
                s = s.encode("utf-8", "surrogateescape")
            try:
                j = json.loads(s.decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(j, dict) or not any(
                    isinstance(v, dict) and "mapName" in v for v in list(j.values())[:20]):
                continue
            maps: dict[str, dict] = {}
            for k, v in j.items():
                if not isinstance(v, dict):
                    continue
                maps[k.lstrip("0") or "0"] = {
                    "street": v.get("streetName") or "",
                    "name": v.get("mapName") or "",
                    "desc": v.get("mapDesc") or "",
                }
            try:
                CLS_MMIDX.mkdir(parents=True, exist_ok=True)
                tmp = CLS_NAMES.with_suffix(".tmp")
                tmp.write_text(json.dumps({"stamp": stamp, "maps": maps}, ensure_ascii=False),
                               encoding="utf-8")
                os.replace(tmp, CLS_NAMES)
            except OSError:
                pass
            _cls_names_state["state"] = {"stamp": stamp, "maps": maps}
            return maps
    return {}


CLS_TABLES = CLS_MMIDX / "tables.json"        # 怀旧服 Mob/Skill 名单缓存（带 json 包 mtime 戳）
_cls_tables_state: dict = {"state": None}


def _cls_tables() -> dict:
    """怀旧服实装名单：TextAsset「Mob」1395 只 /「Skill」737 个的 key 集合。

    key 是去前导零的 id（和 Map 名字表同款约定）。给 WebUI 的「怀旧服」筛选用：
    现代素材库是超集，按这份名单过滤出怀旧服真正有的怪/技能。同样按所有
    json 包的最新 mtime 打戳缓存到 .cache\\minimap-cls\\tables.json。
    """
    empty = {"mob": [], "skill": []}
    w = _cls_w_dir()
    if w is None:
        return empty
    try:
        stamp = max(p.stat().st_mtime for p in (w / "w").glob("json_*.bundle"))
    except ValueError:
        return empty
    st = _cls_tables_state["state"]
    if st is not None and st["stamp"] == stamp:
        return st["tables"]
    try:
        raw = json.loads(CLS_TABLES.read_text(encoding="utf-8"))
        if raw.get("stamp") == stamp:
            tables = {"mob": raw.get("mob") or [], "skill": raw.get("skill") or []}
            _cls_tables_state["state"] = {"stamp": stamp, "tables": tables}
            return tables
    except (OSError, ValueError):
        pass
    try:
        import UnityPy
    except ImportError:
        return empty
    found: dict[str, list[str]] = {}
    for b in sorted((w / "w").glob("json_*.bundle")):
        try:
            env = UnityPy.load(str(b))
        except Exception:                      # noqa: BLE001
            continue
        for o in env.objects:
            if o.type.name != "TextAsset":
                continue
            try:
                d = o.read()
            except Exception:                  # noqa: BLE001
                continue
            nm = getattr(d, "m_Name", "")
            if nm not in ("Mob", "Skill"):
                continue
            s = d.m_Script
            if isinstance(s, str):
                s = s.encode("utf-8", "surrogateescape")
            try:
                j = json.loads(s.decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                continue
            ids = sorted(k for k in j
                         if isinstance(k, str) and k.isdigit() and len(k) >= 3)
            if len(ids) < 50:                  # 太小的不是名单表（防同名小 TextAsset 误伤）
                continue
            found[nm.lower()] = ids
        if len(found) >= 2:
            break
    tables = {"mob": found.get("mob", []), "skill": found.get("skill", [])}
    try:
        CLS_MMIDX.mkdir(parents=True, exist_ok=True)
        tmp = CLS_TABLES.with_suffix(".tmp")
        tmp.write_text(json.dumps({"stamp": stamp, **tables}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, CLS_TABLES)
    except OSError:
        pass
    _cls_tables_state["state"] = {"stamp": stamp, "tables": tables}
    return tables


CLS_ITEMNAMES = CLS_MMIDX / "itemnames.json"   # 怀旧服道具名表缓存（带 json 包 mtime 戳）
_cls_items_state: dict = {"state": None}


def _cls_itemnames() -> dict:
    """怀旧服道具名表（中文）：去前导零 itemid → name。

    源 = json_a2909* 包的 TextAsset「Item」（17298 条）。图鉴/掉落显示用的
    vendor/itemnames.json 来自服务端 String.wz 是英文的；用户实际玩的怀旧服
    名字是中文——道具名优先取这里，取不到再退英文。
    """
    w = _cls_w_dir()
    if w is None:
        return {}
    try:
        stamp = max(p.stat().st_mtime for p in (w / "w").glob("json_*.bundle"))
    except ValueError:
        return {}
    st = _cls_items_state["state"]
    if st is not None and st["stamp"] == stamp:
        return st["names"]
    try:
        raw = json.loads(CLS_ITEMNAMES.read_text(encoding="utf-8"))
        if raw.get("stamp") == stamp:
            names = raw.get("names") or {}
            _cls_items_state["state"] = {"stamp": stamp, "names": names}
            return names
    except (OSError, ValueError):
        pass
    names: dict[str, str] = {}
    try:
        import UnityPy
    except ImportError:
        return names
    for b in sorted((w / "w").glob("json_*.bundle")):
        try:
            env = UnityPy.load(str(b))
        except Exception:                    # noqa: BLE001
            continue
        for o in env.objects:
            if o.type.name != "TextAsset":
                continue
            try:
                d = o.read()
            except Exception:                # noqa: BLE001
                continue
            if getattr(d, "m_Name", "") != "Item":
                continue
            s = d.m_Script
            if isinstance(s, str):
                s = s.encode("utf-8", "surrogateescape")
            try:
                j = json.loads(s.decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                continue
            cand = {k: (v.get("name") or "")
                    for k, v in j.items()
                    if isinstance(k, str) and k.isdigit()
                    and isinstance(v, dict)}
            if len(cand) > len(names):       # 取最大的那张（防同名小表误伤）
                names = cand
        if len(names) > 10000:
            break
    try:
        CLS_MMIDX.mkdir(parents=True, exist_ok=True)
        tmp = CLS_ITEMNAMES.with_suffix(".tmp")
        tmp.write_text(json.dumps({"stamp": stamp, "names": names}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, CLS_ITEMNAMES)
    except OSError:
        pass
    _cls_items_state["state"] = {"stamp": stamp, "names": names}
    return names


# ── 怀旧服 WZJS 大图库（1750 张地图 → 可渲染 map_dir）────────────────────
# json_573635e1*.bundle 的 WZJS 地图对象 + Obj/Tile/Back 分类页，配上
# spritesheet 注册表，由 vendor/clsmap.py 转成 .cache\wzjs-map\<id>\
# （map.json + img\），与 nxmap 产物同构 —— 预览/窗口导出/小地图 HUD/
# 怪物叠加全链路复用。数据正确性已对照经典服务端 XML 验证：非新手岛
# 样本 foothold/portal 100% 一致（000000000/10000/20000 差异是重制版
# 重设计教程岛，多出 tutorial*/adviceMap portal）。
WZJS_MAP = CACHE / "wzjs-map"
WZJS_VER = 2                             # 图库格式版本（2 = atlas_idx 裸 int + inlink 表）
# map.json 语义版本（与贴图无关，只升级它时全库重转 map.json、贴图保持不动）：
#   2 = tile 的 z 取瓦片图节点（客户端 Tile.cpp: z = dsrc["z"] → zM），
#       同层 tile/obj 按 z 归并绘制（原来 tile z 恒 0 且后画，地面盖住物件）
WZJS_MVER = 2
WZJS_REG = WZJS_MAP / "registry.json.gz"     # spritesheet 注册表（带来源戳）
# 注册表语义版本（独立于贴图版本：升级它只重建注册表，不重转地图）：
#   2 = inlink 前缀从 ("Map/",) 扩到 ("Map/", "Item/", "Mob/")——
#       掉落物图标（Item/Consume/0200 里 02000000 红药水等）走的是 inlink 表
WZJS_REGVER = 2
WZJS_BUILT = WZJS_MAP / "built.json"         # {stamp, ver, ids, errs}（断点续跑）
_wzjsjob: dict = {"running": False, "error": "", "done": 0, "total": 0, "stage": ""}
_wzjs_lock = threading.Lock()


def _wzjs_stamp(w: Path) -> str:
    """来源戳：json 地图包 + 全部 spritesheet 包的 名字/mtime/size。"""
    parts = []
    for pat in ("json_573635e1*", "spritesheet_*"):
        for p in sorted((w / "w").glob(pat)):
            st = p.stat()
            parts.append(f"{p.name}:{st.st_mtime:.0f}:{st.st_size}")
    return "|".join(parts)


def _wzjs_ready() -> bool:
    """大图库建好且来源包没变（客户端更新自动过期，下次导入重建）。"""
    w = _cls_w_dir()
    if w is None:
        return False
    try:
        b = json.loads(WZJS_BUILT.read_text(encoding="utf-8"))
        return (bool(b.get("ids")) and b.get("ver") == WZJS_VER
                and b.get("mver") == WZJS_MVER
                and b.get("stamp") == _wzjs_stamp(w))
    except (OSError, ValueError):
        return False


def _wzjs_mob_ids() -> list:
    """有自然刷怪（life 里 type=m）的地图 id —— 列表页「有怪/无怪」筛选用。

    正路是建库时顺手记进 built.json 的 mobs；老缓存没这个字段就扫一遍
    map.json（只看 life 有没有 type=m，文本查比重解析快一个数量级），
    结果回写 built.json，之后零成本。建库进行中不写，免得被后台任务覆盖。
    """
    try:
        b = json.loads(WZJS_BUILT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(b.get("mobs"), list):
        return b["mobs"]
    ids = []
    for mid in b.get("ids") or []:
        try:
            txt = (WZJS_MAP / mid / "map.json").read_text(encoding="utf-8")
        except OSError:
            continue
        if '"type": "m"' in txt:
            ids.append(mid)
    if not _wzjsjob["running"]:
        b["mobs"] = ids
        try:
            tmp = WZJS_BUILT.with_suffix(".tmp")
            tmp.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, WZJS_BUILT)
        except OSError:
            pass
    return ids


def _wzjs_map_lv() -> dict:
    """全库地图 → 推荐等级区间 [min,max]（图内自然刷怪怪的服务端等级聚合）。

    列表页每行徽标用；扫 1750 张 map.json 只做一次，结果回写 built.json
    （同 _wzjs_mob_ids 的缓存策略），之后零成本。建库进行中不写。
    """
    try:
        b = json.loads(WZJS_BUILT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(b.get("mob_lv"), dict):
        return b["mob_lv"]
    st = _mob_stat_table()
    if not st:
        return {}
    out: dict = {}
    for mid in b.get("ids") or []:
        try:
            mj = json.loads((WZJS_MAP / mid / "map.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        kids = {str(l.get("id", "")).lstrip("0")
                for l in mj.get("life", []) if l.get("type") == "m"}
        lvs = [st[k][0] for k in kids if k in st and (st[k][0] or 0) > 0]
        if lvs:
            out[mid] = [min(lvs), max(lvs)]
    if not _wzjsjob["running"]:
        b["mob_lv"] = out
        try:
            tmp = WZJS_BUILT.with_suffix(".tmp")
            tmp.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, WZJS_BUILT)
        except OSError:
            pass
    return out


def _map_lv_table(life: dict[str, list[dict]]) -> dict:
    """现代库：life 反向聚合（怪→图）翻成正表 地图id → [min,max] 推荐等级。"""
    st = _mob_stat_table()
    if not st:
        return {}
    out: dict = {}
    for kid, rows in life.items():
        lv = (st.get(kid) or [0])[0]
        if not lv or lv <= 0:
            continue
        for r in rows:
            pair = out.setdefault(r["mapid"], [lv, lv])
            if lv < pair[0]:
                pair[0] = lv
            if lv > pair[1]:
                pair[1] = lv
    return out


def _wzjs_registry(w: Path, progress=None) -> dict:
    """加载（stamp/regver 一致时复用）或重建 spritesheet 注册表。

    inlink 前缀含 Item/Mob：掉落物图鉴、地图掉落合成都要靠它们取图标/怪物帧
    （红药水 02000000 的图标在 Item/Consume/0200 的 **inlink 表**里，主帧表没有）。
    Character 的 inlink 不注册——350 万条发型引用会把注册表撑爆，装备图标
    本来就是主帧。
    """
    stamp = _wzjs_stamp(w)
    try:
        with gzip.open(WZJS_REG, "rt", encoding="utf-8") as f:
            raw = json.load(f)
        if (raw.get("stamp") == stamp and raw.get("ver") == WZJS_VER
                and raw.get("regver") == WZJS_REGVER):
            return raw.get("reg")
    except (OSError, ValueError):
        pass
    from vendor.clssprite import build_registry
    # build_registry 期望 aa\w 子目录（bundle 都在那层）
    reg = build_registry(w / "w", progress=progress,
                         inlink_prefixes=("Map/", "Item/", "Mob/"))
    try:
        WZJS_MAP.mkdir(parents=True, exist_ok=True)
        tmp = WZJS_REG.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"stamp": stamp, "ver": WZJS_VER,
                       "regver": WZJS_REGVER, "reg": reg}, f, ensure_ascii=False)
        os.replace(tmp, WZJS_REG)
    except OSError:
        pass
    return reg


# ---------------------------------------------------------------- 掉落物（图鉴 + 地图合成）

CLS_ICONS = CACHE / "cls-icons"          # 掉落物图标 PNG + index.json
_ICON_CAT = {1: "equip", 2: "consume", 3: "install", 4: "etc", 5: "pet"}
_drops_cache: dict = {}
_itemnames_cache: dict = {}
_iconsjob: dict = {"running": False, "error": "", "done": 0, "total": 0, "stage": ""}
_icons_lock = threading.Lock()


def _drops() -> dict:
    """vendor/drops.json：mobid(int) → [{i, q:[min,max], c:chance}]。

    JSON 对象键只能是字符串，装载时统一转回 int（查表方都拿 int 来）。
    """
    if not _drops_cache:
        try:
            raw = json.loads(
                (VENDOR / "drops.json").read_text(encoding="utf-8"))
            for k, v in raw.items():
                try:
                    _drops_cache[int(k)] = v
                except (TypeError, ValueError):
                    continue
        except (OSError, ValueError):
            pass
    return _drops_cache


def _itemnames() -> dict:
    """vendor/itemnames.json：zfill8 itemid → {name, desc}。"""
    if not _itemnames_cache:
        try:
            _itemnames_cache.update(json.loads(
                (VENDOR / "itemnames.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return _itemnames_cache


def _cls_icons_stamp() -> str:
    """掉落图标库的有效性戳：注册表戳 + drops/itemnames 的 mtime。"""
    parts = []
    try:
        with gzip.open(WZJS_REG, "rt", encoding="utf-8") as f:
            parts.append(f"reg:{json.load(f).get('stamp', '')}")
    except (OSError, ValueError):
        parts.append("reg:none")
    for p in (VENDOR / "drops.json", VENDOR / "itemnames.json"):
        try:
            parts.append(f"{p.name}:{p.stat().st_mtime:.0f}")
        except OSError:
            parts.append(f"{p.name}:none")
    return "|".join(parts)


def _cls_icons_ready() -> bool:
    try:
        d = json.loads((CLS_ICONS / "index.json").read_text(encoding="utf-8"))
        return bool(d.get("icons")) and d.get("stamp") == _cls_icons_stamp()
    except (OSError, ValueError):
        return False


def _cls_icons_build(log=None) -> None:
    """后台任务：从 spritesheet 注册表提取全部 `info/icon` 帧 → .cache\\cls-icons\\。

    图标键两形态（id 都取倒数第二段）：
      Item/<类>/<目录>/<8位id>/info/icon   —— 道具（含 inlink 补齐的红药水等）
      Character/<部位>/<8位id>/info/icon   —— 装备（穿戴贴图的图鉴头像）
    只提取**有名字或有怪掉落**的（itemnames.json / drops.json 里查得到），
    避免 UI/技能杂项把图鉴撑爆。按 bundle+贴图排序遍历，BundleTex 的
    FIFO 大图缓存不会来回重解码。
    """
    if not _icons_lock.acquire(blocking=False):
        return
    try:
        w = _cls_w_dir()
        if w is None:
            raise CliError("找不到怀旧服数据（…\\mxdclassic\\Maplestory_Classic_Data\\"
                           "StreamingAssets\\aa）", 1)
        reg = _wzjs_registry(w, progress=lambda s, d, t:
                             _iconsjob.update(stage=f"注册表 {s}"))
        names = _itemnames()
        drops = _drops()
        rev: dict[int, list] = {}          # itemid → [mobid]（哪些怪掉）
        for mob, items in drops.items():
            for e in items:
                rev.setdefault(int(e["i"]), []).append(int(mob))
        want = set(names) | {f"{i:08d}" for i in rev}
        # 只取 Item/ 与 Character/ 的图标键；键 = <wzpath>/<sub>/info/icon，
        # 所以 id 在倒数第三段（倒数第二段恒为 "info"）：
        #   Item/<类>/<目录>/<id>/info/icon、Character/<部位>/<id>/info/icon。
        # Skill/<id>/info/icon 同理取 [-3]，但不在图鉴范围。
        todo = sorted((k for k in reg
                       if k.endswith("/info/icon")
                       and (k.startswith("Item/") or k.startswith("Character/"))
                       and k.split("/")[-3] in want),
                      key=lambda k: (reg[k]["b"], reg[k]["t"], reg[k]["y"], reg[k]["x"]))
        _iconsjob.update(running=True, error="", done=0, total=len(todo),
                         stage="提取掉落物图标")
        CLS_ICONS.mkdir(parents=True, exist_ok=True)
        from vendor.clsmap import BundleTex
        tex = BundleTex(w)
        icons: dict[str, dict] = {}
        miss = 0
        for k in todo:
            iid = k.split("/")[-3]
            png = CLS_ICONS / f"{iid}.png"
            try:
                if not png.is_file():
                    im = tex.frame(reg[k])
                    if im is not None and im.width > 1 and im.height > 1:
                        im.save(png)
                if png.is_file():
                    # 类别看**真实 id** 首位（zfill8 的 "02000000" 首位是 0，会全成 other）
                    cat = _ICON_CAT.get(int(str(int(iid))[0]), "other")
                    icons[iid] = {
                        "n": (names.get(iid) or {}).get("name", ""),
                        "c": cat,
                        "m": sorted(rev.get(int(iid), []))}
                else:
                    miss += 1
            except Exception:                     # noqa: BLE001
                miss += 1
            _iconsjob["done"] += 1
        idx = {"stamp": _cls_icons_stamp(), "icons": icons}
        # ---- 金币掉落贴图（Item/Special/0900/0900000X/info/iconRaw/N，4 帧旋转动画）----
        # 客户端 MapDrops.cpp：>49 金币 / >99 捆 / >999 袋，掉落合成按金额选档。
        # 帧键是 …/iconRaw/N（不以 info/icon 结尾），上面主循环扫不到，单独提。
        meso_ok = []
        for tier in range(4):
            for fr in range(4):
                k = f"Item/Special/0900/0900000{tier}/info/iconRaw/{fr}"
                if k not in reg:
                    continue
                p = CLS_ICONS / f"meso_{tier}_{fr}.png"
                try:
                    if not p.is_file():
                        im = tex.frame(reg[k])
                        if im is not None:
                            p.parent.mkdir(parents=True, exist_ok=True)
                            im.save(p)
                    if p.is_file():
                        meso_ok.append(tier)
                        break              # 每档取到一帧就够（合成时随机档位）
                except Exception:           # noqa: BLE001
                    pass
        if meso_ok:
            idx["meso_tiers"] = sorted(set(meso_ok))
        tmp = CLS_ICONS / "index.tmp"
        tmp.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CLS_ICONS / "index.json")
        msg = (f"掉落物图标库完成：{len(icons)} 个图标"
               + (f"（{miss} 个取图失败）" if miss else ""))
        if log:
            log(msg)
        _iconsjob["stage"] = msg
    except Exception as exc:                       # noqa: BLE001
        _iconsjob["error"] = str(exc)
    finally:
        _iconsjob["running"] = False
        _icons_lock.release()


def c_cls_icons() -> dict:
    """掉落物图鉴：图标库就绪返回清单；没建好（或来源变了）自动后台构建。"""
    if not _cls_icons_ready():
        if not _iconsjob["running"]:
            threading.Thread(target=_cls_icons_build, daemon=True).start()
        return {"ready": False, "running": _iconsjob["running"],
                "done": _iconsjob["done"], "total": _iconsjob["total"],
                "stage": _iconsjob["stage"] or "等待构建"}
    d = json.loads((CLS_ICONS / "index.json").read_text(encoding="utf-8"))
    cn = _cls_itemnames()                    # 怀旧服中文名优先（index 里是英文名）
    icons = [{"id": iid,
              "name": cn.get(str(int(iid))) or v["n"],
              "cat": v["c"], "mobs": v["m"]}
             for iid, v in d["icons"].items()]
    icons.sort(key=lambda r: r["id"])
    return {"ready": True, "icons": icons}


def c_cls_icon_png(iid: str) -> Path:
    p = CLS_ICONS / f"{iid}.png"
    if not p.is_file():
        raise CliError(f"图标 {iid} 不存在（图鉴还没建好？）", 2)
    return p


_drops_api_cache: dict = {}


def c_cls_drops() -> dict:
    """掉落表（正向）：mobid(去前导零字符串) → [{i, n, c}]。

    页面叠加层「显示掉落」用（与怪物立绘叠加同款纯前端随机摆放）：
    i 是 8 位 zfill 道具 id，直接拼 /api/cls/icon/&lt;i&gt; 取图标；
    只下发**图标已提取**的道具（没图标的掉出来也看不见）；
    meso 是金币贴图可用档位（meso_&lt;tier&gt;_0.png）。
    掉落表真源是服务端（vendor/drops.json，gen-drops.py 生成）。
    """
    if _drops_api_cache.get("drops"):
        return _drops_api_cache
    names = _itemnames()
    cn = _cls_itemnames()                    # 怀旧服中文名优先，退服务端英文名
    out: dict[str, list] = {}
    for mob, items in _drops().items():
        rows = []
        for e in items:
            iid = int(e.get("i") or 0)
            if iid <= 0 or not (CLS_ICONS / f"{iid:08d}.png").is_file():
                continue
            rows.append({"i": f"{iid:08d}",
                         "n": (cn.get(str(iid))
                               or (names.get(f"{iid:08d}") or {}).get("name", "")),
                         "c": e.get("c", 0)})
        if rows:
            out[str(mob)] = rows
    meso = [t for t in range(4) if (CLS_ICONS / f"meso_{t}_0.png").is_file()]
    _drops_api_cache.clear()
    _drops_api_cache.update({"drops": out, "meso": meso})
    return _drops_api_cache


def _wzjs_build(log=None) -> None:
    """后台任务：注册表 + 1750 张 WZJS 地图 → .cache\\wzjs-map\\<id>\\。

    断点续跑：每转完一张就把进度写进 built.json（中断重跑只补缺的图）；
    单张失败不中断（记进 errs，最后汇总）。解码 atlas 按 FIFO 限内存
    （BundleTex.MAX_BYTES），全库转换摸到几百张贴图不会把 RAM 撑爆。
    """
    if not _wzjs_lock.acquire(blocking=False):
        return
    try:
        w = _cls_w_dir()
        if w is None:
            raise CliError("找不到怀旧服数据（…\\mxdclassic\\Maplestory_Classic_Data\\"
                           "StreamingAssets\\aa）—— 工作区选 mxdclassic 或安装根目录", 1)
        try:
            import UnityPy  # noqa: F401
        except ImportError:
            raise CliError("缺 UnityPy —— 先跑 python scripts\\deps.py（依赖装在 .pylibs\\）", 1)
        _wzjsjob.update(running=True, error="", done=0, total=0,
                        stage="扫描 spritesheet 注册表")
        reg = _wzjs_registry(
            w, progress=lambda s, d, t: _wzjsjob.update(stage=s))
        if not reg:
            raise CliError("spritesheet 注册表是空的（12 个包一个描述符都没解到）"
                           "—— 客户端结构变了？", 1)
        stamp = _wzjs_stamp(w)
        built: dict = {"stamp": stamp, "ver": WZJS_VER, "mver": WZJS_MVER,
                       "ids": [], "errs": {}, "mobs": []}
        stale = False                     # 旧版图库：img/ 里的 PNG 是按错的 atlas 裁的
        remap = False                     # map.json 语义过期：重转条目，贴图保留
        try:
            old = json.loads(WZJS_BUILT.read_text(encoding="utf-8"))
            if old.get("stamp") == stamp and old.get("ver") == WZJS_VER:
                built = {"stamp": stamp, "ver": WZJS_VER, "mver": WZJS_MVER,
                         "ids": list(old.get("ids") or []),
                         "errs": dict(old.get("errs") or {}),
                         "mobs": list(old.get("mobs") or [])}
                if old.get("mver") != WZJS_MVER:
                    remap = True
                    built["ids"] = []
                    built["mobs"] = []
            elif old.get("stamp") == stamp:
                stale = True
                remap = True
        except (OSError, ValueError):
            pass
        from vendor.clsmap import BundleTex, ClsMapSource
        src = ClsMapSource(w, reg)
        tex = BundleTex(w)
        names = _cls_names()
        done = set(built["ids"])
        todo = [i for i in src.map_ids() if i not in done]
        _wzjsjob.update(total=len(done) + len(todo), done=len(done),
                        stage="解析 WZJS 地图")
        for mid in todo:
            try:
                mm = src.convert(mid, WZJS_MAP / mid, tex, names, overwrite=stale)
                built["ids"].append(mid)
                if any((l or {}).get("type") == "m" for l in mm.get("life") or []):
                    built["mobs"].append(mid)
            except Exception as e:              # noqa: BLE001
                built["errs"][mid] = f"{type(e).__name__}: {str(e)[:200]}"
            _wzjsjob["done"] += 1
            try:
                tmp = WZJS_BUILT.with_suffix(".tmp")
                tmp.write_text(json.dumps(built, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, WZJS_BUILT)
            except OSError:
                pass
        if not built["ids"]:
            first = next(iter(built["errs"].values()), "")
            raise CliError(f"WZJS 地图全转失败（{len(built['errs'])} 张）：{first}", 1)
        msg = (f"怀旧服大图库完成：{len(built['ids'])} 张"
               + (f"（{len(built['errs'])} 张失败，明细 {WZJS_BUILT}）"
                  if built["errs"] else ""))
        if log:
            log(msg)
        _wzjsjob["stage"] = msg
    except Exception as exc:                    # noqa: BLE001
        _wzjsjob["error"] = str(exc)
    finally:
        _wzjsjob["running"] = False
        _wzjs_lock.release()


def c_cls_maps() -> dict:
    """怀旧服地图列表：优先 WZJS 大图库（1750 张，可渲染大图），
    没建好再退回小地图库的 523 张 canvas × 本地名表（1838 张）。

    WZJS 模式的行可以直接渲染完整地图（/api/map/export 带 cls=1）；
    canvas 模式只有小图可看。mod=同 id 在现代库里也存在（可以跳过去看）。
    """
    names = _cls_names()
    try:
        modern = set(c_maps()["maps"])
    except Exception:                              # noqa: BLE001
        modern = set()
    if _wzjs_ready():
        built = json.loads(WZJS_BUILT.read_text(encoding="utf-8"))
        mobs = set(_wzjs_mob_ids())
        out = []
        for mid in sorted(built.get("ids") or []):
            n = names.get(mid.lstrip("0") or "0") or {}
            out.append({"id": mid, "street": n.get("street", ""),
                        "name": n.get("name", ""), "desc": n.get("desc", ""),
                        "mob": mid in mobs,
                        "mod": mid.zfill(9) in modern})
        return {"maps": out, "names": len(names), "wzjs": True,
                "life_maps": sorted(mobs), "mob_lv": _wzjs_map_lv()}
    if not _cls_ready():
        raise CliError("怀旧服小地图库还没建好 —— 数据源面板点「⬇ 转换缺失数据」", 1)
    idx = json.loads((CLS_MMIDX / "index.json").read_text(encoding="utf-8"))
    out = []
    for mid in sorted(idx):
        n = names.get(mid.lstrip("0") or "0") or {}
        ent = idx[mid] or {}
        out.append({"id": mid, "street": n.get("street", ""), "name": n.get("name", ""),
                    "desc": n.get("desc", ""), "w": ent.get("cw"), "h": ent.get("ch"),
                    "mod": mid in modern})
    return {"maps": out, "names": len(names), "wzjs": False, "mob_lv": None}


def c_cls_tables() -> dict:
    """怀旧服 Mob/Skill 名单（前端筛选用）+ 库状态。"""
    t = _cls_tables()
    return {"ready": _cls_ready(), "mob": t["mob"], "skill": t["skill"]}


def _cls_bundle_meta() -> Path | None:
    """上次用过的来源 bundle（还在且 mtime 没变就复用，免去全包探测）。"""
    try:
        meta = json.loads((CLS_MMIDX / "index.meta.json").read_text(encoding="utf-8"))
        b = Path(meta["bundle"])
        if b.is_file() and b.stat().st_mtime == meta.get("mtime"):
            return b
    except (OSError, ValueError, KeyError):
        pass
    return None


def _cls_build() -> None:
    """后台线程：定位 canvas atlas → 提取 523 张透明 PNG → 建找图签名。

    bundle 里的 canvas 存在一张大 spritesheet atlas 上（透明区 RGB 是相邻贴图
    的杂色），所以提取必须按 alpha 裁内容区、RGB 预乘 alpha（透明=黑，和找图
    匹配的底色约定一致），PNG 保留透明通道给人看。
    bundle 定位：压缩存储没法快扫字符串，只能 UnityPy 开包——按体积从大到小
    试（canvas atlas 是最大的那个），找到就记进 meta，之后复用不再探测。
    """
    if not _cls_lock.acquire(blocking=False):
        return
    try:
        import struct
        try:
            import UnityPy
        except ImportError:
            raise CliError("缺 UnityPy —— 先跑 python scripts\\deps.py（依赖装在 .pylibs\\）", 1)
        _clsjob.update(running=True, error="", done=0, total=0, stage="定位数据包")
        w = _cls_w_dir()
        if w is None:
            raise CliError("找不到怀旧服数据（…\\mxdclassic\\Maplestory_Classic_Data\\"
                           "StreamingAssets\\aa）—— 工作区选 mxdclassic 或安装根目录", 1)
        cached = _cls_bundle_meta()
        env = None
        bundle = None
        if cached is not None:
            bundle = cached
            _clsjob.update(stage=f"打开 {bundle.name[:20]}…")
            env = UnityPy.load(str(bundle))
            if not any(o.type.name == "MonoBehaviour" and b"miniMap/canvas" in o.get_raw_data()[:400]
                       for o in env.objects):
                env = None                        # 缓存的包里没有了（客户端大更新）：重新探测
        if env is None:
            cands = sorted((w / "w").glob("spritesheet_*.bundle"),
                           key=lambda p: p.stat().st_size, reverse=True)
            for i, p in enumerate(cands[:6]):
                _clsjob.update(stage=f"探测数据包 {i + 1}/{min(6, len(cands))}：{p.name[:20]}…")
                env = UnityPy.load(str(p))
                if any(o.type.name == "MonoBehaviour" and b"miniMap/canvas" in o.get_raw_data()[:400]
                       for o in env.objects):
                    bundle = p
                    break
                env = None
        if env is None or bundle is None:
            raise CliError("canvas atlas 不在前 6 大的 spritesheet 里 —— 客户端结构变了，"
                           "把 aa\\w 目录发出来看看", 1)

        # 描述符：MonoBehaviour 里 [0,0,0,W,H,pivX,pivY,0]（W×H 是真实区域）
        _clsjob.update(stage="解析 canvas 描述符")
        desc: dict[str, tuple[int, int]] = {}
        for o in env.objects:
            if o.type.name != "MonoBehaviour":
                continue
            raw = o.get_raw_data()
            if b"miniMap/canvas" not in raw[:400]:
                continue
            ss = re.findall(rb"[\x20-\x7e]{4,}", raw[:160])
            if len(ss) < 4 or not ss[1].startswith(b"Map/Map/Map"):
                continue
            k = (raw.find(b"miniMap/canvas") + 14 + 3) & ~3
            if k + 32 > len(raw):
                continue
            f = struct.unpack("<8f", raw[k:k + 32])
            if f[3] > 1 and f[4] > 1:
                desc[ss[0].decode("utf-8", "replace")] = (int(f[3]), int(f[4]))
        if not desc:
            raise CliError("canvas 描述符解析失败（一个都没解到）", 1)

        _clsjob.update(stage="提取小地图", done=0, total=len(desc))
        if CLS_MMIDX.exists():
            drop_dir(CLS_MMIDX)                  # 旧库整目录进 trash，不逐文件删
        CLS_MMIDX.mkdir(parents=True, exist_ok=True)
        index: dict[str, dict] = {}
        for o in env.objects:
            if o.type.name != "Texture2D":
                continue
            t = o.read()
            if not t.m_Name.endswith("_0"):
                continue
            mid = t.m_Name[:-2]
            if mid not in desc:
                continue
            img = np.array(t.image.convert("RGBA"))
            a = img[:, :, 3]
            ys, xs = np.where(a > 0)
            if len(xs) == 0:
                continue
            x0, x1 = int(xs.min()), int(xs.max()) + 1
            y0, y1 = int(ys.min()), int(ys.max()) + 1
            crop = img[y0:y1, x0:x1]
            h, w = crop.shape[:2]
            if w < 8 or h < 8:
                continue
            # RGB 预乘 alpha：透明=黑（atlas 杂色绝不能进库），PNG 仍带透明通道
            bgra = cv2.cvtColor(crop, cv2.COLOR_RGBA2BGRA)
            al = (bgra[:, :, 3:4].astype(np.float32) / 255.0)
            bgra[:, :, :3] = (bgra[:, :, :3].astype(np.float32) * al).astype(np.uint8)
            cv2.imwrite(str(CLS_MMIDX / f"{mid}.png"), bgra)
            index[mid] = {"cw": w, "ch": h}
            _clsjob["done"] = len(index)
        if not index:
            raise CliError("一张 canvas 都没提取到 —— 贴图和描述符没对上", 1)
        (CLS_MMIDX / "index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        st = bundle.stat()
        (CLS_MMIDX / "index.meta.json").write_text(json.dumps({
            "bundle": str(bundle), "mtime": st.st_mtime, "size": st.st_size,
            "bundle_atime": time.time(), "count": len(index)}), encoding="utf-8")
        _clsjob["total"] = _clsjob["done"] = len(index)
        _cls_thumbs_build(progress=True)
        _clsjob["stage"] = ""
    except Exception as exc:                    # noqa: BLE001
        _clsjob["error"] = str(exc)
    finally:
        _clsjob["running"] = False
        _cls_lock.release()


def _cls_thumb_one(mid: str):
    g = cv2.imread(str(CLS_MMIDX / f"{mid}.png"), cv2.IMREAD_GRAYSCALE)
    if g is None:
        return None
    t = cv2.resize(g, (THUMB_PX, THUMB_PX),
                   interpolation=cv2.INTER_AREA).astype(np.float32)
    t -= t.mean()
    std = float(t.std())
    if std >= 1e-3:
        t = np.clip(t / std * 127.0, -127, 127)
    else:
        t = np.zeros_like(t)
    return t.astype(np.int8)


def _cls_thumbs_build(progress: bool = False) -> None:
    """怀旧服库的 64×64 归一化签名（500 来张，秒级）。"""
    if cv2 is None or np is None:
        return
    if _cls_thumbs["building"]:
        return
    _cls_thumbs["building"] = True
    try:
        index = json.loads((CLS_MMIDX / "index.json").read_text(encoding="utf-8"))
        mids = sorted(index.keys())
        if progress:
            _clsjob.update(stage="生成找图签名", done=0, total=len(mids))
        rows = [_cls_thumb_one(m) for m in mids]
        arr = np.zeros((len(mids), THUMB_PX, THUMB_PX), dtype=np.int8)
        kept: list[str] = []
        for mid, t in zip(mids, rows):
            if t is None:
                continue
            arr[len(kept)] = t
            kept.append(mid)
        tmp = CLS_MMIDX / "thumbs.npz.tmp"
        with open(tmp, "wb") as f:
            np.savez_compressed(f, mids=np.array(kept), thumbs=arr[:len(kept)],
                                stamp=np.float64(time.time()))
        os.replace(tmp, CLS_THUMBS_NPZ)
        if progress:
            _clsjob["done"] = _clsjob["total"]
        _cls_thumbs["state"] = None
    except Exception:                        # noqa: BLE001
        pass
    finally:
        _cls_thumbs["building"] = False


def _cls_thumbs_load():
    """返回 (mids, float32 签名矩阵)；库不在/未建返回 None。"""
    if cv2 is None or np is None:
        return None
    if not _cls_ready():
        return None
    st = _cls_thumbs["state"]
    if st is not None and st["bundle_mtime"] == json.loads(
            (CLS_MMIDX / "index.meta.json").read_text(encoding="utf-8")).get("mtime"):
        return st["mids"], st["arr"]
    try:
        with np.load(CLS_THUMBS_NPZ, allow_pickle=False) as z:
            st = {"bundle_mtime": json.loads((CLS_MMIDX / "index.meta.json")
                   .read_text(encoding="utf-8")).get("mtime"),
                  "mids": z["mids"], "arr": z["thumbs"].astype(np.float32)}
    except Exception:                        # noqa: BLE001
        return None
    _cls_thumbs["state"] = st
    return st["mids"], st["arr"]


def _cls_score_one(qg, cg):
    """查询图 vs 一张怀旧服 canvas：多尺度双向匹配（等比尺度，粗排用）。

    游戏里显示倍率不固定（勇士部落东入口实测 ~1.5×，面板还会裁边），和现代版
    「单尺度+长宽比硬淘汰」不同：这里以面积比 f0 为基准扫 ±倍率，双向模板匹配
    取最高分。返回 (score, canvas 相对查询图的缩放倍数)。"""
    qh, qw = qg.shape
    ch, cw = cg.shape
    if qh < 8 or qw < 8 or ch < 8 or cw < 8:
        return None
    f0 = math.sqrt((qw * qh) / float(cw * ch))
    best = (-1.0, 1.0)
    for mult in (0.5, 0.7, 0.85, 1.0, 1.15, 1.3, 1.5, 1.75, 2.0, 2.5):
        r, f = _cls_match_at(qg, cg, f0 * mult, f0 * mult)
        if r is not None and r > best[0]:
            best = (r, f)
    return best if best[0] > 0 else None


def _cls_score_aniso(qg, cg):
    """精排：在各等比尺度附近允许 ±8% 的各向异性缩放。

    实测游戏内小地图不是严格等比——面板把 canvas 塞进窗口时横向多裁/纵向少裁
    会带 ~4% 的纵横比偏差，等比网格恰好会跨过最佳对齐点（东入口两张截图在纯
    等比下只有 0.45，各向异性网格直接拉回 0.75）。粗排用 _cls_score_one，只有
    前几名才值得花这份算力。"""
    qh, qw = qg.shape
    ch, cw = cg.shape
    if qh < 8 or qw < 8 or ch < 8 or cw < 8:
        return None
    f0 = math.sqrt((qw * qh) / float(cw * ch))
    best = (-1.0, 1.0)
    for gm in (0.5, 0.7, 0.85, 1.0, 1.15, 1.3, 1.5, 1.75, 2.0, 2.5):
        gf = f0 * gm
        for dx in (-0.08, -0.04, 0, 0.04, 0.08):
            for dy in (-0.08, -0.04, 0, 0.04, 0.08):
                r, f = _cls_match_at(qg, cg, gf * (1 + dx), gf * (1 + dy))
                if r is not None and r > best[0]:
                    best = (r, f)
    return best if best[0] > 0 else None


def _cls_match_at(qg, cg, fx, fy):
    """canvas 按 (fx, fy) 缩放后与查询图双向模板匹配；落不进任一方向的尺度
    （一边大一边小，无法整窗对齐）返回 (None, f)。"""
    qh, qw = qg.shape
    ch, cw = cg.shape
    nw = max(8, int(round(cw * fx)))
    nh = max(8, int(round(ch * fy)))
    interp = cv2.INTER_AREA if fx < 1 and fy < 1 else cv2.INTER_LINEAR
    c2 = cv2.resize(cg, (nw, nh), interpolation=interp)
    try:
        if nw <= qw and nh <= qh:              # canvas 缩进查询图里
            r = float(cv2.matchTemplate(qg, c2, cv2.TM_CCOEFF_NORMED).max())
        elif nw >= qw and nh >= qh:           # 查询图是 canvas 的一部分
            r = float(cv2.matchTemplate(c2, qg, cv2.TM_CCOEFF_NORMED).max())
        else:
            return None, fx
        return r, (fx + fy) / 2
    except cv2.error:
        return None, fx


def c_cls_imgsearch(image: str) -> dict:
    """怀旧服截图找图：.cache\\minimap-cls 的 canvas 三级匹配。

    粗筛（64×64 签名）→ 等比多尺度粗排 → 前 15 名各向异性精排。
    整窗截图（带 UI 的大图）先按宽度降到 700px 以内，否则 matchTemplate 要跑分钟级。"""
    if cv2 is None or np is None:
        raise CliError("缺依赖：先跑 python scripts\\deps.py（依赖装在 .pylibs\\ 里）", 1)
    if _cls_w_dir() is None:
        raise CliError("当前工作区找不到怀旧服客户端（mxdclassic 的 Unity 数据包）", 1)
    if not _cls_ready():
        if not _clsjob["running"]:
            threading.Thread(target=_cls_build, daemon=True).start()
        return {"building": True}
    th = _cls_thumbs_load()
    if th is None:                            # 签名还没落盘（正在建）：前端轮询
        return {"building": True}
    mids, arr = th
    raw_b64 = image.split(",", 1)[-1] if image.startswith("data:") else image
    buf = np.frombuffer(base64.b64decode(raw_b64), dtype=np.uint8)
    q = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if q is None:
        raise CliError("图片解析失败 —— 确认粘贴的是截图", 2)
    if q.shape[1] > 560:                      # 整窗截图降采样：面板撑死几百像素宽，
        s = 560.0 / q.shape[1]                # 全窗大图不降的话 matchTemplate 要跑分钟级
        q = cv2.resize(q, (560, max(8, int(round(q.shape[0] * s)))),
                       interpolation=cv2.INTER_AREA)
    qg = cv2.cvtColor(q, cv2.COLOR_BGR2GRAY)
    q64 = cv2.resize(qg, (THUMB_PX, THUMB_PX),
                     interpolation=cv2.INTER_AREA).astype(np.float32)
    q64 -= q64.mean()
    qn = float(q64.std())
    if qn < 1e-3:
        raise CliError("截图几乎是纯色（没有可识别的地形），换一张小地图截图", 2)
    q64 /= qn
    coarse = arr.reshape(len(mids), -1) @ q64.reshape(-1)
    cand_mids = [str(mids[i]) for i in np.argsort(coarse)[::-1][:60]]

    def rough(mid: str):
        cg = cv2.imread(str(CLS_MMIDX / f"{mid}.png"), cv2.IMREAD_GRAYSCALE)
        if cg is None:
            return None
        r = _cls_score_one(qg, cg)
        return None if not r else [r[0], r[1], mid]

    with ThreadPoolExecutor(max_workers=8) as ex:
        s1 = [c for c in ex.map(rough, cand_mids) if c]
    s1.sort(reverse=True)
    top15 = [c[2] for c in s1[:15]]

    def refine(mid: str):
        cg = cv2.imread(str(CLS_MMIDX / f"{mid}.png"), cv2.IMREAD_GRAYSCALE)
        if cg is None:
            return None
        r = _cls_score_aniso(qg, cg)
        if not r:
            return None
        for c in s1:                           # 粗排分数保底（aniso 缺网的尺度不至于丢分）
            if c[2] == mid and c[0] > r[0]:
                return [c[0], c[1], mid]
        return [r[0], r[1], mid]

    with ThreadPoolExecutor(max_workers=8) as ex:
        s2 = [c for c in ex.map(refine, top15) if c]
    for c in s1:                              # 没进前 15 的保持粗排分数垫底
        if c[2] not in top15:
            s2.append(c)
    s2.sort(reverse=True)

    index = json.loads((CLS_MMIDX / "index.json").read_text(encoding="utf-8"))
    # 怀旧服 id 有一半左右在现代版数据里也有（同一张图新旧客户端共用 9 位 id，
    # 实测 523 张里 230 张重叠）——这些结果可以直接点开看/导出大地图。
    # 剩下的再按「街道 + 地图名」在现代库里找同名图（新旧版改过 id 的图，21 张）。
    # 两条都不中就是现代版确实没有这张图（改版删掉的旧地图），只能识别 id。
    modern_ids: set[str] = set()
    modern_by_name: dict[tuple[str, str], list[str]] = {}
    try:
        modern_ids = set(c_maps().get("maps") or [])
        mj = MMIDX / "index.json"
        if mj.exists():
            for k, v in json.loads(mj.read_text(encoding="utf-8")).items():
                if not isinstance(v, dict):
                    continue
                k = str(k)
                modern_ids.add(k)
                nm, sd = v.get("name") or "", v.get("street") or ""
                if nm:
                    modern_by_name.setdefault((sd, nm), []).append(k)
    except Exception:                        # noqa: BLE001
        pass
    cls_names = _cls_names()

    def resolve(mid: str):
        """一张 canvas → (open_id, note, mod, alt, 名字元信息)。"""
        nm = cls_names.get(mid.lstrip("0") or "0") or {}
        in_mod = mid in modern_ids
        alt = ""
        if not in_mod and nm.get("name"):
            cand = modern_by_name.get((nm.get("street") or "", nm["name"])) or []
            if len(set(cand)) == 1:          # 同名唯一才敢认，重名的不猜
                alt = cand[0]
        note = ("现代版有同一张图" if in_mod else
                f"现代版有同名图 {alt}" if alt else
                "现代版数据里没有这张图（只能识别）")
        return (mid if in_mod else alt), note, in_mod, alt, nm

    # 同一张大地图常被多张 canvas 覆盖：现代版同 id 的和怀旧服独有 id 的往往是
    # 同一张图；「隐秘的研究室」这种还有 24 个 id 共用一张 canvas。按打开目标
    # 或「街道+名字」归并，只留分数最高的一行，并标出同名共几张。
    groups: dict[str, dict] = {}
    order: list[str] = []
    for score, f, mid in s2[:40]:
        open_id, note, in_mod, alt, nm = resolve(mid)
        key = open_id or (f"{nm.get('street')}/{nm.get('name')}" if nm.get("name") else f"id:{mid}")
        if key not in groups:
            groups[key] = {"row": {"id": mid, "score": round(score, 3), "scale": round(f, 2),
                                   "name": nm.get("name") or "", "street": nm.get("street") or "",
                                   "desc": nm.get("desc") or "", "note": note,
                                   "mod": in_mod, "alt": alt, "open": open_id,
                                   "w": index.get(mid, {}).get("cw"),
                                   "h": index.get(mid, {}).get("ch"),
                                   "thumb": f"/preview/minimap-cls/{mid}.png"},
                           "more": 0, "ids": [mid]}
            order.append(key)
        else:
            g = groups[key]
            g["more"] += 1
            g["ids"].append(mid)
            if score > g["row"]["score"]:      # 用分数更高的那行做代表
                g["row"].update({"id": mid, "score": round(score, 3), "scale": round(f, 2),
                                 "w": index.get(mid, {}).get("cw"),
                                 "h": index.get(mid, {}).get("ch"),
                                 "thumb": f"/preview/minimap-cls/{mid}.png"})
    out = []
    for key in order[:9]:
        g = groups[key]
        row = dict(g["row"])
        if g["more"]:
            row["note"] += f"（同图共 {g['more'] + 1} 张）"
        out.append(row)
    return {"building": False, "results": out}


def _web_clsbuild() -> dict:
    """WebUI「怀旧服小地图库」按钮：强制重建（旧库进 trash，后台线程跑）。"""
    if _clsjob["running"]:
        raise CliError("怀旧服小地图库正在构建中", 1)
    if _cls_w_dir() is None:
        raise CliError("当前工作区找不到怀旧服客户端（mxdclassic 的 Unity 数据包）", 1)
    for name in ("index.meta.json", "thumbs.npz"):
        f = CLS_MMIDX / name
        if f.exists():
            f.unlink()
    threading.Thread(target=_cls_build, daemon=True).start()
    return {"started": True}


def c_cls_imgindex() -> dict:
    """怀旧服小地图库状态（找图弹窗轮询用，含构建进度）。"""
    return {**_cls_state(), "done": _clsjob["done"], "total": _clsjob["total"],
            "stage": _clsjob.get("stage", ""), "error": _clsjob["error"]}





# 导出 ----------------------------------------------------------------------

_trash_busy = threading.Event()


def _sweep_trash_now() -> None:
    """同步清空 .cache/trash（rd /s /q 比逐文件删快几十倍；删不掉就重试）。

    供「彻底清除」直接调用——清除不只是挪进 trash，必须把 trash 也删掉才算完。
    """
    trash = CACHE / "trash"
    if not trash.exists():
        return
    _trash_busy.set()
    try:
        for c in list(trash.iterdir()):
            for _ in range(3):          # 被占用/扫描锁拦下就再试，最多 3 轮
                try:
                    if os.name == "nt":
                        subprocess.run(["cmd", "/c", "rd", "/s", "/q", str(c)],
                                       capture_output=True)
                    elif c.is_dir():
                        for f in sorted(c.rglob("*"), key=lambda x: len(x.parts), reverse=True):
                            f.rmdir() if f.is_dir() else f.unlink()
                        c.rmdir()
                    else:
                        c.unlink()
                except BaseException:
                    pass
                if not c.exists():
                    break
        try:
            trash.rmdir()               # 清空后把 trash 目录本身也拿掉
        except BaseException:
            pass
    finally:
        _trash_busy.clear()


def _trash_sweep_async() -> None:
    """后台尽力清空 .cache/trash（常规单目录清理用；清除走 _sweep_trash_now）。"""
    if _trash_busy.is_set():
        return
    threading.Thread(target=_sweep_trash_now, daemon=True).start()


def drop_dir(p: Path) -> None:
    """清空一个导出/缓存目录：**整体改名挪进 .cache/trash/**，秒级完成。

    Windows 上逐文件 rglob+unlink 1.4 万个文件实测只有 19 个/秒（十几分钟），
    还会被环境的批量删除保护拦下——所以必须走目录 rename 这条无损快路，
    真正的删除交给后台线程用系统 rd /s /q 去啃。
    """
    if not p.exists():
        return
    try:
        trash = CACHE / "trash"
        trash.mkdir(parents=True, exist_ok=True)
        p.rename(trash / f"{p.name}-{int(time.time() * 1000)}")
        _trash_sweep_async()
        return
    except BaseException:
        pass
    try:
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "rd", "/s", "/q", str(p)], capture_output=True)
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
        _run(["nxmob", "export", str(outdir), "--only", raw, "--named"], _nx_dir(),
             timeout=600)
    return outdir


def _png_feet_oy(p: Path, h: int) -> int:
    """帧 PNG 里最后一个「肉眼可见」行（该行存在 alpha>=100 的像素）的行号+1。

    幽灵/烟雾类怪（幽灵白雪人等）的底部是一段渐隐尾巴：alpha 从 255 一路掉到
    17~85，贴图确实贴着 canvas 底，但深色背景上那段尾巴完全看不见——按 canvas
    底对齐就会显得整只怪浮空。按可见底部做竖直锚点，普通怪（脚踩实线）结果
    不变，幽灵怪的可见身体才踩在地面上。打不开图就退回 canvas 高度。
    """
    try:
        from PIL import Image
        with Image.open(p) as im:
            if im.mode != "RGBA":
                im = im.convert("RGBA")
            w, ih = im.size
            if ih != h:
                return h                       # manifest 和图对不上，别瞎修
            data = im.tobytes()                # RGBA 行主序
        step = w * 4
        for y in range(ih - 1, -1, -1):
            row = data[y * step:(y + 1) * step]
            for i in range(3, step, 4):
                if row[i] >= 100:
                    return y + 1
    except Exception:
        pass
    return h


def _mob_anchor(w: int, h: int, ox: int, oy: int,
                path: "Path | None" = None) -> tuple[int, int]:
    """怪物帧锚点兜底：国服 Mob.nx 的帧节点下没有 origin 属性，nxmob 写出的
    manifest 全是 (0,0)——那是「贴图左上角」而不是锚点，会把怪摆到地面右下方。
    没有真锚点时按怪物通用规则取脚底中心；竖直方向尽量用 _png_feet_oy 的
    「可见底部」而不是 canvas 底（幽灵怪的渐隐尾巴在深色背景上看不见）。"""
    if ox == 0 and oy == 0:
        return w // 2, (_png_feet_oy(path, h) if path else h)
    return ox, oy


def _mob_anim_frames(ws: Path, kind_id: str) -> dict:
    """一种怪的全部动画帧，按动作分组：{action: [(path, w, h, ox, oy), ...]}。

    每帧 PNG 都按自己的包围盒裁出，ox/oy 是该帧锚点（脚底基准）；
    stand/move/attack1/hit1/die1/jump/fly 等动作都在 manifest 里。没图返回 {}。
    """
    raw = kind_id.lstrip("0") or "0"
    mobnx = _nx_dir() / "Mob.nx"
    if mobnx.exists():
        meta = {m["id"]: m for m in mob_meta(mobnx)}
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
            ox, oy = _mob_anchor(w, h, ox, oy, by_name[f[4]])
            anims.setdefault(f[2], []).append((by_name[f[4]], w, h, ox, oy))
    if not anims and by_name:          # 兜底：没有 manifest 就按文件名前缀分组
        from PIL import Image
        for name, p in sorted(by_name.items()):
            with Image.open(p) as im:
                w, h = im.size
            anims.setdefault(name.split("_")[0], []).append((p, w, h, w // 2, _png_feet_oy(p, h)))
    return _strip_noise_anims(anims)


# 各动作的抽取权重：站/走/飞最常见，攻击次之，受击/死亡/跳跃偏少
ACT_WEIGHTS = (("stand", 3), ("move", 3), ("walk", 3), ("fly", 3),
               ("attack", 2), ("regen", 2),
               ("hit", 1), ("die", 1), ("jump", 1))


def _act_weight(act: str) -> float:
    for pre, w in ACT_WEIGHTS:
        if act.startswith(pre):
            return float(w)
    return 1.0


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
        ox, oy = _mob_anchor(w, h, ox, oy, p)
        out.append({"file": f[4],
                    "path": str(p),
                    "url": url_prefix + p.relative_to(root).as_posix(),
                    "anim": f[2], "frame": int(f[3]),
                    "w": w, "h": h, "ox": ox, "oy": oy, "delay": delay})
    # 素材页展示列表同样剔除加密噪点帧（源文件保留，只是不上台面）
    return [r for r in out if not _is_noise(Path(r["path"]))]


def c_mob_export(raw: str, preview: bool = False) -> dict:
    """导出一只怪的全部动画帧 PNG（逐帧，含锚点 manifest）。

    preview=True 只出预览（.cache，重复调用秒回）；默认导出到 .exports/mobs/。
    链接怪（info/link）自动顺链导出目标怪。
    """
    ws = _need_ws()
    raw = str(raw).strip()
    if not re.fullmatch(r"\d+", raw):
        raise CliError("怪 id 不合法（应为数字）", 2)
    raw_req = raw
    mobnx = _nx_dir() / "Mob.nx"
    if mobnx.exists():
        meta = {m["id"]: m for m in mob_meta(mobnx)}
        raw = _resolve_mob_link(raw, meta)
        raw = (meta.get(raw) or {}).get("raw") or raw.zfill(7)
    link_info = {"from": raw_req, "to": raw} if raw != raw_req else None
    if preview:
        outdir = _ensure_mob_preview(raw, ws)
    else:
        outdir = EXPORTS / "mobs" / raw
        if outdir.exists():
            drop_dir(outdir)
        outdir.parent.mkdir(parents=True, exist_ok=True)
        _run(["nxmob", "export", str(outdir), "--only", raw, "--named"], _nx_dir(),
             timeout=600)
    pngs = sorted(outdir.rglob("*.png"))
    if not pngs:
        raise CliError("没导出任何帧（这只怪可能没有图片资源）", 1)
    root = CACHE if preview else EXPORTS
    prefix = "/preview/" if preview else "/exports/"
    good = [p for p in pngs if not _is_noise(p)]
    if not good:
        raise CliError("这只怪的帧全是加密噪点（国服运行时解密，无法还原）", 1)
    return {"dir": str(outdir), "count": len(good),
            "pngs": [prefix + p.relative_to(root).as_posix() for p in good[:400]],
            "png_paths": [str(p) for p in good],
            "frames": _read_manifest(outdir, root, prefix, pngs),
            "preview": preview, "link": link_info}


# ---------------------------------------------------------------- 人物素材（角色）

#: 部位 -> Character.nx 里的目录名。skin（肤色）比较特殊：身体 000020XX / 头 000120XX
CHAR_CATS = {
    "skin": "Skin", "hair": "Hair", "face": "Face", "cap": "Cap",
    "coat": "Coat", "pants": "Pants", "longcoat": "Longcoat", "shoes": "Shoes",
    "glove": "Glove", "cape": "Cape", "shield": "Shield", "weapon": "Weapon",
}
CHAR_SLOTS = ("hair", "face", "cap", "coat", "pants", "longcoat",
              "shoes", "glove", "cape", "shield", "weapon")

#: 客户端 Body.cpp 里的肤色名（String.nx 里没有 Skin 段）
CHAR_SKINS = ["Light", "Tan", "Dark", "Pale", "Ashen", "Green", "", "", "",
              "Ghostly", "Pale Pink", "Clay", "Alabaster"]

#: 图层顺序：客户端权威 zmap（国服 Base.wz 的 zmap_cn.img 数值序，zmap.img
#: 并集补齐变体名，184 项，2026-11-09 提取）。zmap 数值越小越在顶层
#: （face 40 < head 43 → 脸画在头盖骨上面；capAccessoryBelowBody 77 >
#: body 74 → 假发帽脑后块藏在身体后面），表已按绘制序排好（底→顶）。
#: 此前手抄的 40 项映射漏了 36 个 z 名——假发帽的 capAccessoryBelowBody
#: 落进兜底 50 盖住脸，是「人物没有人脸」bug 的根因。
CHAR_ZMAP_ORDER = (
    'backSaddleFront', 'saddleMid', 'backWeaponEffectUnder', 'Bd', 'Hd', 'backWeaponEffectOver',
    'Hr', 'Fc', 'At', 'Af', 'Am', 'Ae',
    'As', 'Ay', 'Cp', 'Ri', 'backWeaponOverGloveEffectUnder', 'Gv',
    'Wp', 'backWeaponOverGloveEffectOver', 'Si', 'So', 'Pn', 'Ws',
    'Ma', 'Wg', 'Sr', 'Tm', 'Sd', 'backTamingMobMid',
    'backMobEquipUnderSaddle', 'backSaddle', 'backAccessoryOverHead', 'backMobEquipMid', 'backTamingMobFront', 'backMobEquipFront',
    'mobEquipRear', 'tamingMobRear', 'backWeaponOverHeadEffectUnder', 'saddleRear', 'characterEnd', 'backWeaponOverHeadEffectOver',
    'backWeapon', 'backHairBelowHead', 'backShieldBelowBody', 'backMailChestAccessory', 'backCapAccessory', 'backWeaponOverShieldEffectUnder',
    'backAccessoryFace', 'backAccessoryEar', 'backWeaponOverShieldEffectOver', 'backBody', 'backGlove', 'backGloveWrist',
    'weaponBelowBodyEffectUnder', 'backWeaponOverGlove', 'backMailChestBelowPants', 'weaponBelowBodyEffectOver', 'backPantsBelowShoes', 'backShoesBelowPants',
    'backPants', 'backShoes', 'backPantsOverShoesBelowMailChest', 'backMailChest', 'backPantsOverMailChest', 'backMailChestOverPants',
    'backHead', 'backAccessoryFaceOverHead', 'backCape', 'backHairBelowCap', 'backHairBelowCapNarrow', 'backHairBelowCapWide',
    'backWeaponOverHead', 'backCap', 'backHair', 'backCapOverHair', 'backShield', 'backWeaponOverShield',
    'backWing', 'backHairOverCape', 'capeBelowWeapon', 'weaponBelowBody', 'weaponOverBodyEffectUnder', 'hairBelowBody',
    'capeBelowBody', 'weaponOverBodyEffectOver', 'shieldBelowBody', 'capAccessoryBelowBody', 'gloveBelowBody', 'gloveWristBelowBody',
    'body', 'gloveOverBody', 'mailChestBelowPants', 'weaponOverArmBelowHeadEffectUnder', 'pantsBelowShoes', 'shoes',
    'weaponOverArmBelowHeadEffectOver', 'pants', 'mailChestOverPants', 'weaponEffectUnder', 'shoesOverPants', 'pantsOverShoesBelowMailChest',
    'weaponEffectOver', 'shoesTop', 'mailChest', 'pantsOverMailChest', 'mailChestOverHighest', 'gloveWristOverBody',
    'mailChestTop', 'weaponOverBody', 'armBelowHead', 'mailArmBelowHead', 'armBelowHeadOverMailChest', 'accessoryEyeBelowFace',
    'gloveBelowHead', 'mailArmBelowHeadOverMailChest', 'gloveWristBelowHead', 'weaponOverArmBelowHead', 'shield', 'weapon',
    'arm', 'hand', 'glove', 'capeOverFace', 'mailArm', 'gloveWrist',
    'cape', 'head', 'hairShade', 'accessoryFaceBelowFace', 'accessoryOverHair', 'face',
    'accessoryEarOverHair', 'accessoryFaceOverFaceBelowCap', 'capBelowAccessory', 'accessoryEar', 'weaponBelowArmEffectUnder', 'capAccessoryBelowAccFace',
    'accessoryFace', 'weaponBelowArmEffectOver', 'accessoryEyeShadow', 'accessoryEye', 'hair', 'cap',
    'capAccessory', 'accessoryEyeOverCap', 'capeOverArm', 'hairOverHead', 'capOverHair', 'weaponOverArmEffectUnder',
    'weaponBelowArm', 'armOverHairBelowWeapon', 'weaponOverArmEffectOver', 'mailArmOverHairBelowWeapon', 'armOverHair', 'gloveBelowMailArm',
    'mailArmOverHair', 'gloveWristBelowMailArm', 'weaponOverHandEffectUnder', 'weaponOverArm', 'handBelowWeapon', 'weaponOverHandEffectOver',
    'gloveBelowWeapon', 'gloveWristBelowWeapon', 'shieldOverHair', 'weaponOverHand', 'weaponOverGloveEffectUnder', 'handOverHair',
    'gloveOverHair', 'weaponOverGloveEffectOver', 'gloveWristOverHair', 'weaponOverGlove', 'weaponWristOverGloveEffectUnder', 'capeOverHead',
    'weaponWristOverGlove', 'weaponWristOverGloveEffectOver', 'emotionOverBody', 'characterStart', 'tamingMobMid', 'mobEquipUnderSaddle',
    'saddleFront', 'mobEquipMid', 'tamingMobFront', 'mobEquipFront',
)
CHAR_ZRANK = {name: i for i, name in enumerate(CHAR_ZMAP_ORDER)}
CHAR_ZSLOT = {"body": CHAR_ZRANK["body"], "head": CHAR_ZRANK["head"],
              "hair": CHAR_ZRANK["hair"], "face": CHAR_ZRANK["face"],
              "cap": CHAR_ZRANK["cap"], "coat": CHAR_ZRANK["mailChest"],
              "longcoat": CHAR_ZRANK["mailChest"], "pants": CHAR_ZRANK["pants"],
              "shoes": CHAR_ZRANK["shoes"], "glove": CHAR_ZRANK["glove"],
              "cape": CHAR_ZRANK["cape"], "shield": CHAR_ZRANK["shield"],
              "weapon": CHAR_ZRANK["weapon"]}

_char_cache: dict = {"lists": {}}


def _char_nx() -> Path:
    ch = _nx_dir() / "Character.nx"
    if not ch.exists():
        hint = "国服数据源还没转换 Character.nx（工作区卡片点「转换」会一起生成）" \
            if _cn_mode() is not None else "先转换 Character.wz"
        raise CliError(f"没有 Character.nx —— {hint}", 1)
    return ch


def c_char_parts(cat: str = "hair") -> dict:
    """某个部位的全部可选素材：{cat, items:[{id,name}]}。"""
    cat = (cat or "hair").lower()
    if cat not in CHAR_CATS:
        raise CliError(f"未知部位 {cat}（可选：{'/'.join(CHAR_CATS)}）", 2)
    ch = _char_nx()
    key = f"{cat}:{ch.stat().st_mtime}"
    lists = _char_cache["lists"]
    if key not in lists:
        if len(lists) > 30:
            lists.clear()
        out = _run(["nxchar", "list", CHAR_CATS[cat]], ch.parent, timeout=300)
        i = out.find("{")
        items = json.loads(out[i:]).get("items", []) if i >= 0 else []
        for it in items:
            it["id"] = str(it.get("id", "")).replace(".img", "")
            if cat == "skin":
                s = int(it.get("skin", 0))
                if not it.get("name"):
                    it["name"] = CHAR_SKINS[s] if s < len(CHAR_SKINS) and CHAR_SKINS[s] else f"Skin {s}"
        lists[key] = items
    return {"cat": cat, "items": lists[key]}


def c_char_icon(cat: str, iid: str) -> Path:
    """素材列表的小缩略图（stand1 第 0 帧的第一张图），缓存到 .cache/char/icon/。"""
    cat = (cat or "hair").lower()
    if cat not in CHAR_CATS or cat == "skin":
        raise CliError(f"没有 {cat} 的缩略图", 2)
    ch = _char_nx()
    iid = re.sub(r"\D", "", iid)[:8]
    if not iid:
        raise CliError("id 不合法", 2)
    out = CACHE / "char" / "icon" / f"{cat}_{iid}.png"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        _run(["nxchar", "icon", CHAR_CATS[cat], iid, str(out)], ch.parent, timeout=120)
    if not out.exists():
        raise CliError(f"{cat} {iid} 没有图片", 1)
    return out


def _char_spec(spec: dict) -> dict:
    """前端选装 -> nxchar 参数（顺手把 id 洗成 8 位数字）。"""
    out = {"skin": int(spec.get("skin") or 0),
           "actions": [str(a) for a in (spec.get("actions") or ["stand1"]) if a]}
    for k in CHAR_SLOTS:
        out[k] = re.sub(r"\D", "", str(spec.get(k) or ""))[:8]
    if not out["actions"]:
        out["actions"] = ["stand1"]
    if out["longcoat"]:                      # 套服盖住上衣+裤裙（客户端 has_overall 行为）
        out["coat"] = ""
        out["pants"] = ""
    return out


def _char_layer_xy(L: dict, A: dict) -> tuple[int, int]:
    """一层图层在角色坐标系里的左上角（与客户端 Texture::shift 同一套锚点算法）。

    角色坐标系 = 身体图 body 部件的 origin 为 (0,0)；
    各部位按 map 里的锚点（navel/neck/brow/hand/handMove）对齐到身体/头/手的对应点。
    """
    mp = L.get("map") or {}
    ox, oy = int(L.get("ox") or 0), int(L.get("oy") or 0)
    slot, z = L.get("slot"), (L.get("z") or "")

    def ref(name: str) -> list:
        return A.get(name) or [0, 0]

    def vec(name: str) -> list:
        return mp.get(name) or [0, 0]

    if slot == "body":
        if z == "handBelowWeapon" or "handMove" in mp:
            r, a = ref("handMove"), vec("handMove")
        else:
            r, a = ref("navel"), vec("navel")
        return r[0] - a[0] - ox, r[1] - a[1] - oy

    if slot == "head":                       # 头：neck 对齐，再按 headNeck 摆
        r, hn = ref("neck"), ref("headNeck")
        return r[0] - hn[0] - ox, r[1] - hn[1] - oy

    if slot in ("hair", "face", "cap") or z.startswith("cap"):
        r = (ref("neck")[0] - ref("headNeck")[0] + ref("headBrow")[0],
             ref("neck")[1] - ref("headNeck")[1] + ref("headBrow")[1])
        a = vec("brow") if "brow" in mp else [0, 0]
        return r[0] - a[0] - ox, r[1] - a[1] - oy

    if slot in ("shield", "weapon"):         # 武器/盾：看它挂在 handMove / hand / navel
        if "handMove" in mp:
            r, a = ref("handMove"), mp["handMove"]
        elif "hand" in mp:
            r, a = ref("armHand"), mp["hand"]
        else:
            r, a = ref("navel"), vec("navel")
        return r[0] - a[0] - ox, r[1] - a[1] - oy

    if "navel" in mp:                        # 衣服类：都挂在腰（navel）上
        r, a = ref("navel"), mp["navel"]
        return r[0] - a[0] - ox, r[1] - a[1] - oy

    return -ox, -oy


CHAR_CLIMB = {"rope", "ladder"}          # 客户端 Stance::is_climbing

# 爬绳/爬梯时客户端 CharLook::draw 走的是完全另一条分支（背对镜头）：
#   body → gloves → shoes → pants → top/mail → cape → head → 头发背面 → 帽子 → 背盾 → 背武器
# 两个关键差异：**不画脸**（所以不该有眼睛）、**后发画在头之后**（盖住后脑）。
_CLIMB_RANK = (("body", 10), ("glove", 11), ("shoes", 12),
               ("pants", 15), ("top", 17), ("mail", 17), ("cape", 18),
               ("head", 20), ("cap", 22), ("hair", 24),
               ("shield", 26), ("weapon", 28))


def _climb_rank(z: str, slot: str) -> int:
    key = (z or slot or "").lower()
    for name, rank in _CLIMB_RANK:
        if name in key:
            return rank
    return 50


def _char_rank(L: dict, action: str = "") -> int:
    if action in CHAR_CLIMB:
        return _climb_rank(L.get("z") or "", L.get("slot") or "")
    # 未知 z 名兜底到最底层（0）而不是高层——盖住脸比藏在后面糟糕得多
    return CHAR_ZRANK.get(L.get("z") or "", CHAR_ZSLOT.get(L.get("slot"), 0))


CHAR_DUMP_V = 4                          # dump 结构/规则有变时+1，旧缓存自动失效
                                         # v3: nxchar 帧枚举改为数字名子节点（帧号不再要求从 0 开始）
                                         # v4: nxchar 脸没有 default 表情时回退其它表情（0005xxxx 170 张脸）


def _char_dump(spec: dict) -> tuple[dict, Path, str]:
    """跑 nxchar dump，拿到每一帧的图层表 + 锚点（结果按选装哈希缓存）。"""
    sp = _char_spec(spec)
    ws = _char_nx().parent
    raw = json.dumps({**sp, "actions": sorted(sp["actions"])}, sort_keys=True)
    key = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
    outdir = CACHE / "char" / key
    jf = outdir / "layers.json"
    if jf.exists():
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            if data.get("v") == CHAR_DUMP_V:      # 旧规则（缺脸层/错层序）的缓存直接重来
                return data, outdir, key
        except Exception:                     # noqa: BLE001
            pass

    outdir.mkdir(parents=True, exist_ok=True)
    args = ["nxchar", "dump", "--out", str(outdir), "--skin", str(sp["skin"]),
            "--actions", ",".join(sp["actions"])]
    for k in CHAR_SLOTS:
        if sp[k]:
            args += [f"--{k}", sp[k]]
    out = _run(args, ws, timeout=600)
    i = out.find("{")
    if i < 0:
        raise CliError("nxchar 没有输出（Character.nx 可能不完整）", 1)
    data = json.loads(out[i:])
    data["v"] = CHAR_DUMP_V
    jf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data, outdir, key


def c_char_render(spec: dict) -> dict:
    """把一套选装渲染成动画帧 PNG（1x 客户端像素），返回帧表 + 公共画布。"""
    from PIL import Image

    sp = _char_spec(spec)
    data, outdir, key = _char_dump(sp)
    frames = data.get("frames") or []
    if not frames:
        raise CliError("这套选装没有可渲染的帧（检查发型/身体 id 是否存在）", 1)

    plans = []
    for fr in frames:
        A = fr.get("anchors") or {}
        climbing = fr.get("action") in CHAR_CLIMB
        has_back_hair = any((L.get("z") or "") == "backHair" for L in fr.get("layers") or [])
        has_cap = any(L.get("slot") == "cap" for L in fr.get("layers") or [])
        layers = []
        for L in fr.get("layers") or []:
            if _is_noise(outdir / L["img"]):
                continue          # 国服加密噪点部件（无法还原）：整层不画，别把雪花贴到人身上
            if climbing and L.get("slot") == "face":
                continue                      # 背对镜头：脸不画（客户端 climb 分支没有 face）
            if (L.get("z") or "") == "hairOverHead" and has_cap:
                continue                      # 有帽子不画刘海（CharLook::draw 的 captype 分支只走 CAP）
            if climbing and has_back_hair and (L.get("z") or "") == "backHairBelowCap":
                continue                      # 无帽时只画整块后发，半盖版重叠会显双色边
            x, y = _char_layer_xy(L, A)
            layers.append((_char_rank(L, fr.get("action") or ""), x, y, outdir / L["img"], L))
        layers.sort(key=lambda t: t[0])       # 稳定排序：同层保持 dump 顺序
        body = next(((x, y, L) for _, x, y, _, L in layers
                     if L.get("slot") == "body"), None)
        plans.append((fr, layers, body))

    x0 = min(x for _, ls, _b in plans for _, x, _, _, _ in ls)
    y0 = min(y for _, ls, _b in plans for _, _, y, _, _ in ls)
    x1 = max(x + L["w"] for _, ls, _b in plans for _, x, _, _, L in ls)
    y1 = max(y + L["h"] for _, ls, _b in plans for _, _, y, _, L in ls)
    bw, bh = max(1, x1 - x0), max(1, y1 - y0)

    out = []
    for fr, layers, body in plans:
        # 脚底锚点：body 部件的 origin 在画布里的位置——客户端就是拿它对齐
        # 角色站位点的。合成画布是全动作的公共包围盒，没有这个锚点帧会悬空。
        if body:
            bx, by, BL = body
            ox = bx + int(BL.get("ox") or 0) - x0
            oy = by + int(BL.get("oy") or 0) - y0
        else:
            ox, oy = bw // 2, bh
        im = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        for _, x, y, png, _L in layers:
            try:
                with Image.open(png) as src:
                    im.alpha_composite(src.convert("RGBA"), (x - x0, y - y0))
            except Exception:                 # noqa: BLE001
                continue
        name = f"f_{fr['action']}_{fr['frame']}.png"
        im.save(outdir / name)
        out.append({"action": fr["action"], "frame": fr["frame"],
                    "delay": max(20, int(fr.get("delay") or 100)),
                    "url": f"/preview/char/{key}/{name}", "w": bw, "h": bh,
                    "ox": ox, "oy": oy})

    return {"key": key, "spec": sp, "box": [x0, y0, bw, bh],
            "frames": out, "dir": str(outdir)}


def c_char_export(spec: dict) -> dict:
    """导出这套选装的全部动画帧到 .exports/chars/<slug>/（附 manifest.csv）。"""
    res = c_char_render(spec)
    sp = res["spec"]
    slug = "s%d_%s_%s_%s_%s" % (sp["skin"], sp["hair"] or "nohair",
                                sp["face"] or "noface", sp["coat"] or "noco",
                                sp["pants"] or "nopants")
    outdir = EXPORTS / "chars" / re.sub(r"[^0-9A-Za-z_]+", "_", slug)
    if outdir.exists():
        drop_dir(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    src = Path(res["dir"])
    rows = ["action,frame,delay,file,w,h,ox,oy"]
    files = []
    for f in res["frames"]:
        p = src / Path(f["url"]).name
        if not p.exists():
            continue
        (outdir / p.name).write_bytes(p.read_bytes())
        files.append(p.name)
        rows.append(f"{f['action']},{f['frame']},{f['delay']},{p.name},"
                    f"{f['w']},{f['h']},{res['box'][0]},{res['box'][1]}")
    (outdir / "manifest.csv").write_text("\r\n".join(rows) + "\r\n", encoding="utf-8")
    (outdir / "look.json").write_text(json.dumps(sp, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    return {"dir": str(outdir), "count": len(files), "files": files,
            "box": res["box"], "spec": sp}


# ---------------------------------------------------------------- 技能特效

_skill_cache: dict = {"lists": {}}


def _skill_nx() -> Path:
    sk = _nx_dir() / "Skill.nx"
    if not sk.exists():
        hint = "国服数据源还没转换 Skill.nx（工作区卡片点「转换」会一起生成）" \
            if _cn_mode() is not None else "先转换 Skill.wz"
        raise CliError(f"没有 Skill.nx —— {hint}", 1)
    return sk


def _png_noise_flag(path: Path) -> bool | None:
    """判断一张特效帧 PNG 是不是国服加密位图解不出来的噪点。

    国服客户端对部分位图做了运行时解密：WZ 里的像素就是被打散的密文
    （alpha 完好、RGB 逐像素随机），wz2nx/nxskill 拿到什么就导什么，
    没有客户端密钥还原不了——只能识别出来剔除，别把雪花图摆上台面。
    判据（用 6659 张真图案 + 已知密文标定，2026-11-09）：
    真图案有大量「相邻像素几乎相同」的平坦区（p5=0.243），密文几乎没有
    （max≈0.11）；密文的颜色重复率显著更低（uniq 0.42~0.56），而 flat 低
    的真图案（描边/反锯齿小部件）uniq 都 <0.39。双条件同时满足才判噪点：
    flat >= 0.15 → 真图案；flat < 0.15 且 uniq > 0.40 → 噪点。
    注意 alpha 通道不被加密，噪点帧的轮廓/射线都还在（缩小看像灰雾、
    1:1 看才是彩色雪花），别被缩略图骗了。
    两类已证实误杀场景（v3 修正，2026-11-10）：
    1. 小物件（红伞蘑菇 15×24，op 229px）：跨过小图守卫后，抗锯齿边缘
       拉低 flat、渐变色拉高 uniq，被判噪。守卫提到 300px。
    2. 死亡残影帧（alpha 大多 40~85 的渐隐轮廓+尘土粒子，r128=0.18~0.22）：
       灰白渐变+粒子同样触发双条件。加渐隐守卫：实像素（alpha>128）占比
       <0.30 时不表态——密文帧的 alpha 来自原画，主体都是实心的
       （实测密文 0.63~0.68、真怪 0.83）。"""
    import numpy as np
    from PIL import Image

    try:
        with Image.open(path) as im:
            a = np.asarray(im.convert("RGBA"), dtype=np.int32)
    except Exception:                     # noqa: BLE001
        return None                       # 读不了图不表态
    op = a[:, :, 3] > 40
    n_op = int(op.sum())
    if n_op < 300:
        return None                       # 像素太少判断不了，保留
    if int((a[:, :, 3] > 128).sum()) / n_op < 0.30:
        return None                       # 渐隐残影帧（死亡淡出/尘土），保留
    rgb = a[:, :, :3]
    m1 = op[:, 1:] & op[:, :-1]
    m2 = op[1:, :] & op[:-1, :]
    n = int(m1.sum() + m2.sum())
    if n < 100:
        return None
    d1 = np.abs(rgb[:, 1:, :] - rgb[:, :-1, :]).max(axis=2)
    d2 = np.abs(rgb[1:, :, :] - rgb[:-1, :, :]).max(axis=2)
    flat = (int(((d1 < 8) & m1).sum()) + int(((d2 < 8) & m2).sum())) / n
    if flat >= 0.15:                      # 平坦区够多 = 真图案
        return False
    key = rgb[op][:, 0] * 65536 + rgb[op][:, 1] * 256 + rgb[op][:, 2]
    if len(np.unique(key)) / int(op.sum()) <= 0.40:  # 颜色大量重复 = 真图案
        return False
    return True


_NOISE_MEMO: dict[str, bool] = {}


def _is_noise(path: Path) -> bool:
    """_png_noise_flag 的记忆化版（None/读不了都按「不噪」处理）。

    同一张图在怪物/人物/技能多条链路反复遇到，按 路径+mtime 记结论，
    换缓存重导后自动失效。"""
    try:
        key = f"{path}|{path.stat().st_mtime_ns}"
    except OSError:
        return False
    v = _NOISE_MEMO.get(key)
    if v is None:
        v = _png_noise_flag(path) is True
        if len(_NOISE_MEMO) > 4096:
            _NOISE_MEMO.clear()
        _NOISE_MEMO[key] = v
    return v


def _strip_noise_anims(anims: dict) -> dict:
    """剔除动画帧表里的加密噪点帧；整条动作全噪就把这个动作去掉。

    国服对部分位图做运行时解密，WZ 里的像素就是密文（alpha 完好、RGB
    逐像素随机），没有密钥还原不了——识别出来不上台面。怪物链接路
    （图标/战斗页/地图合成/素材预览）统一走这里。"""
    out = {}
    for act, frs in anims.items():
        good = [fr for fr in frs if not _is_noise(fr[0])]
        if good:
            out[act] = good
    return out


def c_skill_jobs() -> dict:
    """技能职业卷列表：{jobs:[{id,skills}]}（按 mtime 缓存）。"""
    sk = _skill_nx()
    key = f"jobs:{sk.stat().st_mtime}"
    if key not in _skill_cache["lists"]:
        out = _run(["nxskill", "jobs"], sk.parent, timeout=300)
        i = out.find("{")
        data = json.loads(out[i:]) if i >= 0 else {"jobs": []}
        _skill_cache["lists"][key] = data["jobs"]
        if len(_skill_cache["lists"]) > 60:
            _skill_cache["lists"].clear()
            _skill_cache["lists"][key] = data["jobs"]
    return {"jobs": _skill_cache["lists"][key]}


def c_skill_list(job: str) -> dict:
    """一个职业卷下的全部技能：{items:[{id,name,icon,effects:[...]}]}。"""
    sk = _skill_nx()
    job = re.sub(r"\D", "", str(job))[:6]
    if not job:
        raise CliError("职业卷 id 不合法", 2)
    key = f"{job}:{sk.stat().st_mtime}"
    if key not in _skill_cache["lists"]:
        out = _run(["nxskill", "list", job], sk.parent, timeout=300)
        i = out.find("{")
        data = json.loads(out[i:]) if i >= 0 else {"items": []}
        _skill_cache["lists"][key] = data["items"]
        if len(_skill_cache["lists"]) > 60:
            _skill_cache["lists"].clear()
            _skill_cache["lists"][key] = data["items"]
    return {"job": job, "items": _skill_cache["lists"][key]}


def c_skill_export(job: str, skill: str) -> dict:
    """导出一个技能的全部特效序列（帧 PNG + manifest.json），缓存到 .cache/skill/。"""
    sk = _skill_nx()
    job = re.sub(r"\D", "", str(job))[:6]
    skill = re.sub(r"\D", "", str(skill))[:12]
    if not job or not skill:
        raise CliError("技能 id 不合法", 2)
    outdir = CACHE / "skill" / job / skill
    if not (outdir / "manifest.json").exists():
        if outdir.exists():
            drop_dir(outdir)
        outdir.parent.mkdir(parents=True, exist_ok=True)
        _run(["nxskill", "export", "--job", job, "--skill", skill,
              "--out", str(outdir)], sk.parent, timeout=600)
    mf = outdir / "manifest.json"
    if not mf.exists():
        raise CliError("这个技能没有任何特效帧", 1)
    man = json.loads(mf.read_text(encoding="utf-8"))
    # 剔除国服加密位图的噪点帧（判定结果缓存到 _noise.json，manifest 没变不重算；
    # v3 = 判据修正版（小物件/渐隐帧守卫），旧结论全部作废重算）
    bad: set[str] = set()
    verdict_p = outdir / "_noise.json"
    stamp = mf.stat().st_mtime
    try:
        saved = json.loads(verdict_p.read_text(encoding="utf-8"))
        if saved.get("mtime") == stamp and saved.get("v") == 3:
            bad = set(saved.get("bad") or [])
    except Exception:                     # noqa: BLE001
        saved = None
    if saved is None:
        bad = {fr["img"]
               for seq in man.get("seqs") or []
               for fr in seq.get("frames") or []
               if (outdir / fr["img"]).exists() and _png_noise_flag(outdir / fr["img"])}
        verdict_p.write_text(json.dumps({"v": 3, "mtime": stamp, "bad": sorted(bad)}),
                             encoding="utf-8")
    seqs = []
    for seq in man.get("seqs") or []:
        frs = seq.get("frames") or []
        good = [fr for fr in frs if fr["img"] not in bad]
        if not good:
            continue
        if len(good) * 3 < len(frs) * 2:
            continue                        # 噪点超过 1/3：整条序列缺帧跳闪，干脆丢掉
        seqs.append({**seq, "frames": good})
    man = {**man, "seqs": seqs}
    for seq in man.get("seqs") or []:
        for fr in seq.get("frames") or []:
            # 国服 Skill.nx 的帧节点没有 origin，nxskill 写出的 ax/ay 全是
            # (0,0)（= 贴图左上角）。按中心兜底：命中/施法特效绕锚点环绕更自然。
            if not fr.get("ax") and not fr.get("ay"):
                fr["ax"], fr["ay"] = (fr.get("w") or 0) // 2, (fr.get("h") or 0) // 2
            fr["url"] = "/preview/skill/" + job + "/" + skill + "/" + fr["img"]
    for i, ic in enumerate(man.get("icons") or []):
        man["icons"][i] = "/preview/skill/" + job + "/" + skill + "/" + ic
    return {"job": job, "id": skill, "dir": str(outdir), "manifest": man}


def c_skill_export_to(job: str, skill: str) -> dict:
    """把一个技能的全部特效（帧 PNG + manifest.json）导出到 .exports/skills/。"""
    res = c_skill_export(job, skill)
    outdir = EXPORTS / "skills" / res["job"] / res["id"]
    if outdir.exists():
        drop_dir(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    files = []
    for p in sorted(Path(res["dir"]).iterdir()):
        if p.is_file():
            if p.suffix == ".png" and _is_noise(p):
                continue          # 加密噪点帧不进导出包（判定复用 _noise.json 结论）
            (outdir / p.name).write_bytes(p.read_bytes())
            files.append(p.name)
    return {"dir": str(outdir), "count": len(files), "files": files}


# 伤害数字库（Effect/BasicEff.img 里的 No* 数字集） ----------------------------

DMG_DIR = CACHE / "dmg"
DMG_SETS = DMG_DIR / "sets.json"

_DMG_CN = {
    "NoRed0": "普通伤害（红，默认；带 Miss/防御/抵抗）", "NoRed1": "红色样式 1",
    "NoRed2": "红色样式 2", "NoRed3": "红色样式 3",
    "NoBlue0": "蓝色样式 0", "NoBlue1": "蓝色样式 1",
    "NoGreen0": "绿色样式 0", "NoGreen1": "绿色样式 1",
    "NoViolet0": "紫色样式 0（带 Miss/防御）", "NoViolet1": "紫色样式 1",
    "NoCri0": "暴击样式 0", "NoCri1": "暴击样式 1", "NoCri2": "暴击样式 2",
    "NoCri3": "暴击样式 3", "NoBattleH": "战斗力数字", "NoNaval0": "海战数字（带逗号）",
    "EffNumber": "特效数字", "AquaPointNumber": "水点数",
}


class _Nx:
    """只读 NoLifeNx 格式：mmap 节点表 / 字符串表 / 位图表（LZ4 → BGRA → PNG）。

    伤害数字库专用的小读器（原生工具都是整 img 级导出，取几个 No* 子树用
    Python 直接读更省事）。节点 20 字节：name(u32) children(u32) num(u16)
    type(u16) + 8 字节值联合体。"""

    def __init__(self, path: Path):
        import mmap as _mmap
        self._f = open(path, "rb")
        self._mm = _mmap.mmap(self._f.fileno(), 0, access=_mmap.ACCESS_READ)
        if self._mm[:4] != b"PKG4":
            raise CliError(f"{path.name} 不是 NX 文件", 1)
        self._no, = struct.unpack_from("<Q", self._mm, 8)
        self._so, = struct.unpack_from("<Q", self._mm, 20)
        self._bo, = struct.unpack_from("<Q", self._mm, 32)

    def close(self) -> None:
        """必须显式关：mmap 占着的文件在 Windows 上没法 rename/删（drop_dir 会静默失败）。"""
        try:
            self._mm.close()
            self._f.close()
        except BaseException:
            pass

    def _str(self, i: int) -> str:
        pos, = struct.unpack_from("<Q", self._mm, self._so + i * 8)
        ln, = struct.unpack_from("<H", self._mm, pos)
        return self._mm[pos + 2:pos + 2 + ln].decode("utf-8", "replace")

    def node(self, i: int):
        """→ (name, first_child, count, type, value)；value 依类型：int/float/str/
        (x,y) 向量 / (index,w,h) 位图。"""
        o = self._no + i * 20
        name, first = struct.unpack_from("<2I", self._mm, o)
        num, ty = struct.unpack_from("<2H", self._mm, o + 8)
        v = None
        if ty == 1:
            v = struct.unpack_from("<q", self._mm, o + 12)[0]
        elif ty == 2:
            v = struct.unpack_from("<d", self._mm, o + 12)[0]
        elif ty == 3:
            v = self._str(struct.unpack_from("<I", self._mm, o + 12)[0])
        elif ty == 4:
            v = struct.unpack_from("<2i", self._mm, o + 12)
        elif ty == 5:
            v = struct.unpack_from("<IHH", self._mm, o + 12)
        return self._str(name), first, num, ty, v

    def child(self, i: int, key: str) -> int | None:
        _, first, num, _, _ = self.node(i)
        for k in range(num):
            ci = first + k
            if self.node(ci)[0] == key:
                return ci
        return None

    def find(self, path: str) -> int | None:
        cur = 0
        for part in path.split("/"):
            cur = self.child(cur, part)
            if cur is None:
                return None
        return cur

    def png(self, bmp) -> bytes:
        """位图 (index,w,h) → PNG bytes。NX 位图数据是 LZ4 块压缩的 BGRA。"""
        import io
        import lz4.block
        from PIL import Image
        idx, w, h = bmp
        pos, = struct.unpack_from("<Q", self._mm, self._bo + idx * 8)
        ln, = struct.unpack_from("<I", self._mm, pos)
        raw = lz4.block.decompress(self._mm[pos + 4:pos + 4 + ln],
                                   uncompressed_size=w * h * 4)
        img = Image.frombytes("RGBA", (w, h), bytes(raw))     # 字节序实际是 BGRA
        r, g, b, a = img.split()
        buf = io.BytesIO()
        Image.merge("RGBA", (b, g, r, a)).save(buf, "PNG")
        return buf.getvalue()


def _dmg_effect_dir() -> Path | None:
    """国服数据的 Effect 卷（伤害数字在 Effect/BasicEff.img 里）。"""
    data = _cn_mode()
    if data is None:
        return None
    eff = data / "Effect"
    return eff if (eff / "Effect_000.wz").is_file() else None


def _dmg_ready() -> bool:
    return DMG_SETS.is_file()


def _dmg_state() -> dict:
    eff = _dmg_effect_dir()
    n = 0
    if eff is not None and _dmg_ready():
        try:
            n = len(json.loads(DMG_SETS.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            n = 0
    return {"dir": str(eff) if eff else None, "ready": _dmg_ready(), "count": n}


def _dmg_build(log=None, stage=None) -> None:
    """构建伤害数字库：wz2nx 只转 BasicEff.img → Python 读 No* 子树 → PNG 缓存。

    中间产物 _src/（Effect 的 BasicEff 单 img，实测约 900 MB、十几秒）提取完
    即删，留在 .cache\\dmg\\ 的只有几 MB 的 PNG + sets.json。数字集判定：名字
    以 No 开头（或已知数字表）且 0-9 十个数字都能取到位图。"""
    eff = _dmg_effect_dir()
    if eff is None:
        raise CliError("找不到国服 Effect 数据（工作区不是国服官方客户端？）", 1)
    log = log or (lambda s: None)
    stage = stage or (lambda s: None)
    _need_tools()
    if DMG_DIR.exists():
        drop_dir(DMG_DIR)
    src = DMG_DIR / "_src"
    src.mkdir(parents=True, exist_ok=True)
    nx = None
    try:
        stage("转换 Effect/BasicEff.img（只这一个 img，几分钟）")
        _cn_wz2nx(_cn_workdir(eff.parent), "Effect/Effect_000.wz", str(src / "e.nx"),
                  [("_Canvas", "Effect/_Canvas/_Canvas_000.wz")], "", "BasicEff.img",
                  log)
        stage("提取伤害数字帧")
        nx = _Nx(src / "e.nx")
        basic = nx.find("BasicEff.img")
        if basic is None:
            raise CliError("BasicEff.img 不在转换产物里", 1)
        _, first, num, _, _ = nx.node(basic)
        sets = []
        for k in range(num):
            si = first + k
            nm, sf, sn, _, _ = nx.node(si)
            # 伤害数字集 = 名字以 No 开头（NoRed/NoCri/NoBlue…）或已知数字表；
            # BasicEff 里 JobChanged 之类普通特效也是 0..N 编号帧，不能只按编号认
            if not (nm.startswith("No") or nm in ("EffNumber", "AquaPointNumber")):
                continue
            digits = [nx.child(si, d) for d in "0123456789"]

            def frame_of(i: int):
                """节点 → 位图：直接是位图就取自己；是只包一张位图的文件夹
                （EffNumber/3/0 这种）就取里面那张。"""
                _, ff, fc, ft, _ = nx.node(i)
                if ft == 5:
                    return i
                bmps = [ff + j for j in range(fc) if nx.node(ff + j)[3] == 5]
                return bmps[0] if len(bmps) == 1 else None

            if any(d is None or frame_of(d) is None for d in digits):
                continue                      # 编号不像数字（防御：No 开头的别的特效）
            sdir = DMG_DIR / nm
            sdir.mkdir(parents=True, exist_ok=True)
            frames = []
            for j in range(sn):
                fi = sf + j
                fn, _, _, fty, _ = nx.node(fi)
                if not fn:
                    continue
                bi = fi if fty == 5 else frame_of(fi)
                if bi is None:
                    continue                  # 多帧文件夹等非数字内容，先不展开
                _, _, _, _, fv = nx.node(bi)
                org = nx.child(bi, "origin")
                ox, oy = nx.node(org)[4] if org is not None and nx.node(org)[3] == 4 \
                    else (0, 0)
                safe = fn.replace("/", "_")
                (sdir / f"{safe}.png").write_bytes(nx.png(fv))
                frames.append({"k": fn, "w": fv[1], "h": fv[2],
                               "ox": ox, "oy": oy,
                               "url": f"/preview/dmg/{nm}/{safe}.png"})
            sets.append({"id": nm, "cn": _DMG_CN.get(nm, ""), "frames": frames})
        if not sets:
            raise CliError("BasicEff.img 里没找到数字集（数据格式变了？）", 1)
        DMG_SETS.write_text(json.dumps(sets, ensure_ascii=False), encoding="utf-8")
        log(f"  伤害数字库：{len(sets)} 套，共 "
            f"{sum(len(s['frames']) for s in sets)} 帧")
    finally:
        if nx is not None:
            nx.close()          # mmap 不关，Windows 上 _src 没法 rename（drop_dir 会静默失败）
        drop_dir(src)


def c_dmg_sets() -> dict:
    """伤害数字集列表（WebUI 数字面板数据源）。"""
    if not _dmg_ready():
        eff = _dmg_effect_dir()
        raise CliError("伤害数字库未建" + ("（点「⬇ 转换缺失数据」会自动建）"
                                        if eff else "（需要国服官方数据源）"), 1)
    return {"items": json.loads(DMG_SETS.read_text(encoding="utf-8"))}


def c_dmg_export_to(set_id: str) -> dict:
    """把一套伤害数字的全部 PNG + sets 说明导出到 .exports/dmg/。"""
    if not _dmg_ready():
        raise CliError("伤害数字库未建", 1)
    sets = json.loads(DMG_SETS.read_text(encoding="utf-8"))
    hit = next((s for s in sets if s["id"] == set_id), None)
    if hit is None:
        raise CliError(f"没有这套数字：{set_id}", 1)
    outdir = EXPORTS / "dmg" / set_id
    if outdir.exists():
        drop_dir(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    files = []
    for p in sorted((DMG_DIR / set_id).iterdir()):
        if p.is_file() and p.suffix == ".png":
            (outdir / p.name).write_bytes(p.read_bytes())
            files.append(p.name)
    (outdir / "manifest.json").write_text(
        json.dumps(hit, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"dir": str(outdir), "count": len(files)}


def _composite_mobs(ws: Path, outdir: Path, mj: dict, base_png: Path, seed: str,
                   density: int = 1, no_bg: bool = False):
    """把这张图的怪随机贴到地面上，产出 preview_mobs.png（脚底落在 foothold 地面）。

    返回 (png路径, 摆放列表)：places 每项 {kind, x, y, w, h} 是该怪在预览图上的
    精确像素框（x,y=左上角）。

    大 BOSS（tier=big）一律不参与合成（只刷大 BOSS 的图会得到空结果），
    高阶 BOSS / 普通怪照常出现。

    每个怪在「随机游走区间」内取随机 x：优先用 life 自带 rx0~rx1（客户端巡逻区间），
    没有就按图宽 ±8% 兜底；y 由 foothold 段线性插值得出（同一段里取离出生地面最近
    的，保证贴地不悬空），落点悬空时退回出生点附近。动作状态也随机：按 ACT_WEIGHTS
    加权抽动作（站/走/飞多、攻击次之、受击/死亡/跳跃少），动作内随机抽帧，所有怪
    50% 随机镜像朝向，随机结果按 seed 稳定（同 seed 同布局）。
    """
    from PIL import Image
    life = [l for l in mj.get("life", []) if l.get("type") == "m"]
    if life:
        mobnx = _nx_dir() / "Mob.nx"
        meta = {m["id"]: m for m in mob_meta(mobnx)} if mobnx.exists() else {}
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
    places = []                          # 摆放记录（每只怪的像素框）

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


def _composite_drops(outdir: Path, mj: dict, base_png: Path, seed: str,
                     no_bg: bool = False):
    """在合成图（通常已带怪）上再撒一层**关联掉落物**。

    规则（挂机训练数据的真实性要求）：
    - 只掉**本图 life 里有的怪**能掉的东西（vendor/drops.json，服务端掉落表），
      绝不出现这张图不可能掉的道具；
    - 掉落点跟着对应怪的巡逻区间（rx0~rx1）走——怪在哪刷，东西掉在哪，
      foothold 落地不悬空；
    - 每个刷怪点 55% 概率有一堆（挂机一段时间后的地面状态），每堆 =
      0~1 个金币（80%）+ 1~3 件道具（按 chance 加权抽，图标缺的跳过）；
      金币档位按客户端 MapDrops 规则（>49 金 / >99 捆 / >999 袋）随机金额映射。
    返回 (out_png, places)；places 是每件掉落的像素框（YOLO 标注直接用）。
    """
    from PIL import Image
    life = [l for l in mj.get("life", []) if l.get("type") == "m"]
    if not life or not CLS_ICONS.is_dir():
        return None, []
    drops = _drops()
    rng = random.Random(f"{seed}:drops")
    im = Image.open(base_png).convert("RGBA")
    minx = miny = None
    # 与渲染一致的画布原点：读预览旁的 .meta 太绕，直接用 map_bounds+pad 重算
    R = _render_mod()
    bx, by, bw, bh = R.map_bounds(mj)
    minx, miny = bx - 40, by - 40
    fhs = [(float(f["x1"]), float(f["y1"]), float(f["x2"]), float(f["y2"]))
           for f in mj.get("foothold", [])]

    def _ground(x: float, y_ref: float) -> float | None:
        best_d = best_y = None
        for x1, y1, x2, y2 in fhs:
            lo, hi = (x1, x2) if x1 <= x2 else (x2, x1)
            if hi > lo and lo <= x <= hi:
                y = y1 + (y2 - y1) * (x - x1) / (x2 - x1)
                d = abs(y - y_ref)
                if best_d is None or d < best_d:
                    best_d, best_y = d, y
        return best_y

    meso_tiers = [0, 1, 2, 3]
    icon_cache: dict[int, Image.Image] = {}
    places: list[dict] = []

    def _icon(itemid: int) -> Image.Image | None:
        if itemid not in icon_cache:
            p = CLS_ICONS / f"{itemid:08d}.png"
            icon_cache[itemid] = Image.open(p).convert("RGBA") if p.is_file() else None
        return icon_cache[itemid]

    def _place(img: Image.Image, x: float, gy: float, itemid: int):
        px = round(x - minx - img.width / 2)
        py = round(gy - miny - img.height)
        im.alpha_composite(img, (px, py))
        places.append({"item": itemid, "x": px, "y": py,
                       "w": img.width, "h": img.height})

    for l in life:
        mob = int(str(l.get("id", "")).lstrip("0") or 0)
        table = drops.get(mob)
        if not table or rng.random() > 0.55:
            continue
        items = [e for e in table
                 if e["i"] and (CLS_ICONS / f"{e['i']:08d}.png").is_file()]
        if not items:
            continue
        bx0 = float(l.get("x", 0))
        # y 取出生参考、fh 指向的地面 y（cy 键可能存在但值 None，判空同 life_points）
        base_y = (float(l["cy"]) if l.get("fh") and l.get("cy") is not None
                  else float(l.get("y") or 0))
        rx0, rx1 = l.get("rx0"), l.get("rx1")
        if rx0 is not None and rx1 is not None and float(rx1) > float(rx0):
            lo, hi = float(rx0), float(rx1)
        else:
            lo, hi = bx0 - 120, bx0 + 120
        cx = rng.uniform(lo, hi)
        gy0 = _ground(cx, base_y) or base_y
        # 金币（80%）：金额 → 档位（客户端同款阈值）
        if rng.random() < 0.8 and meso_tiers:
            amount = int(10 ** rng.uniform(0.8, 4.5))     # 6 ~ ~3 万
            tier = 3 if amount > 999 else 2 if amount > 99 else 1 if amount > 49 else 0
            fr = rng.randrange(4)
            p = CLS_ICONS / f"meso_{tier}_{fr}.png"
            if p.is_file():
                coin = Image.open(p).convert("RGBA")
                _place(coin, cx + rng.uniform(-8, 8), gy0, -tier - 1)
        # 道具 1~3 件（chance 加权）
        tot = sum(e["c"] for e in items) or 1
        for _ in range(rng.randint(1, 3)):
            r = rng.random() * tot
            e = items[-1]
            for cand in items:
                r -= cand["c"]
                if r <= 0:
                    e = cand
                    break
            ic = _icon(e["i"])
            if ic is None:
                continue
            x = cx + rng.uniform(-30, 30)
            gy = _ground(x, base_y)
            if gy is None:
                gy = gy0
            _place(ic, x, gy, e["i"])
    if not places:
        return None, []
    out = outdir / ("preview_drops_nobg.png" if no_bg else "preview_drops.png")
    (im if no_bg else im.convert("RGB")).save(out)
    return out, places


# ── 随机玩家（页面「显示玩家」叠加层 + 导出合成）────────────────────────────
# 外观/名字一套生成器，两种展示共用：页面叠加层用 /api/char/players 的
# 随机批（前端随机摆放），导出合成用 seed 稳定的同款规则。

PLAYER_NAME_PRE = ["勇敢的", "可爱的", "神秘的", "孤独的", "暴走的", "摸鱼的",
                   "爆肝的", "无敌的", "快乐的", "温柔的", "飞快的", "认真的"]
PLAYER_NAME_SUF = ["蘑菇仔", "小射手", "战士", "小法师", "飞侠", "海盗",
                   "冒险家", "枫叶仔", "小萌新", "弓箭手"]
# 肤色加权：Light 最常见，Tan/Dark 次之，特殊肤色稀有
PLAYER_SKINS = [(0, 6), (1, 3), (2, 2), (3, 1), (9, 1), (10, 1)]
# 各装备槽的穿戴概率（武器大件/帽子/披风等随机有或没有，光板也真实）
PLAYER_LOOK_P = {"cap": 0.35, "coat": 0.75, "pants": 0.65, "longcoat": 0.12,
                 "shoes": 0.9, "glove": 0.3, "cape": 0.25, "shield": 0.18,
                 "weapon": 0.75}
_PLAYER_POOL_LOCK = threading.Lock()
_PLAYER_POOL: dict[str, list] = {}


def _player_pool(cat: str) -> list:
    """部位素材全表（带缓存；没转 Character.nx 等异常时给空表）。"""
    with _PLAYER_POOL_LOCK:
        if cat not in _PLAYER_POOL:
            try:
                _PLAYER_POOL[cat] = c_char_parts(cat)["items"]
            except Exception:                  # noqa: BLE001
                _PLAYER_POOL[cat] = []
        return _PLAYER_POOL[cat]


def _rand_player_look(rng: random.Random) -> dict:
    """随机外观 + 随机名字。返回 {spec, name}；spec 键 = _char_spec 认的那套。"""
    tot = sum(w for _, w in PLAYER_SKINS)
    r = rng.random() * tot
    skin = PLAYER_SKINS[-1][0]
    for s, w in PLAYER_SKINS:
        r -= w
        if r <= 0:
            skin = s
            break
    spec = {"skin": skin}
    for cat in CHAR_SLOTS:
        pool = _player_pool(cat)
        if cat in ("hair", "face"):
            spec[cat] = rng.choice(pool)["id"] if pool else ""
        else:
            spec[cat] = rng.choice(pool)["id"] if pool and rng.random() < PLAYER_LOOK_P[cat] else ""
    if spec["longcoat"]:                       # 套服盖住上衣+裤裙（_char_spec 同款规则）
        spec["coat"] = ""
        spec["pants"] = ""
    return {"spec": spec, "name": rng.choice(PLAYER_NAME_PRE) + rng.choice(PLAYER_NAME_SUF)}


def _player_stand(spec: dict, rng: random.Random | None = None) -> dict | None:
    """渲染一套选装的 stand1 帧，随机挑一帧返回 {path, w, h, ox, oy}。

    c_char_render 结果按选装哈希落 .cache/char/<key>/（多玩家/多张图复用）。
    """
    res = c_char_render({**spec, "actions": ["stand1"]})
    frs = [f for f in res.get("frames", []) if f.get("action") == "stand1"]
    if not frs:
        return None
    fr = rng.choice(frs) if rng else frs[0]
    return {"path": Path(res["dir"]) / f"f_{fr['action']}_{fr['frame']}.png",
            "url": fr["url"], "w": fr["w"], "h": fr["h"], "ox": fr["ox"], "oy": fr["oy"]}


def c_char_players() -> dict:
    """一批随机玩家（页面「显示玩家」叠加层用）：8 个外观 + 名字 + 站立帧。"""
    _need_ws()
    rng = random.Random(random.randrange(1 << 30))
    out = []
    for _ in range(8):
        look = _rand_player_look(rng)
        try:
            fr = _player_stand(look["spec"])
        except CliError:
            fr = None
        if fr is None:
            continue
        out.append({"name": look["name"],
                    "spec": {k: v for k, v in look["spec"].items() if v},
                    # path 是本地磁盘路径（合成内部用），不发给前端
                    "frame": {k: v for k, v in fr.items() if k != "path"}})
    if not out:
        raise CliError("随机玩家生成失败：先转换 Character.nx（人物素材页签能用就行）", 1)
    return {"players": out}


def _composite_players(outdir: Path, mj: dict, base_png: Path, seed: str,
                       no_bg: bool = False):
    """把 1~5 个随机玩家贴到地面上，产出 preview_players.png。

    外观/名字/站位全部由 seed 决定（同 seed 同布局，与 _composite_mobs 同款）：
    随机外观（肤色/发型/脸型/装备穿戴概率）+ 随机名字，随机 foothold 段内随机
    x 落地，50% 随机镜像；名字画在脚底正下方（客户端 Char.cpp:68
    namelabel.draw(absp+(0,-4))、字形向下展开 → 名字落在角色脚下），带
    NAMETAG 黑 60% 半透明圆角背板（GraphicsGL.cpp:976）、白字居中。
    返回 (png路径, places)，places 带名字与像素框。
    """
    from PIL import Image, ImageDraw, ImageFont
    fhs = [(float(f["x1"]), float(f["y1"]), float(f["x2"]), float(f["y2"]))
           for f in mj.get("foothold", [])]
    if not fhs:
        return None, []
    R = _render_mod()
    bx, by, bw, bh = R.map_bounds(mj)
    minx, miny = bx - 40, by - 40
    im = Image.open(base_png).convert("RGBA")
    rng = random.Random(f"{seed}:players")
    try:                                       # 客户端字体是 12px 宋体位图字
        f = ImageFont.truetype("C:/Windows/Fonts/simsun.ttc", 12)
    except Exception:                          # noqa: BLE001
        f = None
    places: list[dict] = []
    tags: list[tuple[int, int, str]] = []
    for _ in range(rng.randint(1, 5)):
        look = _rand_player_look(rng)
        try:
            fr = _player_stand(look["spec"], rng)
        except Exception:                      # noqa: BLE001
            continue
        if fr is None:
            continue
        img = Image.open(fr["path"]).convert("RGBA")
        flip = rng.random() < 0.5
        if flip:                               # 镜像后锚点 = 帧宽 - 原锚点
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
            ox = img.width - fr["ox"]
        else:
            ox = fr["ox"]
        x1, y1, x2, y2 = rng.choice(fhs)
        lo, hi = (x1, x2) if x1 <= x2 else (x2, x1)
        if hi <= lo:
            continue
        x = rng.uniform(lo, hi)
        gy = y1 + (y2 - y1) * (x - x1) / (x2 - x1)
        px = round(x - minx - ox)
        py = round(gy - miny - fr["oy"])
        if px < -img.width or py < -img.height \
                or px > im.width + img.width or py > im.height + img.height:
            continue
        im.alpha_composite(img, (px, py))
        # 客户端字形占 [脚上2px, 脚下9px]、背板占脚下 2~13px（GraphicsGL
        # drawtext 的 NAMETAG 分支）：名字在正下方、轻微压住鞋底
        tags.append((px + img.width // 2, py + img.height - 2, look["name"]))
        places.append({"name": look["name"], "x": px, "y": py,
                       "w": img.width, "h": img.height})
    if not places:
        return None, []
    if f is not None and tags:
        # 客户端 NAMETAG 背板 = 黑 60% 半透明圆角小条（GraphicsGL.cpp:976）。
        # ImageDraw 直接写像素不混合，背板和文字都画在独立图层上再一次性
        # alpha_composite（背板要能透出下面的地面，文字不能被背板压暗）
        tag_layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
        td = ImageDraw.Draw(tag_layer)
        for tx, ty, nm in tags:
            bb = td.textbbox((tx, ty), nm, font=f, anchor="ma")
            td.rounded_rectangle((bb[0] - 4, bb[1] - 1, bb[2] + 4, bb[3] + 1),
                                 radius=3, fill=(0, 0, 0, 153))
            td.text((tx, ty), nm, font=f, fill=(255, 255, 255, 255),
                    stroke_width=1, stroke_fill=(0, 0, 0, 255), anchor="ma")
        im.alpha_composite(tag_layer)
    out = outdir / ("preview_players_nobg.png" if no_bg else "preview_players.png")
    (im if no_bg else im.convert("RGB")).save(out)
    return out, places


def _map_cache_fresh(outdir: Path) -> bool:
    """导出缓存是否还能用：map.json 可解析且带 miniMap（旧版 nxmap 不导小地图），
    且数据源戳一致（换了数据源——比如从 v0.83 换国服——旧缓存全部视为过期）。"""
    try:
        mj = json.loads((outdir / "map.json").read_text(encoding="utf-8"))
        if "miniMap" not in mj:
            return False
        stamp = outdir / ".srcstamp"
        return stamp.read_text().strip() == str((_nx_dir() / "Map.nx").stat().st_mtime)
    except Exception:
        return False


def _map_cache_stamp(outdir: Path) -> None:
    try:
        (outdir / ".srcstamp").write_text(
            str((_nx_dir() / "Map.nx").stat().st_mtime), encoding="utf-8")
    except OSError:
        pass


def _res_url(p: Path) -> str:
    """缓存/导出目录里的文件 → 静态路由 URL。怀旧服大图库在 .cache\\wzjs-map
    下（窗口导出也写在那），统一按 /preview/ 走，和 preview 模式一致。"""
    try:
        return "/preview/" + p.relative_to(CACHE).as_posix()
    except ValueError:
        return "/exports/" + p.relative_to(EXPORTS).as_posix()


def c_map_export(mid: str, preview: bool = False, mobs: bool = False,
                 density: int = 1, view: tuple | None = None,
                 seed: str | None = None,
                 _visited: set | None = None,
                 no_bg: bool = False,
                 cls: bool = False,
                 drops: bool = False,
                 players: bool = False) -> dict:
    """导出一张地图：map.json（life/foothold/ladderRope）+ 全部贴图 + 渲染预览图。

    cls=True 走怀旧服 WZJS 大图库（.cache\\wzjs-map\\<id>\\，导入时已建好），
    map_dir 与 nxmap 同构，下游渲染全部复用；id 是不带前导零的页名。

    链接图（info/link，自己的 img 是空的）自动顺链导出目标图，返回带 link 字段。
    mobs=True 时预览图把怪随机画到地面上（preview_mobs.png，沿 rx 巡逻区间散布、
    foothold 落地，大 BOSS 不合成）；seed 缺省每次随机，传同值可复现同一布局。
    drops=True 再撒一层掉落物（preview_drops.png）：只掉本图怪的掉落表里的
    东西、掉落点跟刷怪点走（金币 4 档 + chance 加权道具）。
    view=(x, y) 是导出窗口（VIEW_W×VIEW_H = 客户端画面 1366×768）的左上角世界坐标；
    缺省取地图正中。导出文件统一是 1366×768，窗口超出地图边界的地方补空白。
    life_points 是刷怪点（x, y 已按客户端规则落到 cy 地面），overlay 是
    世界坐标→预览图的变换（minx/miny/s/w/h），两者配合可在任何客户端复现点位。
    foothold_segs 是全部地面段（前端随机摆怪用它做贴地插值）。
    """
    ws = _need_ws()
    mid = str(mid).strip()
    if cls:
        return _cls_map_export(mid, preview, mobs, density, view, seed,
                               _visited if _visited is not None else set(),
                               no_bg, drops, players)
    if not re.fullmatch(r"\d{9}", mid):
        raise CliError("地图 id 应该是 9 位数字", 2)
    visited = _visited if _visited is not None else set()
    if mid in visited:
        raise CliError(f"link 成环了：{' → '.join(sorted(visited))} → {mid}", 1)
    visited.add(mid)
    root = CACHE if preview else EXPORTS
    outdir = (root / "map" / mid) if preview else (EXPORTS / "maps" / mid)

    if not _map_cache_fresh(outdir):
        outdir.parent.mkdir(parents=True, exist_ok=True)
        # 覆盖式刷新，不整目录重删：nxmap 会重写 map.json 与引用到的贴图，
        # 旧版本缓存的遗留 PNG 留着无害（渲染只按 map.json 引用取图），
        # 而且几百个文件的整目录删除在部分环境会被批量删除保护拦下
        _run(["nxmap", mid, str(outdir)], _nx_dir(), timeout=600)
    if not (outdir / "map.json").exists():
        raise CliError(f"nxmap 没产出 map.json（这个 id 存在吗？{mid}）", 1)
    _map_cache_stamp(outdir)
    mj = json.loads((outdir / "map.json").read_text(encoding="utf-8"))

    # 链接图：拿目标图的数据渲染（客户端同款行为）
    link = str(mj.get("info", {}).get("link") or "").strip()
    if link and link != mid and re.fullmatch(r"\d{9}", link):
        d = c_map_export(link, preview, mobs, density, view, seed,
                         visited, no_bg=no_bg, drops=drops, players=players)
        d["link"] = {"from": mid, "to": link}
        return d

    return _map_export_inner(ws, mid, preview, mobs, density, view, visited,
                             seed=seed, outdir=outdir, mj=mj, no_bg=no_bg,
                             drops=drops)


def _cls_map_export(mid: str, preview: bool, mobs: bool, density: int,
                    view: tuple | None, seed: str | None,
                    visited: set, no_bg: bool, drops: bool = False,
                    players: bool = False) -> dict:
    """怀旧服 WZJS 大图导出：map_dir 已在导入时建好（.cache\\wzjs-map\\<id>），
    这里只负责读 map.json + 复用渲染管线。id 是不带前导零的页名（如 101040000
    也可能像 20000 这样短），链接图语义与国服相同（顺链渲染目标图）。"""
    ws = _need_ws()
    if not re.fullmatch(r"\d{1,9}", mid):
        raise CliError("地图 id 应该是数字（WZJS 页名）", 2)
    if mid in visited:
        raise CliError(f"link 成环了：{' → '.join(sorted(visited))} → {mid}", 1)
    visited.add(mid)
    if not _wzjs_ready():
        raise CliError("怀旧服大图库还没建好 —— 数据源面板点「⬇ 转换缺失数据」", 1)
    outdir = WZJS_MAP / mid
    if not (outdir / "map.json").exists():
        raise CliError(f"怀旧服大图库里没有 {mid}（可能个别图转换失败，"
                       f"明细见 {WZJS_BUILT}）", 1)
    mj = json.loads((outdir / "map.json").read_text(encoding="utf-8"))
    link = str(mj.get("info", {}).get("link") or "").strip()
    if link and link != mid and re.fullmatch(r"\d{1,9}", link):
        d = _cls_map_export(link, preview, mobs, density, view, seed,
                            visited, no_bg, drops, players)
        d["link"] = {"from": mid, "to": link}
        return d
    return _map_export_inner(ws, mid, preview, mobs, density, view, visited,
                             seed=seed, outdir=outdir, mj=mj, no_bg=no_bg,
                             drops=drops, players=players)


# ── 自动寻路：foothold 导航图 + A* ───────────────────────────────────────────
# 地形/绳梯不需要"识别"：map.json 里的 foothold / ladderRope 就是客户端读的那份
# 真值（WZJS 转换与经典 XML 对照 9/9 一致）。这里把它建成图，A* 出「走 / 跳 /
# 落 / 爬」动作序列 —— 后续游戏内执行端（按键）按序列走即可。
#
# 物理常量全部来自客户端源码（mxd_client_source/repo）：
#   Physics.cpp      GRAVFORCE = 0.14 px/tick²
#   Player::get_walkforce   = 0.05 + 0.11·speed/100；地面摩擦 (0.5+0.1)·hspeed/3
#                            → 平衡速度 = 6×walkforce ≈ 0.8 px/tick（speed=100）
#   Player::get_jumpforce   = 1.0 + 3.5·jump/100，jump 基础 100 → 初速 4.5 px/tick
#                            → 跳高 = 4.5²/(2·0.14) ≈ 72px，滞空 ≈ 64 tick
#   Player::get_climbforce  = speed/100 → 1.0 px/tick
#   Constants::TIMESTEP     = 8ms → 125 tick/s
# 空中不加速（FALL 态只有反向减速 0.025/tick），所以水平跳距 ≈ 0.8×64 ≈ 51px，
# 起跳速度就是助跑速度，不会有"跳得更远"的额外加成。
NAV_TICK_HZ = 125.0
NAV_WALK_T = 0.80                    # px/tick
NAV_CLIMB_T = 1.00                   # px/tick
NAV_JUMP_T = 4.50                    # px/tick（起跳初速，向上）
NAV_GRAV_T = 0.14                    # px/tick²
NAV_WALK = NAV_WALK_T * NAV_TICK_HZ  # px/s（代价与启发函数用）
NAV_CLIMB = NAV_CLIMB_T * NAV_TICK_HZ
NAV_JUMP_UP = NAV_JUMP_T ** 2 / (2 * NAV_GRAV_T)   # ≈ 72px
NAV_JUMP_MAXFALL = 520               # 一次起跳最多算落到下方多少 px
NAV_SPOT_STEP = 80                   # 沿 foothold 采样间距（起跳/下坠候选点）

_NAV_PROFILE = None


def _nav_jump_profile() -> list:
    """按客户端物理逐 tick 积分一次跳跃，返回下降段 [(dy, dx)]（dy 向下为正）。

    起跳点为 (0,0)、初速向上 4.5px/tick、重力 0.14px/tick²、水平 0.8px/tick
    （助跑满速）。顶点之后 y 单调增，所以落点高度 dy 与水平位移 dx 一一对应，
    查表即可得到"跳到低 dy 的平台时最远能飞多远"。"""
    global _NAV_PROFILE
    if _NAV_PROFILE is not None:
        return _NAV_PROFILE
    pts, v, y, x, prev = [], -NAV_JUMP_T, 0.0, 0.0, 0.0
    while y < NAV_JUMP_MAXFALL:
        v += NAV_GRAV_T
        y += v
        x += NAV_WALK_T
        if y > prev:                 # 已过顶点：下降段
            pts.append((y, x))
        prev = y
    _NAV_PROFILE = pts
    return pts


def _nav_reach(dy: float) -> float | None:
    """跳到「比起点低 dy」的高度时，水平最多能飞多远（px）。dy 负数=更高。"""
    if dy < -NAV_JUMP_UP:
        return None                  # 跳不了那么高
    pts = _nav_jump_profile()
    if dy <= pts[0][0]:              # 顶点附近，第一次采样之后再插值
        for yy, xx in pts:
            if yy >= dy:
                return xx
        return None
    lo, hi = 0, len(pts) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if pts[mid][0] < dy:
            lo = mid + 1
        else:
            hi = mid
    yy, xx = pts[lo]
    if lo == 0:
        return xx
    y0, x0 = pts[lo - 1]
    if yy <= y0:
        return xx
    return x0 + (xx - x0) * (dy - y0) / (yy - y0)


def _nav_norm_fh(mj: dict) -> list:
    """把 foothold 统一成「左端 / 右端 + 两端 y」，斜面按线性插值取高度。"""
    out = []
    for f in mj.get("foothold") or []:
        try:
            x1, x2 = int(f["x1"]), int(f["x2"])
            y1, y2 = int(f["y1"]), int(f["y2"])
        except (KeyError, TypeError, ValueError):
            continue
        if x1 == x2:
            continue
        if x1 > x2:                  # 统一成左→右
            x1, x2, y1, y2 = x2, x1, y2, y1
        out.append({"id": int(f.get("id") or 0), "xl": x1, "yl": y1,
                    "xr": x2, "yr": y2, "layer": int(f.get("layer") or 0),
                    "prev": int(f.get("prev") or 0), "next": int(f.get("next") or 0)})
    return out


def _nav_y_at(fh: dict, x: float) -> float:
    if fh["xr"] <= fh["xl"]:
        return float(fh["yl"])
    x = min(max(x, fh["xl"]), fh["xr"])
    return fh["yl"] + (fh["yr"] - fh["yl"]) * (x - fh["xl"]) / (fh["xr"] - fh["xl"])


def _nav_spans(fh: dict, x: float, pad: float = 2.0) -> bool:
    return fh["xl"] - pad <= x <= fh["xr"] + pad


def _nav_ladders(mj: dict) -> list:
    out = []
    for lr in mj.get("ladderRope") or []:
        try:
            x = int(lr["x"]); a, b = int(lr["y1"]), int(lr["y2"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"x": x, "top": min(a, b), "bot": max(a, b),
                    "ladder": bool(int(lr.get("l") or 0))})
    return out


def _nav_land_below(fhs: list, x: float, y: float) -> dict | None:
    """从 (x, y) 垂直下坠，落在最高的那段 foothold 上（客户端 update_fh 同语义）。"""
    best = None
    for fh in fhs:
        if not _nav_spans(fh, x, 0.0):
            continue
        yy = _nav_y_at(fh, x)
        if yy <= y + 1:
            continue
        if best is None or yy < _nav_y_at(best, x):
            best = fh
    return best


def _nav_blocked(fhs: list, x: float, y_from: float, y_to: float,
                 up: bool = False) -> bool:
    """跳跃路径上会不会先撞到别的平台（撞上就只能落在它上面）。

    下落/平跳：中间有平台挡住落点 → 该边不存在；上跳：头顶有平台挡住起跳空间
    （客户端平台可以从下往上穿过，所以上跳只查 y_from 以上 30px 内的"天花板"）。"""
    lo, hi = (y_from - 30, y_to) if up else (y_from - 8, y_to - 2)
    for fh in fhs:
        if not _nav_spans(fh, x, 2.0):
            continue
        yy = _nav_y_at(fh, x)
        if abs(yy - y_from) <= 2.5:      # 起跳/下坠点自己脚下这段不算挡路
            continue
        if lo < yy < hi:
            return True
    return False


def _nav_graph(mj: dict) -> dict:
    """建点图：点 = (foothold, x) 或绳梯端点（fh=-1），边 = 走 / 跳 / 落 / 爬。

    绳梯两端的 y1/y2 就是挂在绳上的极限位置，客户端挂绳判定（MapInfo.cpp
    Ladder::inrange）= 水平 |dx| <= 10 且高度落在 [y1, y2] 内（上下各带 5px
    余量）。所以：绳梯底端悬空时只能从上面的平台下爬、到绳底再松手掉下去 ——
    用两个虚拟端点节点把这段表达出来。"""
    fhs, lads = _nav_norm_fh(mj), _nav_ladders(mj)
    spots, adj = [], []

    def new_spot(x: float, y: float, fh: int = -1, kind: str = "walk") -> int:
        spots.append({"fh": fh, "x": float(x), "y": float(y), "kind": kind})
        adj.append([])
        return len(spots) - 1

    def add_spot(fi: int, x: float, kind: str = "walk") -> int:
        """foothold 上的点（同段同 x 复用）；x 夹进段内。"""
        x = float(min(max(x, fhs[fi]["xl"]), fhs[fi]["xr"]))
        for i, sp in enumerate(spots):
            if sp["fh"] == fi and abs(sp["x"] - x) < 1.5:
                return i
        return new_spot(x, _nav_y_at(fhs[fi], x), fi, kind)

    def link(a: int, b: int, cost: float, act: dict) -> None:
        if a == b:
            return
        adj[a].append({"to": b, "cost": cost, "act": act})

    def walk(a: int, b: int) -> None:
        d = abs(spots[b]["x"] - spots[a]["x"])
        if d < 0.8:
            return
        link(a, b, d / NAV_WALK, {"a": "walk", "to": [spots[b]["x"], spots[b]["y"]]})
        link(b, a, d / NAV_WALK, {"a": "walk", "to": [spots[a]["x"], spots[a]["y"]]})

    # ---- 1. 每段 foothold 上的采样点（两端 + 等距；跳/落候选都从这里出）----
    for fi, fh in enumerate(fhs):
        xs = [float(fh["xl"]), float(fh["xr"])]
        n = int((fh["xr"] - fh["xl"]) / NAV_SPOT_STEP)
        xs += [fh["xl"] + (i + 1) * (fh["xr"] - fh["xl"]) / (n + 1) for i in range(n)]
        xs.sort()
        for x in xs:
            add_spot(fi, x)

    # ---- 2. 绳梯：两个虚拟端点 + 客户端判定范围内的挂点 ----
    lad_nodes: list = [[] for _ in lads]
    for li, lad in enumerate(lads):
        top = new_spot(lad["x"], lad["top"], -1, "ladder_end")
        bot = new_spot(lad["x"], lad["bot"], -1, "ladder_end")
        lad_nodes[li] = [(lad["top"], top), (lad["bot"], bot)]
        for fi, fh in enumerate(fhs):
            if not _nav_spans(fh, lad["x"], 10.0):
                continue
            yy = _nav_y_at(fh, lad["x"])
            if lad["top"] - 8 <= yy <= lad["bot"] + 8:      # 客户端 inrange + 余量
                lad_nodes[li].append((yy, add_spot(fi, lad["x"], "ladder")))

    # ---- 3. 走：同一段上相邻点 + 跨段（prev/next 或首尾相接）----
    by_fh: dict = {}
    for i, sp in enumerate(spots):
        if sp["fh"] >= 0:
            by_fh.setdefault(sp["fh"], []).append(i)
    for fi, ids in by_fh.items():
        ids.sort(key=lambda i: spots[i]["x"])
        for a, b in zip(ids, ids[1:]):
            walk(a, b)
    # 跨段：A 的右端 ≈ B 的左端（含 prev/next 声明的连接），高度差小就算连通
    for fi, fh in enumerate(fhs):
        for gi, gh in enumerate(fhs):
            if fi == gi:
                continue
            gap = gh["xl"] - fh["xr"]
            dy = abs(_nav_y_at(gh, gh["xl"]) - _nav_y_at(fh, fh["xr"]))
            if fh["next"] != gh["id"] and not (-4 <= gap <= 14 and dy <= 16):
                continue
            if dy > 24:
                continue
            a = add_spot(fi, fh["xr"])
            b = add_spot(gi, gh["xl"])
            d = max(1.0, abs(gap) + dy)
            link(a, b, d / NAV_WALK, {"a": "walk", "to": [spots[b]["x"], spots[b]["y"]]})
            link(b, a, d / NAV_WALK, {"a": "walk", "to": [spots[a]["x"], spots[a]["y"]]})

    # ---- 4. 落：每个点上垂直下坠到下面最高的平台（含绳梯端点松手）----
    for i, sp in enumerate(spots):
        land = _nav_land_below(fhs, sp["x"], sp["y"])
        if land is None or (sp["fh"] >= 0 and land is fhs[sp["fh"]]):
            continue
        ly = _nav_y_at(land, sp["x"])
        dy = ly - sp["y"]
        if dy < 6 or _nav_blocked(fhs, sp["x"], sp["y"], ly):
            continue
        j = add_spot(fhs.index(land), sp["x"], "fall")
        act = {"a": "fall", "to": [spots[j]["x"], spots[j]["y"]]}
        if sp["fh"] >= 0:
            # 段中间下坠 = 客户端 DOWN+JUMP（穿过平台）；两端 = 走下去
            fh = fhs[sp["fh"]]
            act["drop"] = fh["xl"] + 4 < sp["x"] < fh["xr"] - 4
        link(i, j, 0.25 + dy / 700.0, act)

    # ---- 5. 跳：每个点上按跳跃包线找能落脚的其他平台 ----
    for i, sp in enumerate(spots):
        if sp["fh"] < 0:
            continue
        sx, sy, sfi = sp["x"], sp["y"], sp["fh"]
        for gi, gh in enumerate(fhs):
            if gi == sfi:
                continue
            if gh["xr"] < sx - 220 or gh["xl"] > sx + 220:
                continue
            lx = min(max(sx, gh["xl"]), gh["xr"])
            ly = _nav_y_at(gh, lx)
            dy, dx = ly - sy, lx - sx
            if abs(dy) < 2 and abs(dx) < 2:
                continue
            reach = _nav_reach(dy)
            if reach is None or abs(dx) > reach + 2 or dy > 380:
                continue
            if _nav_blocked(fhs, lx, sy, ly, up=dy < 0):
                continue
            if dy < 0 and _nav_blocked(fhs, lx, sy - NAV_JUMP_UP + 4, sy, up=True):
                continue
            j = add_spot(gi, lx, "jump")
            cost = 0.55 + max(0.0, dy) / 260.0 + abs(dx) / 420.0
            act = {"a": "jump", "from": [sx, sy], "to": [spots[j]["x"], spots[j]["y"]],
                   "dir": 1 if dx >= 0 else -1}
            link(i, j, cost, act)
        # 跳过去抓绳：绳底悬空的绳梯只能这么上（客户端允许空中按↑抓）
        for li, nodes in enumerate(lad_nodes):
            for _, j in nodes:
                nx, ny = spots[j]["x"], spots[j]["y"]
                dx, dy = nx - sx, ny - sy
                if abs(dx) > 24 or dy > 320:
                    continue
                reach = _nav_reach(dy)
                if reach is None or abs(dx) > reach + 2:
                    continue
                if _nav_blocked(fhs, nx, sy, ny, up=dy < 0):
                    continue
                link(i, j, 0.75 + abs(dx) / 420.0, {
                    "a": "grab", "from": [sx, sy], "to": [nx, ny],
                    "dir": 1 if dx >= 0 else -1, "x": lads[li]["x"]})

    # ---- 6. 爬：同一根绳梯上按高度排序的节点两两相连（相邻层，双向）----
    for li, lad in enumerate(lads):
        nodes = sorted(lad_nodes[li], key=lambda t: t[0])
        merged = []                     # 高度几乎相同的挂点/端点合并，免得原地打转
        for y, i in nodes:
            if merged and abs(y - merged[-1][0]) < 3.5:
                continue
            merged.append((y, i))
        for (ya, a), (yb, b) in zip(merged, merged[1:]):
            dy = yb - ya
            if dy < 4:
                continue
            up = {"a": "climb", "from": [spots[a]["x"], spots[a]["y"]],
                  "to": [spots[b]["x"], spots[b]["y"]],
                  "ladder": lad["ladder"], "x": lad["x"]}
            dn = {"a": "climb", "from": up["to"], "to": up["from"],
                  "ladder": lad["ladder"], "x": lad["x"]}
            link(a, b, dy / NAV_CLIMB + 0.4, up)
            link(b, a, dy / NAV_CLIMB + 0.4, dn)

    return {"fhs": fhs, "ladders": lads, "spots": spots, "adj": adj}


_NAV_CACHE: dict = {}


def _nav_cached(mid: str) -> dict | None:
    """按 map.json 的 mtime 缓存导航图（一张图几百个点，A* 很快）。"""
    for d in (WZJS_MAP / mid, CACHE / "map" / mid):
        p = d / "map.json"
        if not p.exists():
            continue
        try:
            st = p.stat().st_mtime
        except OSError:
            continue
        key = (str(p), st)
        hit = _NAV_CACHE.get(key)
        if hit is None:
            try:
                mj = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            hit = _nav_graph(mj)
            _NAV_CACHE.clear()
            _NAV_CACHE[key] = hit
        return hit
    return None


def _nav_snap(g: dict, x: float, y: float) -> int | None:
    """把任意点吸到 foothold 上：优先脚下/最近的平台，再在段内取 x。"""
    fhs, best = g["fhs"], None
    for fi, fh in enumerate(fhs):
        if not _nav_spans(fh, x, 40.0):
            continue
        px = min(max(x, fh["xl"]), fh["xr"])
        yy = _nav_y_at(fh, px)
        score = abs(yy - y) + abs(px - x) * 0.35
        if yy < y - 26:              # 头顶的平台不是"脚下这块"
            score += 200
        if best is None or score < best[0]:
            best = (score, fi, px, yy)
    if best is None:
        return None
    _, fi, px, yy = best
    for i, sp in enumerate(g["spots"]):
        if sp["fh"] == fi and abs(sp["x"] - px) < 1.5:
            return i
    g["spots"].append({"fh": fi, "x": px, "y": yy, "kind": "tmp"})
    g["adj"].append([])
    new = len(g["spots"]) - 1
    # 临时点接回原图：同段点 + 走/跳/落三类边
    _nav_wire_tmp(g, new)
    return new


def _nav_wire_tmp(g: dict, i: int) -> None:
    """给动态点（起点/终点）补边：同段相邻点走，其余平台按跳/落规则连。"""
    sp, spots, fhs, adj = g["spots"][i], g["spots"], g["fhs"], g["adj"]
    sx, sy = sp["x"], sp["y"]
    same = [j for j, o in enumerate(spots)
            if o["fh"] == sp["fh"] and abs(o["x"] - sx) > 1.0]
    same.sort(key=lambda j: abs(spots[j]["x"] - sx))
    for j in same[:2]:
        d = abs(spots[j]["x"] - sx)
        adj[i].append({"to": j, "cost": d / NAV_WALK,
                       "act": {"a": "walk", "to": [spots[j]["x"], spots[j]["y"]]}})
        adj[j].append({"to": i, "cost": d / NAV_WALK,
                       "act": {"a": "walk", "to": [sx, sy]}})
    land = _nav_land_below(fhs, sx, sy)
    if land is not None and land is not fhs[sp["fh"]]:
        ly = _nav_y_at(land, sx)
        dy = ly - sy
        if dy > 6 and not _nav_blocked(fhs, sx, sy, ly):
            for j, o in enumerate(spots):
                if o["fh"] == fhs.index(land) and abs(o["x"] - sx) < 1.5:
                    adj[i].append({"to": j, "cost": 0.25 + dy / 700.0,
                                   "act": {"a": "fall", "to": [o["x"], o["y"]]}})
                    break
    for gi, gh in enumerate(fhs):
        if gi == sp["fh"]:
            continue
        if gh["xr"] < sx - 220 or gh["xl"] > sx + 220:
            continue
        lx = min(max(sx, gh["xl"]), gh["xr"])
        ly = _nav_y_at(gh, lx)
        dy, dx = ly - sy, lx - sx
        if abs(dy) < 2 and abs(dx) < 2:
            continue
        reach = _nav_reach(dy)
        if reach is None or abs(dx) > reach + 2 or dy > 380:
            continue
        if _nav_blocked(fhs, lx, sy, ly, up=dy < 0):
            continue
        for j, o in enumerate(spots):
            if o["fh"] == gi and abs(o["x"] - lx) < 1.5:
                adj[i].append({"to": j, "cost": 0.55, "act": {
                    "a": "jump", "from": [sx, sy], "to": [o["x"], o["y"]],
                    "dir": 1 if dx >= 0 else -1}})
                break


def _nav_astar(g: dict, src: int, dst: int) -> list | None:
    import heapq
    spots, adj = g["spots"], g["adj"]
    tx, ty = spots[dst]["x"], spots[dst]["y"]

    def h(i: int) -> float:
        return ((spots[i]["x"] - tx) ** 2 + (spots[i]["y"] - ty) ** 2) ** 0.5 / NAV_CLIMB

    gs = {src: 0.0}
    prev: dict = {}
    heap = [(h(src), 0.0, src)]
    seen = set()
    while heap:
        _, gc, i = heapq.heappop(heap)
        if i in seen:
            continue
        seen.add(i)
        if i == dst:
            path = [i]
            while path[-1] != src:
                path.append(prev[path[-1]])
            return list(reversed(path))
        for e in adj[i]:
            j = e["to"]
            if j in seen:
                continue
            ng = gc + e["cost"]
            if ng < gs.get(j, 1e18):
                gs[j] = ng
                prev[j] = i
                heapq.heappush(heap, (ng + h(j), ng, j))
    return None


def c_cls_path(mid: str, fx: float, fy: float, tx: float, ty: float) -> dict:
    """地图寻路：世界坐标 from → to，返回动作序列 + 折线（画图/执行两用）。

    点击点在半空时按客户端语义先**自由落体**：垂直坠到下面第一块 foothold
    （脚下的平台从上面是实心的，下坠只会落在最先碰到的平台上），再从落点
    开始规划；终点在半空同理，目的地 = 点击点正下方的落点。"""
    mid = str(mid).strip()
    if not re.fullmatch(r"\d{1,9}", mid):
        raise CliError("地图 id 应该是数字", 2)
    g = _nav_cached(mid)
    if g is None:
        raise CliError(f"地图 {mid} 还没有 map.json 缓存（先打开这张图渲染一次）", 1)
    fhs = g["fhs"]

    def on_ground(x: float, y: float) -> bool:
        """点击点脚下 30px 内就有 foothold = 站在地面上（页面点怪/点人时
        鼠标常常悬在角色 sprite 中部，偏上几十px 也算站这块地面）。"""
        for fh in fhs:
            if not _nav_spans(fh, x, 40.0):
                continue
            px = min(max(x, fh["xl"]), fh["xr"])
            if abs(_nav_y_at(fh, px) - y) <= 30:
                return True
        return False

    def drop_spot(x: float, y: float) -> int | None:
        """半空点 → 垂直下坠落到下面第一块地面，落点建成临时节点接进图。"""
        land = _nav_land_below(fhs, x, y)
        if land is None:
            return None
        fi = fhs.index(land)
        px = min(max(x, land["xl"]), land["xr"])
        for i, sp in enumerate(g["spots"]):
            if sp["fh"] == fi and abs(sp["x"] - px) < 1.5:
                return i
        g["spots"].append({"fh": fi, "x": px, "y": _nav_y_at(land, px), "kind": "tmp"})
        g["adj"].append([])
        new = len(g["spots"]) - 1
        _nav_wire_tmp(g, new)
        return new

    a = b = None
    pre = None                                  # 半空起点：先落体再规划
    air_a = not on_ground(fx, fy)
    if air_a:
        a = drop_spot(fx, fy)
        if a is not None:
            sp = g["spots"][a]
            pre = {"a": "fall", "from": [round(fx, 1), round(fy, 1)],
                   "to": [round(sp["x"], 1), round(sp["y"], 1)]}
    if a is None:
        a = _nav_snap(g, fx, fy)
    air_b = not on_ground(tx, ty)
    b_eff = [round(tx, 1), round(ty, 1)]        # 终点标记画在「实际能站到的落点」
    if air_b:
        b = drop_spot(tx, ty)
        if b is not None:
            sp = g["spots"][b]
            b_eff = [round(sp["x"], 1), round(sp["y"], 1)]
    if b is None:
        b = _nav_snap(g, tx, ty)
    if a is None or b is None:
        raise CliError("起点或终点附近没有可站立的地面（点在地图外了？）", 1)
    nodes = _nav_astar(g, a, b)
    if not nodes:
        return {"ok": False, "why": "没有连通的路线（可能被墙隔开/需要传送门）",
                "from": [fx, fy], "to": [tx, ty]}
    spots = g["spots"]
    pts = [[round(spots[i]["x"], 1), round(spots[i]["y"], 1)] for i in nodes]
    acts, cost = [], 0.0
    for u, v in zip(nodes, nodes[1:]):
        e = next((x for x in g["adj"][u] if x["to"] == v), None)
        if e is None:
            continue
        cost += e["cost"]
        a_ = e["act"]
        if a_["a"] == "walk" and acts and acts[-1]["a"] == "walk":
            acts[-1]["to"] = a_["to"]          # 连续走合并成一段
        else:
            acts.append(a_)
    if pre is not None:
        acts.insert(0, pre)
    elif acts and acts[0]["a"] == "walk":
        acts[0]["from"] = [round(fx, 1), round(fy, 1)]
    return {"ok": True, "id": mid, "pts": pts, "actions": acts,
            "cost": round(cost, 2), "spots": len(spots),
            "to_eff": b_eff, "air": [air_a, air_b],
            "jumps": sum(1 for x in acts if x["a"] == "jump"),
            "falls": sum(1 for x in acts if x["a"] == "fall"),
            "climbs": sum(1 for x in acts if x["a"] == "climb")}


# ── 全库出图（map-all）──────────────────────────────────────────────────────
# 临时脚本 scripts/_maptest.py 的正式版：把每张图的预览缓存
# （map.json + 贴图 + preview.png，WebUI「预览」同一条链路）批量建好，
# 顺带把全库「能否正常显示」验了一遍。断点续跑，中断重跑自动跳过。
MAPALL_BASE = ROOT / ".build" / "mapall"


def _map_all_one(mid: str) -> dict:
    """子进程里给一张图建预览缓存（与 WebUI「预览」完全同链路）。"""
    t0 = time.time()
    outdir = CACHE / "map" / mid
    try:
        # 已有新鲜缓存 + 预览图：跳过（用户浏览过的图秒过）
        if (_map_cache_fresh(outdir) and (outdir / "preview.png").exists()
                and (outdir / "preview.png").with_suffix(".meta").exists()):
            return {"id": mid, "ok": True, "skip": True, "sec": 0.0}
        outdir.mkdir(parents=True, exist_ok=True)
        _run(["nxmap", mid, str(outdir)], _nx_dir(), timeout=600)
        mjf = outdir / "map.json"
        if not mjf.exists():
            return {"id": mid, "ok": False, "err": "nxmap 没产出 map.json",
                    "sec": round(time.time() - t0, 1)}
        # 链接图跟随（客户端同款）：目标图的贴图要导进同一目录
        seen = {mid}
        mj = json.loads(mjf.read_text(encoding="utf-8"))
        for _ in range(3):
            link = str(mj.get("info", {}).get("link") or "").strip()
            if not (link.isdigit() and link not in seen):
                break
            seen.add(link)
            _run(["nxmap", link, str(outdir)], _nx_dir(), timeout=600)
            mj = json.loads(mjf.read_text(encoding="utf-8"))
        _map_cache_stamp(outdir)
        size = _render_preview(outdir, outdir / "preview.png")
        if (size.get("w") or 0) <= 0 or (size.get("h") or 0) <= 0:
            return {"id": mid, "ok": False,
                    "err": f"渲染尺寸异常 {size.get('w')}x{size.get('h')}",
                    "sec": round(time.time() - t0, 1)}
        return {"id": mid, "ok": True, "w": size["w"], "h": size["h"],
                "sec": round(time.time() - t0, 1)}
    except Exception as e:                     # noqa: BLE001
        return {"id": mid, "ok": False,
                "err": f"{type(e).__name__}: {str(e)[:300]}",
                "sec": round(time.time() - t0, 1)}


def _map_all_done_ids() -> set:
    """断点续跑的已完成集合：results.jsonl 里 ok 的 id + 已有可用缓存的图。"""
    done: set = set()
    res = MAPALL_BASE / "results.jsonl"
    if res.exists():
        for ln in res.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln.strip())
                if r.get("ok"):
                    done.add(r["id"])
            except Exception:                  # noqa: BLE001
                pass
    return done


def _map_all_pending() -> list:
    """还差哪些图没出（决定转换任务要不要并上全库出图这一步）。"""
    mapnx = _nx_dir() / "Map.nx"
    if not mapnx.exists():
        return []
    ids = [i for i in c_maps()["maps"] if re.fullmatch(r"\d{9}", i)]
    done = _map_all_done_ids()
    return [i for i in ids
            if i not in done and not (CACHE / "map" / i / "preview.png").exists()]


def c_map_all(workers: int = 2, log=None, prog=None) -> dict:
    """全库出图：把所有地图的预览缓存批量建好（WebUI 预览同一条链路）。

    跑完之后浏览任何图都是秒开（不用现场 nxmap + 渲染大图），同时等于把
    全库「能否正常显示」全验了一遍。断点续跑：已完成记录在
    .build/mapall/results.jsonl，中断/停掉后重跑自动跳过。
    workers 默认 2 —— 抢得少，跑的时候照样能浏览地图（8 个 worker 会把
    23G Map.nx 和 CPU 全占了，网页点新图会卡几分钟）。
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed
    _need_ws()
    workers = max(1, min(16, int(workers)))
    ids = [i for i in c_maps()["maps"] if re.fullmatch(r"\d{9}", i)]
    MAPALL_BASE.mkdir(parents=True, exist_ok=True)
    res_path = MAPALL_BASE / "results.jsonl"
    done_ids = _map_all_done_ids()
    todo = [i for i in ids if i not in done_ids]
    total = len(ids)
    say = log or print
    say(f"全库出图: 库里 {total} 张，已出 {len(done_ids)}，本轮 {len(todo)}（workers={workers}）")
    stats = {"total": total, "done": len(done_ids), "ok": len(done_ids),
             "fail": 0, "sec": 0.0}
    if not todo:
        return {**stats, "started": False}
    t0 = time.time()
    n = 0
    with res_path.open("a", encoding="utf-8") as rf, \
            ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_map_all_one, i) for i in todo]
        for fu in as_completed(futs):
            r = fu.result()
            rf.write(json.dumps(r, ensure_ascii=False) + "\n")
            rf.flush()
            n += 1
            stats["done"] += 1
            stats["sec"] += r.get("sec") or 0
            if r.get("ok"):
                stats["ok"] += 1
            else:
                stats["fail"] += 1
                say(f"✗ {r['id']}: {r['err']}")
            if n % 50 == 0 or n == len(todo):
                el = time.time() - t0
                left = len(todo) - n
                eta = el / max(1, n) * left / 60
                frac = stats["done"] / max(1, total)
                stage = f"全量出图 {stats['done']}/{total}"
                if prog:
                    prog(stage, frac)
                say(f"… {stage}（预计还要 {eta:.0f} 分钟）")
    say(f"全库出图完成: {stats['ok']}/{total} 可显示，失败 {stats['fail']}")
    return {**stats, "started": True,
            "elapsed_min": round((time.time() - t0) / 60, 1)}


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
    auto = False
    if (mm.get("cw") or 0) <= 1:
        # 退化 canvas：有自动生成的兜底图就用它（找图索引/HUD/导出三处共用）
        fb = MMIDX / f"{outdir.name}.png"
        try:
            from PIL import Image as _Img
            if fb.exists() and _Img.open(fb).size > (1, 1):
                canvas = fb
                auto = True
        except Exception:                      # noqa: BLE001
            pass
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
    if auto:
        url = f"/preview/minimap/{outdir.name}.png"   # 兜底图在 CACHE/minimap/，不在 root 下
        try:
            from PIL import Image as _Img
            cw, ch = _Img.open(canvas).size           # 兜底图的真实尺寸（≠ WZ 里的 1×1）
        except Exception:                             # noqa: BLE001
            cw, ch = mm.get("cw") or 0, mm.get("ch") or 0
    else:
        url = prefix + canvas.relative_to(root).as_posix()
        cw, ch = mm.get("cw") or 0, mm.get("ch") or 0
    portals = [{"pn": p.get("pn", ""),
                **px(p.get("x", 0), p.get("y", 0)),
                "tm": p.get("tm", 0), "tmn": p.get("tmn", "")}
               for p in mj.get("portal", []) if p.get("pt") == 2]
    npcs, mobs = [], []
    if not auto:                                      # 自动生成的图只留传送点
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
    return {"url": url,
            "img_file": str(canvas),
            "w": mm.get("w"), "h": mm.get("h"),
            "cw": cw, "ch": ch,
            "center_x": mm.get("centerX"), "center_y": mm.get("centerY"),
            "mag": mm.get("mag"), "scale": scale,
            "auto": auto, "map_mark": mj.get("mapMark"),
            "portals": portals, "npcs": npcs, "mobs": mobs}


VIEW_W, VIEW_H = 1366, 768      # 客户端画面窗口，也是唯一的导出尺寸


# ── 官方小地图素材（UI.wz/UIWindow2.img/MiniMap + Map.wz/MapHelper.img/minimap）──
# 现在只用怀旧服经典版皮肤：webui/assets/minimap/cls/（分层素材 + marks 标记），
# 素材从游戏内截图 1:1 抠出，_cls_window()/前端 HUD 共用。
MMUI_STATIC = WEBUI / "assets" / "minimap"


_content_pngsize_cache: dict = {}


def _content_bottom(outdir: Path, mj: dict) -> float | None:
    """地面内容（tile/obj 贴图）不透明主体的实际下边缘。

    贴图从 outdir\\img 的 PNG 读（cls/nx 两条管线产物同构），下缘按
    「alpha>128 的像素覆盖率 ≥50% 的最后一行」算——很多地面瓦片（岩山
    enH1 这类）底下几行是稀疏毛边，按整张贴图高算会把这些锯齿缝也算进
    视窗，贴到最底还是从毛边缝里透出一条天空蓝。
    无 VR 地图的视窗下边界钳制用：客户端兜底「最低 foothold +100」的 100px
    是留给地面贴图厚度的余量，但有的图（101030400 东部岩山）最低 foothold
    就在贴图行边上，+100 会把视窗推到地面贴图下面，露出一条游戏里根本
    看不到的天空蓝。"""
    sizes = _content_pngsize_cache.setdefault(str(outdir), {})
    bottom = None
    for L in mj.get("layers", []):
        for e in list(L.get("tile", [])) + list(L.get("obj", [])):
            img = e.get("img")
            if not img:
                continue
            h = sizes.get(img)
            if h is None:
                p = outdir / "img" / img
                try:
                    import numpy as np
                    from PIL import Image
                    a = np.asarray(Image.open(p).convert("RGBA").getchannel("A"))
                    # 视窗下边界要落在「整行每个像素都不透明」的行上：
                    # 岩山 enH1 这类瓦片底缘是稀疏毛刺，刺之间的缝隙柱往上
                    # 延伸十几 px，行覆盖率 50%/95% 的中间阈值都堵不住——
                    # 贴到最底还是会从缝里透蓝。逐级回退防全半透明贴图。
                    cov = (a > 128).mean(axis=1)          # 每行不透明覆盖率
                    solid = np.flatnonzero(cov >= 0.999)
                    if not solid.size:
                        solid = np.flatnonzero(cov >= 0.95)
                    if not solid.size:
                        solid = np.flatnonzero(cov >= 0.5)
                    h = int(solid[-1]) + 1 if solid.size else 0
                except Exception:          # noqa: BLE001
                    h = 0
                sizes[img] = h
            b = float(e.get("y", 0)) - float(e.get("oy", 0)) + h
            bottom = b if bottom is None else max(bottom, b)
    return bottom


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
                      view: tuple | None, visited: set,
                      seed: str | None = None,
                      outdir: Path | None = None, mj: dict | None = None,
                      no_bg: bool = False,
                      drops: bool = False,
                      players: bool = False) -> dict:
    root = CACHE if preview else EXPORTS
    if outdir is None:
        outdir = (root / "map" / mid) if preview else (EXPORTS / "maps" / mid)
    else:
        try:                  # cls 大图库在 .cache 下：URL/小地图按目录真实归属算
            outdir.relative_to(CACHE)
            root = CACHE
        except ValueError:
            root = EXPORTS
    if mj is None:
        mj = json.loads((outdir / "map.json").read_text(encoding="utf-8"))
    # 无背景版单独一个文件名：预览图是按文件是否存在来复用的，
    # 两种版本共用一个名字会互相命中缓存
    png = outdir / ("preview_nobg.png" if no_bg else "preview.png")
    size = _render_preview(outdir, png, no_bg=no_bg)
    if preview and no_bg:
        # 前端底图恒用无背景版（背景由视差层现画）；「点开放大查看」整图
        # 仍是烤好背景的版本 → 两个版本都渲染（都有缓存，只有首次慢）
        _render_preview(outdir, outdir / "preview.png", no_bg=False)
    life = [l for l in mj.get("life", []) if l.get("type") == "m"]
    kinds = sorted({str(l.get("id", "")).lstrip("0") for l in life})
    mobnx = _nx_dir() / "Mob.nx"
    meta = {m["id"]: m for m in mob_meta(mobnx)} if mobnx.exists() else {}
    # 怪物等级（推荐等级用）：meta 已被 mobstats/boss 参照表补齐；不在 Mob.nx 里的
    # 怪再直接查服务端数值表
    _st = _mob_stat_table()

    def _kind_lv(k: str) -> int:
        lv = meta.get(k, {}).get("level") or 0
        if lv <= 0 and k in _st:
            lv = _st[k][0]
        return lv
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
               "lv": _kind_lv(k),
               "tier": meta.get(k, {}).get("tier", "normal")}
              for l in life]
    # 可视范围：客户端 MapInfo.cpp 读 info 的 VRLeft/Right/Top/Bottom，相机永远
    # 钳在里面（Camera.cpp:58-66）；没配 VR 时的兜底不是裸 foothold 包围盒——
    # 客户端 Footholdtree.cpp:91-92 是 walls=(左+25,右-25)、
    # borders=(顶-300,底+100)：地面贴图在最低 foothold 下面还有约 100px 厚度，
    # 不加余量窗口底边正好钳在 foothold 线上，地面边缘被切掉（玩家实测 102020200）
    info = mj.get("info") or {}
    vr = None
    try:
        vr = {"left": int(info["VRLeft"]), "right": int(info["VRRight"]),
              "top": int(info["VRTop"]), "bottom": int(info["VRBottom"])}
    except (KeyError, TypeError, ValueError):
        if mj.get("foothold"):
            xs = [v for f in mj["foothold"] for v in (f["x1"], f["x2"])]
            ys = [v for f in mj["foothold"] for v in (f["y1"], f["y2"])]
            # 下边界：foothold+100（客户端兜底）与地面贴图实际下边缘取较小——
            # +100 的余量是给「贴图比最低 foothold 低一大截」的图的，最低
            # foothold 就贴着贴图边的图（101030400 岩山）会把视窗推进地面
            # 下面的天空里，游戏里视角到不了那里。
            # 保险：最多钳到最低 foothold+10，最低平台本身永远得进视窗
            # （+10 而不是 +20：岩山这类图的内容下缘只比 foothold 低 16px，
            # 给多了又把视窗推进毛边缝里）
            bot = max(ys) + 100
            cb = _content_bottom(outdir, mj)
            if cb is not None:
                bot = min(bot, cb)
            bot = max(bot, max(ys) + 10)
            vr = {"left": min(xs) + 25, "right": max(xs) - 25,
                  "top": min(ys) - 300, "bottom": bot}
    result = {"dir": str(outdir), "preview": size,
              "no_bg": bool(no_bg),
              "preview_path": str(png),
              "preview_url": _res_url(png),
              "life_count": len(life),
              "life_kinds": [{"id": k,
                              "name": meta.get(k, {}).get("name") or mob_names().get(k) or "",
                              "lv": _kind_lv(k),
                              "hp": (meta.get(k, {}).get("hp")
                                     if (meta.get(k, {}).get("hp") or 0) > 0
                                     else (_st[k][1] if k in _st else 0)),
                              "tier": meta.get(k, {}).get("tier", "normal")} for k in kinds],
              # 推荐等级 = 本图自然刷新怪的等级区间（数值来自服务端表；
              # 后期版本图服务端没实装，取不到就为 null，前端不显示）
              "mob_lv": ([min(lvs), max(lvs)]
                         if (lvs := [v for v in map(_kind_lv, kinds) if v > 0]) else None),
              "footholds": len(mj.get("foothold", [])),
              "ladders": len(mj.get("ladderRope", [])),
              "overlay": {"minx": minx, "miny": miny, "s": s,
                          "w": round(bw * s), "h": round(bh * s)},
              "vr": vr,
              "life_points": points,
              # 全部地面段 [x1,y1,x2,y2]：前端随机摆怪时做贴地插值
              "foothold_segs": [[f["x1"], f["y1"], f["x2"], f["y2"]]
                                for f in mj.get("foothold", [])],
              # 绳梯段 [x,y1,y2,l]：l=1 梯子 / 0 绳子（都是垂直的，x 单值）
              "ladder_rope": [[lr["x"], lr["y1"], lr["y2"], lr.get("l", 0)]
                              for lr in mj.get("ladderRope", [])]}
    # 背景层元数据：前端按客户端视差公式把 back 画成屏幕空间层（跟镜头走）。
    # 图片文件在导出目录 img/ 下，直接用 /preview/ 或 /exports/ 静态路由取
    img_prefix = _res_url(outdir / "img")
    result["back"] = [{**b, "url": f"{img_prefix}/{b['img']}"}
                      for b in mj.get("back", []) if b.get("img")]
    # 导出图带怪物：把怪沿地面随机散布合成进预览图（preview_mobs.png）
    use_seed = str(seed) if seed else f"{mid}:{random.randrange(1 << 30):x}"
    places: list[dict] = []
    if mobs and life:
        mob_base, mob_nobg = png, no_bg
        if not preview and not no_bg:
            # 窗口导出的背景由视差公式现画（下面）：怪物必须合成在没烤背景的
            # nobg 底图上，否则烤死的背景会跟现画的视差层叠成两层
            mob_base = outdir / "preview_nobg.png"
            _render_preview(outdir, mob_base, no_bg=True)
            mob_nobg = True
        mob_png, places = _composite_mobs(ws, outdir, mj, mob_base, use_seed,
                                          density=max(1, min(10, int(density or 1))),
                                          no_bg=mob_nobg)
        if mob_png is not None:
            result["preview_path"] = str(mob_png)
            result["preview_url"] = _res_url(mob_png)
            result["mobs"] = True
    # 掉落物合成：只掉本图有的怪的掉落表里的东西，掉落点跟刷怪点走。
    # 没开 mobs 也能单独开 drops（底图用普通预览）。
    if drops and life:
        # 窗口导出 + 要背景：与 mobs 同款——掉落必须撒在没烤背景的 nobg 底图上，
        # 视窗裁剪时透出下面现画的视差背景；带怪时底图已是 mobs 合成结果（nobg）
        if not preview and not no_bg and not result.get("mobs"):
            drop_base = outdir / "preview_nobg.png"
            _render_preview(outdir, drop_base, no_bg=True)
        else:
            drop_base = Path(result.get("preview_path") or png)
        # 底图是透明版（mobs_nobg / no_bg 预览）时掉落也必须保 alpha：
        # 窗口导出靠 alpha_composite 把透明区透给现画的视差背景
        drop_png, drop_places = _composite_drops(
            outdir, mj, drop_base, use_seed,
            no_bg=no_bg or "nobg" in drop_base.stem)
        if drop_png is not None:
            result["preview_path"] = str(drop_png)
            result["preview_url"] = _res_url(drop_png)
            result["drops"] = True
            result["drop_places"] = drop_places
    # 随机玩家合成：1~5 个随机外观的玩家站到地面上（页面叠加层同规则，
    # 名字一起画进图）。底图规则与 drops 一致——窗口导出必须落在 nobg 底图上。
    if players:
        if not preview and not no_bg and not result.get("mobs"):
            pl_base = outdir / "preview_nobg.png"
            _render_preview(outdir, pl_base, no_bg=True)
        else:
            pl_base = Path(result.get("preview_path") or png)
        pl_png, pl_places = _composite_players(
            outdir, mj, pl_base, use_seed,
            no_bg=no_bg or "nobg" in pl_base.stem)
        if pl_png is not None:
            result["preview_path"] = str(pl_png)
            result["preview_url"] = _res_url(pl_png)
            result["players"] = True
            result["player_places"] = pl_places
    # 导出 = 客户端那个固定 1366×768 的画面窗口（预览模式输出整张 1:1 地图，供页面叠窗口用）
    if not preview:
        from PIL import Image
        vx, vy = _view_origin(view, minx, miny, bw, bh)
        # 再钳进 VR（客户端 Camera.cpp:58-66 视窗永远在 VR 里）：直传 API/CLI
        # 的 view 不再能落到游戏里到不了的地方（如岩山无 VR 图兜底边界下面的
        # 天空）。VR 比窗口还小的维度居中，与 _view_origin 同款规则。
        if vr:
            if vr["right"] - vr["left"] >= VIEW_W:
                vx = min(max(vx, vr["left"]), vr["right"] - VIEW_W)
            else:
                vx = (vr["left"] + vr["right"] - VIEW_W) / 2
            if vr["bottom"] - vr["top"] >= VIEW_H:
                vy = min(max(vy, vr["top"]), vr["bottom"] - VIEW_H)
            else:
                vy = (vr["top"] + vr["bottom"] - VIEW_H) / 2
        left, top = round(vx - minx), round(vy - miny)
        canvas = Image.new("RGBA", (VIEW_W, VIEW_H),
                           (0, 0, 0, 0) if no_bg else (16, 18, 24, 255))
        base_png = outdir / "preview_nobg.png"
        if not no_bg:
            # 背景照客户端 MapBackgrounds::draw 现画：焦点 = 视窗中心的世界坐标。
            # 背景是屏幕空间层跟镜头走，不能再从烤死背景的全图上裁
            _render_preview(outdir, base_png, no_bg=True)
            R.draw_backs_view(canvas, mj.get("back", []), R.TexStore([str(outdir / "img")]),
                              vx + VIEW_W / 2, vy + VIEW_H / 2, VIEW_W, VIEW_H, front=0)
        # 窗口底图：带怪/带掉落/带玩家用合成结果（已在 nobg 底图上），否则就是 nobg 底图
        src_path = (Path(result["preview_path"])
                    if (result.get("mobs") or result.get("drops")
                        or result.get("players")) else base_png)
        src = Image.open(src_path).convert("RGBA")
        # 窗口可以超出地图边界（小图/边缘），只贴交集，其余留空
        sx0, sy0 = max(0, left), max(0, top)
        sx1 = min(src.width, left + VIEW_W)
        sy1 = min(src.height, top + VIEW_H)
        if sx1 > sx0 and sy1 > sy0:
            # 必须 alpha_composite：底图是无背景版，透明区域不能把下面
            # 现画的视差背景层擦掉（paste 会连 alpha 一起覆盖成透明黑）
            canvas.alpha_composite(src.crop((sx0, sy0, sx1, sy1)), (sx0 - left, sy0 - top))
        if not no_bg:
            R.draw_backs_view(canvas, mj.get("back", []), R.TexStore([str(outdir / "img")]),
                              vx + VIEW_W / 2, vy + VIEW_H / 2, VIEW_W, VIEW_H, front=1)
        out_png = outdir / ("window_nobg.png" if no_bg else "window.png")
        canvas.convert("RGBA" if no_bg else "RGB").save(out_png)
        result["preview"] = {"w": VIEW_W, "h": VIEW_H}
        result["preview_path"] = str(out_png)
        result["preview_url"] = _res_url(out_png)
        result["window"] = {"x": vx, "y": vy, "w": VIEW_W, "h": VIEW_H,
                            "px": {"x": left, "y": top}}
        # 叠加层坐标系改成以窗口左上角为原点，圆点/立绘在窗口图上照样对准
        result["overlay"] = {"minx": vx, "miny": vy, "s": 1.0,
                             "w": VIEW_W, "h": VIEW_H}

    # 游戏内小地图（miniMap canvas + 客户端同款标记换算），Web UI / AI 都能用
    result["name"] = mj.get("name", "")
    result["street_name"] = mj.get("streetName", "")
    result["minimap"] = _minimap_payload(outdir, root, mj, meta)
    return result


def _cls_window(img: "Image", street: str = "", name: str = "") -> "Image":
    """合成经典版小地图窗口（怀旧服客户端样式，素材从游戏内截图 1:1 抠出）。

    结构（101030400 游戏截图 + canvas 127×134 模板匹配 err1.9 实测）：
    白底标题栏(20px) → 浅蓝名字条(46px，太阳图章+黑字，底 3 行浅色渐变) →
    canvas 行(ch+17：顶蓝线1 + 浅色渐变5 + 背板9 + canvas + 背板2) →
    渐变底条(14px)；最右另有 1px 半透明阴影列(~50% 黑)。
      window_width = max(143, 48+max(区域名宽,地图名宽)+10, canvas宽+16)
      canvas_x     = max(10, (window_width-canvas宽)/2)   # 右边距比左边距小
    左外沿 4 列 = 浅灰184|灰线102|白|蓝线（各带同位）；右缘 2 列 = 蓝线|白。
    MaxMap/c 背板 = 黑 ~65% 半透明、铺满整个内窗（canvas 透明天空与空白区同色，
    合成到画面上时透出场景，和游戏内一致）。img 是已画好标记的 canvas RGBA 图。
    """
    from PIL import Image, ImageDraw, ImageFont
    cw, chh = img.size
    C = MMUI_STATIC / "cls"
    try:                                      # 客户端字体是 12px 宋体位图字
        f = ImageFont.truetype("C:/Windows/Fonts/simsun.ttc", 12)
    except Exception:                         # noqa: BLE001
        f = None
    _m = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    def tw(s: str) -> float:
        if not s:
            return 0
        try:
            return _m.textlength(s, font=f)
        except Exception:                     # noqa: BLE001
            return len(s) * 12

    winW = int(max(143, 48 + max(tw(street), tw(name)) + 10, cw + 16))
    ox = max(10, (winW - cw) // 2)
    rowh = chh + 17                             # 顶蓝线1 + 渐变5 + 背板9 + 内容 + 背板2
    winH = 20 + 46 + rowh + 14
    win = Image.new("RGBA", (winW + 1, winH), (0, 0, 0, 0))   # +1 = 右侧阴影列
    # 顶条：左段贴左、右段贴右（71px，已去掉烤进场景的最右列）、中间补渐变
    tb = Image.new("RGBA", (winW, 20), (255, 255, 255, 255))
    tb.alpha_composite(Image.open(C / "tb-left.png").convert("RGBA"), (0, 0))
    tb.alpha_composite(Image.open(C / "tb-right.png").convert("RGBA"), (winW - 71, 0))
    if winW > 143:
        mid = Image.new("RGBA", (winW - 143, 20))
        d = ImageDraw.Draw(mid)
        grad = [(102, 102, 110), (255, 255, 255), (85, 119, 153),
                (153, 187, 204), (136, 153, 187)]
        for y in range(20):
            c = grad[y] if y < 5 else ((136, 153, 187) if y == 19 else (255, 255, 255))
            d.line([(0, y), (mid.width, y)], fill=c + (255,))
        tb.alpha_composite(mid, (72, 0))
    win.alpha_composite(tb, (0, 0))
    # 名字条：图章（38×38 凹框+太阳，游戏实测 @ (7,3)）+ 两行黑字（带 1px 白投影）
    nb = Image.new("RGBA", (winW, 46), (153, 187, 204, 255))
    d = ImageDraw.Draw(nb)
    for y, c in ((43, (187, 204, 221)), (44, (204, 221, 221)), (45, (187, 204, 221))):
        d.line([(4, y), (winW - 3, y)], fill=c + (255,))
    nb.alpha_composite(Image.open(C / "mark-sun.png").convert("RGBA"), (7, 3))
    if street:
        d.text((52, 7), street, fill=(255, 255, 255, 255), font=f)
        d.text((51, 6), street, fill=(0, 0, 0, 255), font=f)
    if name:
        d.text((52, 25), name, fill=(255, 255, 255, 255), font=f)
        d.text((51, 24), name, fill=(0, 0, 0, 255), font=f)
    _edge_cols(d, winW, 0, 45)                # 左外沿 4 列 + 右缘 2 列
    win.alpha_composite(nb, (0, 20))
    # canvas 行：背板(黑~65% 半透明)铺满内窗 → 顶蓝线 → 5px 浅色渐变 →
    # 左右同位边列 → canvas 1:1 @ (ox,15)
    row = Image.new("RGBA", (winW, rowh), (0, 0, 0, 0))
    d = ImageDraw.Draw(row)
    d.rectangle([7, 6, winW - 4, rowh - 1], fill=(0, 0, 0, 166))
    d.line([(4, 0), (winW - 3, 0)], fill=(85, 119, 153, 255))
    for y, c in ((1, (204, 204, 204)), (2, (187, 187, 187)), (3, (204, 204, 204)),
                 (4, (238, 238, 238)), (5, (255, 255, 255))):
        d.line([(4, y), (winW - 3, y)], fill=c + (255,))
    for x in (4, 5, 6):                       # 左白衬条（蓝线/渐变行除外）
        d.line([(x, 6), (x, rowh - 1)], fill=(255, 255, 255, 255))
    _edge_cols(d, winW, 0, rowh - 1)
    row.alpha_composite(img, (ox, 15))
    win.alpha_composite(row, (0, 66))
    # 底条（143×14 素材 = 游戏截图 y248..261 1:1，含左右圆角；宽窗整体拉伸）
    win.alpha_composite(Image.open(C / "bottom.png").convert("RGBA").resize((winW, 14)), (0, winH - 14))
    # 最右 1px 半透明阴影列（实测 ~50% 黑，盖在场景上）
    d = ImageDraw.Draw(win)
    d.line([(winW, 0), (winW, winH - 1)], fill=(0, 0, 0, 128))
    return win


def _edge_cols(d: "ImageDraw", winW: int, y0: int, y1: int) -> None:
    """窗口纵向边列（名字条/canvas 行共用，照游戏截图 1:1）：
    左外沿 4 列 = 浅灰184|灰线102|白|蓝线；右缘 2 列 = 蓝线|白。"""
    for x, c in ((0, (184, 184, 184)), (1, (102, 102, 110)), (2, (255, 255, 255)),
                 (3, (85, 119, 153)), (winW - 2, (85, 119, 153)), (winW - 1, (255, 255, 255))):
        d.line([(x, y0), (x, y1)], fill=c + (255,))


def _minimap_composite(export: dict, out: Path, zoom: int = 2) -> Path:
    """画出完整经典版小地图窗口（框体+canvas+经典标记）→ PNG。

    框体 = 怀旧服客户端样式（webui/assets/minimap/cls 分层素材），
    标记 = MapHelper.img/minimap 经典版精灵（portal 8×8 / npc 4×7；经典版
    不画怪物标记）。canvas 背板半透明 → 导出 RGBA，叠加到别的画面上时
    透出底图（和游戏内一致）。先按 1x 原生布局合成，再整窗 NEAREST 放大 zoom 倍。
    """
    from PIL import Image
    mm = export.get("minimap")
    if not mm:
        raise CliError("这张图没有 miniMap（hideMinimap=1 或该版本数据缺失）", 1)
    if not (MMUI_STATIC / "cls" / "tb-left.png").is_file():
        raise CliError("经典版小地图素材缺失（webui/assets/minimap/cls）", 1)
    img = Image.open(mm["img_file"]).convert("RGBA")

    def mark(name: str, x: int, y: int) -> None:
        p = Image.open(MMUI_STATIC / "cls" / "marks" / f"{name}.png").convert("RGBA")
        img.alpha_composite(p, (int(x) - p.width // 2, int(y)))

    for n in mm.get("npcs", []):
        mark("npc", n["x"], n["y"])
    for p in mm.get("portals", []):
        mark("portal", p["x"], p["y"])
    win = _cls_window(img, export.get("street_name", ""), export.get("name", ""))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if zoom > 1:
        win = win.resize((win.width * zoom, win.height * zoom), Image.NEAREST)
    win.save(out)                                   # 保留 alpha（canvas 背板半透明）
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
    from PIL import Image as _I
    w, h = _I.open(png).size                 # 窗口实际尺寸（1x 布局 × zoom）
    return {"map": mid,
            "name": d.get("name", ""), "street_name": d.get("street_name", ""),
            "image": str(png),
            "w": w,
            "h": h,
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
    if not _map_cache_fresh(outdir):
        # 同 map-export：覆盖式刷新，不做整目录删除
        outdir.parent.mkdir(parents=True, exist_ok=True)
        _run(["nxmap", mid, str(outdir)], _nx_dir(), timeout=600)
    if not (outdir / "map.json").exists():
        raise CliError(f"nxmap 没产出 map.json（这个 id 存在吗？{mid}）", 1)
    _map_cache_stamp(outdir)
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
    mobnx = _nx_dir() / "Mob.nx"
    meta = {m["id"]: m for m in mob_meta(mobnx)} if mobnx.exists() else {}
    kinds = sorted({str(l.get("id", "")).lstrip("0") for l in life})
    _st = _mob_stat_table()

    def _kind_lv(k: str) -> int:
        lv = meta.get(k, {}).get("level") or 0
        if lv <= 0 and k in _st:
            lv = _st[k][0]
        return lv

    return {"id": mid,
            "bounds": {"x": cx, "y": cy, "w": cw, "h": chh},
            "world": {"x": minx, "y": miny, "w": bw, "h": bh},
            "scale": s,
            "preview_px": {"w": round(bw * s), "h": round(bh * s)},
            "life_count": len(life),
            "life_kinds": [{"id": k,
                            "name": meta.get(k, {}).get("name") or mob_names().get(k) or "",
                            "lv": _kind_lv(k),
                            "tier": meta.get(k, {}).get("tier", "normal")} for k in kinds],
            "mob_lv": ([min(lvs), max(lvs)]
                       if (lvs := [v for v in map(_kind_lv, kinds) if v > 0]) else None),
            "footholds": len(mj.get("foothold", [])),
            "ladders": len(mj.get("ladderRope", [])),
            # 地面段 [x1,y1,x2,y2]：可站立区域（含斜坡），导航/跳跃规划用
            "foothold_segs": [[f["x1"], f["y1"], f["x2"], f["y2"]]
                              for f in mj.get("foothold", [])],
            # 绳梯段 [x,y1,y2,l]：l=1 梯子 / 0 绳子
            "ladder_rope": [[lr["x"], lr["y1"], lr["y2"], lr.get("l", 0)]
                            for lr in mj.get("ladderRope", [])]}


def c_clear_cache() -> dict:
    if _job["running"] or _clsjob["running"]:
        raise CliError("转换/建库正在进行中，等它完成再清除", 1)
    # 彻底清理：先把各子目录秒级改名挪进 trash，再**同步**把 trash 删干净——
    # 不能只挪不删，否则几十 GB 残在 trash 里占着盘（之前就是这么残留的）。
    for d in list(CACHE.iterdir()) if CACHE.exists() else []:
        if d.name == "trash":
            continue
        drop_dir(d) if d.is_dir() else (d.unlink() if d.is_file() else None)
    _sweep_trash_now()              # 真正删数据（几十 GB 要几分钟），删完才返回
    CACHE.mkdir(exist_ok=True)
    _lists.clear()
    # 顺手清掉记住的工作区：数据都清了，下次启动不该还默认进旧目录
    global _ws
    _ws = None
    _save_ws()
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


# ---------------------------------------------------------------- web 适配层
# 同一套 API 约定（wztool web 供页面/本地程序调用；自动化请优先用子命令）。
# stdlib http.server，无第三方依赖。

import http.server
import socket
import urllib.parse
import urllib.request


class _Server(http.server.ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 允许两个进程绑同一个端口，新旧服务各接一半请求
    # → 页面一会儿新一会儿旧（"功能怎么不见了"）。关掉，第二个实例直接失败。
    allow_reuse_address = False
    daemon_threads = True


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
    _POST_ONLY = {"/api/browse", "/api/pickdir", "/api/scan", "/api/convert", "/api/reconvert",
                  "/api/clsbuild", "/api/clsimgsearch",
                  "/api/mob/export",
                  "/api/char/render", "/api/char/export",
                  "/api/map/export", "/api/map/imgsearch", "/api/export/clear-cache"}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        if path == "/" or path == "/index.html":
            self._send_file(WEBUI / "cli.html", "text/html; charset=utf-8")
        elif path == "/api/state":
            self._api(c_state)
        elif path == "/api/ui":
            self._api(c_ui)
        elif path == "/api/job":
            self._api(c_job)
        elif path == "/api/progress":
            self._api(c_progress)
        elif path == "/api/mobs":
            self._api(c_mobs)
        elif path == "/api/mobmaps":
            self._api(c_mobmaps)
        elif path.startswith("/api/mob/icon/"):
            self._api(c_mob_icon, path.rsplit("/", 1)[-1])
        elif path.startswith("/api/mob/") and path.endswith("/maps"):
            self._api(c_mob_maps, path[len("/api/mob/"):-len("/maps")])
        elif path == "/api/maps":
            self._api(c_maps)
        elif path == "/api/skill/jobs":
            self._api(c_skill_jobs)
        elif path == "/api/skill/list":
            qs = urllib.parse.parse_qs(u.query)
            self._api(c_skill_list, (qs.get("job") or [""])[0])
        elif path == "/api/skill/frames":
            qs = urllib.parse.parse_qs(u.query)
            self._api(c_skill_export, (qs.get("job") or [""])[0],
                      (qs.get("skill") or [""])[0])
        elif path == "/api/dmg/sets":
            self._api(c_dmg_sets)
        elif path == "/api/char/parts":
            qs = urllib.parse.parse_qs(u.query)
            self._api(c_char_parts, (qs.get("cat") or ["hair"])[0])
        elif path == "/api/char/players":
            self._api(c_char_players)
        elif path == "/api/cls/path":
            qs = urllib.parse.parse_qs(u.query)
            vals = []
            for k in ("fx", "fy", "tx", "ty"):
                try:
                    vals.append(float((qs.get(k) or ["0"])[0]))
                except ValueError:
                    vals.append(0.0)
            self._api(c_cls_path, (qs.get("id") or [""])[0], *vals)
        elif path.startswith("/api/char/icon/"):
            rest = path[len("/api/char/icon/"):]
            cat, _, iid = rest.partition("/")
            try:
                self._send_file(c_char_icon(cat, iid), "image/png")
            except CliError as e:
                self._send_json(404, {"detail": str(e)})
            except Exception:                 # noqa: BLE001
                self._send_json(404, {"detail": "no icon"})
        elif path == "/api/map/imgindex":
            self._api(c_map_imgindex)
        elif path == "/api/clsimgindex":
            self._api(c_cls_imgindex)
        elif path == "/api/cls/maps":
            self._api(c_cls_maps)
        elif path == "/api/cls/icons":
            self._api(c_cls_icons)
        elif path == "/api/cls/drops":
            self._api(c_cls_drops)
        elif path.startswith("/api/cls/icon/"):
            try:
                self._send_file(c_cls_icon_png(path.rsplit("/", 1)[-1]),
                                "image/png")
            except CliError as e:
                self._send_json(404, {"detail": str(e)})
            except Exception:                 # noqa: BLE001
                self._send_json(404, {"detail": "no icon"})
        elif path == "/api/cls/tables":
            self._api(c_cls_tables)
        elif path.startswith("/preview/"):
            self._send_file(CACHE / urllib.parse.unquote(path[len("/preview/"):]),
                            "image/png")
        elif path.startswith("/exports/"):
            self._send_file(EXPORTS / urllib.parse.unquote(path[len("/exports/"):]),
                            "image/png")
        elif path.startswith("/webui/"):
            mime = {"png": "image/png", "css": "text/css", "js": "text/javascript",
                    "html": "text/html", "txt": "text/plain"}.get(path.rsplit(".", 1)[-1], "application/octet-stream")
            self._send_file(WEBUI / urllib.parse.unquote(path[len("/webui/"):]), mime)
        elif path in self._POST_ONLY:
            self._send_json(405, {"detail": f"Method Not Allowed: {path} 是 POST 接口"})
        else:
            self._send_json(404, {"detail": f"no route: {path}"})

    def do_POST(self):
        path = self.path.split("?")[0]
        b = self._body()
        if path == "/api/browse":
            self._api(c_browse, b.get("path") or "")
        elif path == "/api/pickdir":
            self._api(c_pickdir, str(b.get("path") or ""))
        elif path == "/api/scan":
            if not b.get("path"):
                self._send_json(400, {"detail": "path 必填"})
                return
            self._api(c_workspace, b["path"])
        elif path == "/api/convert":
            self._api(_web_convert, b.get("files") or [], bool(b.get("force")))
        elif path == "/api/reconvert":
            self._api(_web_reconvert)
        elif path == "/api/clsbuild":
            self._api(_web_clsbuild)
        elif path == "/api/clsimgsearch":
            img = b.get("image")
            if not img:
                self._send_json(400, {"detail": "image 必填（base64 截图）"})
                return
            self._api(c_cls_imgsearch, str(img))
        elif path == "/api/mob/export":
            self._api(c_mob_export, str(b.get("raw") or ""), bool(b.get("preview")))
        elif path == "/api/char/render":
            self._api(c_char_render, b)
        elif path == "/api/skill/export":
            self._api(c_skill_export_to, str(b.get("job") or ""),
                      str(b.get("skill") or ""))
        elif path == "/api/dmg/sets":
            self._api(c_dmg_sets)
        elif path == "/api/dmg/export":
            self._api(c_dmg_export_to, str(b.get("set") or ""))
        elif path == "/api/char/export":
            self._api(c_char_export, b)
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
                      str(b["seed"]) if b.get("seed") else None,
                      no_bg=bool(b.get("no_bg")),
                      cls=bool(b.get("cls")),
                      drops=bool(b.get("drops")),
                      players=bool(b.get("players")))
        elif path == "/api/map/imgsearch":
            img = b.get("image")
            if not img:
                self._send_json(400, {"detail": "image 必填（base64 截图）"})
                return
            self._api(c_map_imgsearch, str(img))
        elif path == "/api/export/clear-cache":
            self._api(c_clear_cache)
        elif path in ("/api/state", "/api/job", "/api/mobs", "/api/maps",
                      "/api/char/parts", "/api/char/players",
                      "/api/skill/jobs", "/api/skill/list",
                      "/api/skill/frames", "/api/dmg/sets") \
                or path.startswith("/api/mob/icon/") \
                or (path.startswith("/api/mob/") and path.endswith("/maps")):
            self._send_json(405, {"detail": f"Method Not Allowed: {path} 是 GET 接口"})
        else:
            self._send_json(404, {"detail": f"no route: {path}"})


def _web_convert(names: list[str], force: bool) -> dict:
    """web 版转换：后台线程跑，前端轮询 /api/job（和页面现有约定一致）。

    国服模式下一键全包：缺的 nx 依次转 + 怀旧服小地图库没建好就顺手建上
    （前端不再有单独的「建库」按钮，一个按钮把数据备齐）。
    """
    _need_tools()
    todo: list[Path] = []
    cls_todo = False
    dmg_todo = False
    if _cn_mode() is not None:
        paths = {"String.nx": CN_NX / "String.nx", "Map.nx": CN_NX / "Map.nx",
                 "Mob.nx": CN_NX / "Mob.nx", "Character.nx": CN_NX / "Character.nx",
                 "Skill.nx": CN_NX / "Skill.nx"}
        # 只转用户点名的那几项（names 为空才退回「缺什么转什么」）
        todo_names = [n for n in ("String.nx", "Map.nx", "Mob.nx",
                                  "Character.nx", "Skill.nx")
                      if (not names or n in names) and not paths[n].exists()]
        # 怀旧服小地图库（mxdclassic）没建好 → 并进这次任务，转完自动建
        cls_todo = _cls_w_dir() is not None and not _cls_ready() and not _clsjob["running"]
        # 怀旧服 WZJS 大图库（1750 张可渲染地图）没建好 → 同样并进这次任务
        wzjs_todo = (_cls_w_dir() is not None and not _wzjs_ready()
                     and not _wzjsjob["running"])
        # 伤害数字库（Effect/BasicEff 的 No* 数字集）没建好 → 一样并进这次任务
        dmg_todo = _dmg_effect_dir() is not None and not _dmg_ready()
        # 全库出图没跑完 → 同样并进这次任务：转换完自动把每张图的预览缓存
        # 批量建好（map-all 断点续跑），之后浏览任何图都秒开
        if (_nx_dir() / "Map.nx").exists():
            mapall_todo = bool(_map_all_pending())
        else:
            mapall_todo = "Map.nx" in todo_names     # 本次会转出 Map.nx → 全量必跑
        if not todo_names and not cls_todo and not wzjs_todo \
                and not dmg_todo and not mapall_todo:
            return {"started": False, "message": "国服数据已转换完成（.cache\\cn）"}
        cn_todo = todo_names
    else:
        ws = _need_ws()
        todo = _convert_pick(ws, names, force)
        if not todo:
            return {"started": False, "message": "选中的都已是 .nx（要重转勾「强制重转」）"}
        cn_todo = None
        mapall_todo = False
    total = len(cn_todo) if cn_todo is not None else len(todo)
    if cls_todo:
        total += 1                      # 小地图库算一项，进度条能走满
    if wzjs_todo:
        total += 1                      # WZJS 大图库同上
    if dmg_todo:
        total += 1                      # 伤害数字库同上
    if mapall_todo:
        total += 1                      # 全库出图算一项（进度用它自己的 frac 走满）
    with _lock:
        if _job["running"]:
            raise CliError("已有转换在跑，等它完成", 1)
        _job.update(id=_job["id"] + 1, running=True, log=[], done=0,
                    total=total, stage="", frac=0.0, error="")

    def work():
        try:
            if cn_todo is not None:
                if cn_todo:
                    c_convert_cn(lambda s: _job["log"].append(s), cn_todo,
                                 prog=lambda stage, done, frac=0.0:
                                 _job.update(stage=stage, done=done, frac=frac))
                _job.update(stage="", done=len(cn_todo), frac=0.0)
                if cls_todo:
                    _job["log"].append("转换完成，接着构建怀旧服小地图库…")
                    _cls_build()
                    _job["done"] += 1
                if wzjs_todo:
                    _job["log"].append(
                        "接着构建怀旧服大图库（WZJS 1750 张地图 → 可渲染缓存，"
                        "已转过的自动跳过）…")
                    _wzjs_build(log=lambda s: _job["log"].append(s))
                    if _wzjsjob["error"]:
                        _job["log"].append(f"怀旧服大图库出错：{_wzjsjob['error']}")
                    _job["done"] += 1
                if dmg_todo:
                    _job["log"].append("接着提取伤害数字库（Effect/BasicEff）…")
                    _dmg_build(lambda s: _job["log"].append(s),
                               stage=lambda s: _job.update(stage=s, frac=0.0))
                if mapall_todo:
                    _job["log"].append(
                        "接着全库出图（每张图的预览缓存；跑完浏览任何图都秒开，"
                        "可中断、重跑续传）…")
                    try:
                        c_map_all(2,
                                  log=lambda s: _job["log"].append(s),
                                  prog=lambda stage, frac:
                                  _job.update(stage=stage, frac=frac))
                    finally:
                        _job.update(stage="", frac=0.0)
                _job.update(stage="", done=_job["total"], frac=0.0)
            else:
                for wz in todo:
                    _convert_one(wz, lambda s: _job["log"].append(s))
                    _job["done"] += 1
            _job["log"].append("全部完成 ✔")
            # 数据刚变 —— 顺手把小地图库（找图用的 PNG 目录 + 签名索引）预热出来，
            # 别等用户第一次点「找图」才现解压（那一次要等一分钟）
            minimap_warmup(delay=1.0)
        except Exception as exc:              # noqa: BLE001
            _job["error"] = str(exc)
            _job["log"].append(f"出错：{exc}")
        finally:
            _job["running"] = False
            _lists.clear()

    threading.Thread(target=work, daemon=True).start()
    return {"started": True, "total": total, "job": _job["id"]}


def c_progress() -> dict:
    """WebUI 进度条数据源：转换 / 小地图索引 / 兜底生成 / 怀旧服小地图库 四段进度。"""
    try:
        ready = _minimap_ready()
    except Exception:                          # noqa: BLE001
        ready = False
    return {
        # 重新转换时 _job 会一直 running，但真正的进度在 minimap / auto 段，
        # 这里让位，否则前端会卡在「转换 0/0」的无限转圈上。
        "convert": {"running": _job["running"]
                    and not (_mmjob["running"] or _mmauto["running"]),
                    "done": _job["done"],
                    "total": _job["total"], "stage": _job.get("stage", ""),
                    "frac": _job.get("frac", 0.0),
                    "error": _job["error"]},
        "minimap": {"running": _mmjob["running"], "done": _mmjob.get("done", 0),
                    "total": _mmjob.get("total", 0), "stage": _mmjob.get("stage", ""),
                    "error": _mmjob["error"]},
        "auto": {"running": _mmauto["running"], "done": _mmauto["done"],
                 "total": _mmauto["total"], "stage": "兜底小地图（1×1 退化图）"},
        "cls": {"running": _clsjob["running"], "done": _clsjob.get("done", 0),
                "total": _clsjob.get("total", 0), "stage": _clsjob.get("stage", ""),
                "error": _clsjob["error"], "ready": _cls_ready()},
        "clsmap": {"running": _wzjsjob["running"], "done": _wzjsjob.get("done", 0),
                   "total": _wzjsjob.get("total", 0), "stage": _wzjsjob.get("stage", ""),
                   "error": _wzjsjob["error"], "ready": _wzjs_ready()},
        "ready": ready,
    }


def _web_reconvert() -> dict:
    """WebUI「重新转换」：补齐缺失的国服 .nx，然后强制重建全库小地图库
    （mini-all + 兜底生成 + 识别签名）——把 .cache\\minimap 删掉也能一键恢复。
    后台线程跑，前端轮询 /api/progress。"""
    if _cn_mode() is None:
        raise CliError("「重新转换」只对国服官方数据源生效", 1)
    _need_tools()
    with _lock:
        if _job["running"]:
            raise CliError("已有转换任务在跑，等它完成", 1)
        if _mmjob["running"] or _mmauto["running"]:
            raise CliError("小地图索引正在构建，等它完成", 1)
        _job.update(id=_job["id"] + 1, running=True, log=[], done=0, total=1,
                    error="", stage="检查数据")
    CN_NX.mkdir(parents=True, exist_ok=True)

    def work() -> None:
        log = lambda s: _job["log"].append(s)     # noqa: E731
        try:
            need = [n for n, p in (("String.nx", CN_NX / "String.nx"),
                                   ("Map.nx", CN_NX / "Map.nx"),
                                   ("Mob.nx", CN_NX / "Mob.nx"),
                                   ("Character.nx", CN_NX / "Character.nx"),
                                   ("Skill.nx", CN_NX / "Skill.nx")) if not p.exists()]
            if need:
                _job.update(stage="转换", done=0, total=len(need))
                log("转换缺失数据：" + "、".join(need))
                c_convert_cn(log)
                _job["done"] = len(need)
            else:
                log(".cache\\cn 的 .nx 都在，跳过转换，直接重建小地图库")
            # 强制重建：删掉三处戳，让 mini-all / 兜底 / 签名都重跑
            for name in ("index.meta.json", "auto.meta.json", "thumbs.npz"):
                f = MMIDX / name
                if f.exists():
                    f.unlink()
            _job.update(stage="小地图索引", done=0, total=0)
            log("重建全库小地图索引（mini-all，约 1.4 万张）…")
            _minimap_build()
            if _mmjob["error"]:
                raise RuntimeError(_mmjob["error"])
            _job.update(stage="兜底小地图", done=0, total=1)
            log("生成退化 canvas 的兜底小地图 …")
            _minimap_fallback_build()
            _job.update(stage="", done=1, total=1)
            log("重新转换完成 ✔（小地图库 + 找图索引已就绪）")
        except Exception as exc:               # noqa: BLE001
            _job["error"] = str(exc)
            _job["log"].append(f"出错：{exc}")
        finally:
            _job["running"] = False
            _lists.clear()

    threading.Thread(target=work, daemon=True).start()
    return {"started": True, "job": _job["id"]}


def _port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _is_wztool(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ui", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def cmd_web(args) -> int:
    port = args.port

    # 已经有 wztool 在跑：直接复用（双击第二次不会出现两个服务抢一个端口）
    if _port_busy(port) and _is_wztool(port):
        url = f"http://127.0.0.1:{port}/"
        print(f"已有 wztool 服务在跑：{url} —— 直接打开页面，不再起第二个进程。")
        if not args.no_open:
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        return 0
    # 端口被别的程序占了（比如 sunshine 串流服务常驻 8000）：自动顺延找空位，
    # 不再直接报错退出——双击 exe 闪退十有八九是这个。
    if _port_busy(port):
        for _ in range(50):
            port += 1
            if not _port_busy(port):
                break
        else:
            raise CliError(f"端口 {args.port} 起连续 50 个都被占用了，"
                           f"手动指定：wztool web --port 9000", 1)
        print(f"端口 {args.port} 被别的程序占用，自动改用 {port}")
    url = f"http://127.0.0.1:{port}/"
    try:
        srv = _Server(("127.0.0.1", port), _Handler)
    except OSError as e:
        raise CliError(f"端口 {port} 起不来（{e}）；试试 wztool web --port 9000", 1)
    print(f"wztool web 已启动: {url}  （CLI 版页面；Ctrl+C 退出）")
    # 启动即预热小地图库（找图用）：用户点「找图」时一般已经建好，
    # 不用干等一分钟；延迟几秒让地图列表/接口先响应。
    minimap_warmup(delay=3.0)
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


def cmd_char_parts(a) -> int:
    _out(c_char_parts(a.cat), a.json)
    return 0


def cmd_char_export(a) -> int:
    spec = {k: getattr(a, k) for k in CHAR_SLOTS if getattr(a, k, None)}
    spec["skin"] = a.skin
    spec["actions"] = [x for x in (a.actions or "stand1,walk1").split(",") if x]
    d = c_char_export(spec)
    if a.json:
        _out(d, True)
    else:
        print(f"已导出 {d['count']} 帧 → {d['dir']}")
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
                     view=view, seed=getattr(a, "seed", None),
                     no_bg=bool(getattr(a, "no_bg", False)),
                     drops=bool(getattr(a, "drops", False)),
                     players=bool(getattr(a, "players", False)))
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
    return 0


def cmd_map_all(a) -> int:
    d = c_map_all(a.workers, log=print)
    if a.json:
        _out(d, True)
    else:
        if not d.get("started"):
            print(f"全库 {d['total']} 张都已出过图，无需重跑（清缓存或删 .build/mapall 可强制）")
        else:
            print(f"全库出图完成: 可显示 {d['ok']}/{d['total']} · 失败 {d['fail']} · "
                  f"耗时 {d['elapsed_min']} 分钟；缓存目录 .cache\\map\\<id>")
    return 0


def cmd_clear_cache(a) -> int:
    _out(c_clear_cache(), a.json)
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
  4. wztool mobs --json                        # 怪物列表（图鉴/素材浏览）
  5. wztool mob-export 0100100                 # → .exports/mobs/0100100/（逐帧 PNG + manifest.csv）
  6. wztool maps --json                        # 地图列表（含 life_maps=有怪的图）
  7. wztool map-info 000050000 --json          # 地图元信息（不渲染，含可导窗口范围）
  8. wztool map-export 000050000 --mobs        # → .exports/maps/000050000/（map.json + 贴图 + 窗口图）
  9. wztool map-minimap 000050000              # → 游戏内小地图 PNG

约定:
  - 所有 id 兼容带/不带前导 0（"0100100" 与 "100100" 等价）。
  - 链接图/链接怪（WZ info/link 机制）自动顺链解析，返回 JSON 里有 link 字段。
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

    p = sub.add_parser("workspace", help="设置工作区（放 .wz/.nx 的目录，或国服客户端目录）",
                       epilog="示例: wztool workspace D:/MapleStory/nxrun\n"
                              "      wztool workspace \"C:/Program Files/上海数龙科技有限公司/冒险岛online/mxdclassic\"  # 国服数据源")
    p.add_argument("path", help="目录绝对路径")
    _j(p); p.set_defaults(fn=cmd_workspace)

    p = sub.add_parser("browse", help="列出目录内容（path 省略 = 磁盘根）")
    p.add_argument("path", nargs="?", default="")
    _j(p); p.set_defaults(fn=cmd_browse)

    p = sub.add_parser("convert", help="把 .wz 转成 .nx（国服模式=全量转换到 .cache\\cn）",
                       epilog="示例: wztool convert Mob.wz Map.wz --force")
    p.add_argument("files", nargs="*", default=[], help=".wz 文件名（相对工作区；国服模式忽略）")
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

    p = sub.add_parser("char-parts", help="某个部位的人物素材列表（skin/hair/face/cap/coat/…）",
                       epilog="配合 char-export 拼一套选装；id 都是 8 位（如 00030000）。")
    p.add_argument("cat", nargs="?", default="hair",
                   help="部位：" + "/".join(CHAR_CATS) + "（默认 hair）")
    _j(p); p.set_defaults(fn=cmd_char_parts)

    p = sub.add_parser("char-export", help="按选装导出人物动画帧 PNG（默认 stand1+walk1）",
                       epilog="例：wztool char-export --hair 00030000 --face 00020000 "
                              "--coat 01040000 --pants 01060000 --shoes 01070000\n"
                              "输出 .exports/chars/<slug>/：逐帧 PNG + manifest.csv + look.json")
    p.add_argument("--skin", type=int, default=0, help="肤色编号（0=Light，见 char-parts skin）")
    for _s in CHAR_SLOTS:
        p.add_argument(f"--{_s}", default="", metavar="ID", help=f"{_s} 素材 id")
    p.add_argument("--actions", default="stand1,walk1", help="逗号分隔的动作名")
    _j(p); p.set_defaults(fn=cmd_char_export)

    p = sub.add_parser("mob-frames", help="列出一只怪的动画列表与帧数（stand/move/die1…）")
    p.add_argument("id", help="怪 id")
    _j(p); p.set_defaults(fn=cmd_mob_frames)

    p = sub.add_parser("mob-export", help="导出一只怪的全部动画帧 PNG + manifest.csv",
                       epilog="输出: .exports/mobs/<id>/（--preview 时进 .cache/mob/<id>/）。\n"
                              "manifest.csv 列: mob,anim,frame,file,w,h,ox,oy,delay；\n"
                              "ox/oy=锚点(脚底中心)，把各帧对到同一画布用它。")
    p.add_argument("id", help="怪 id（链接怪自动转向目标怪）")
    p.add_argument("--preview", action="store_true", help="只出预览缓存，不进 .exports")
    _j(p); p.set_defaults(fn=cmd_mob_export)

    p = sub.add_parser("mob-icon", help="取一只怪的 stand 帧（路径/尺寸/锚点）",
                       epilog="给刷怪点叠怪、做图鉴头像用；ox/oy=脚底中心锚点。")
    p.add_argument("id", help="怪 id")
    _j(p); p.set_defaults(fn=cmd_mob_icon)

    p = sub.add_parser("map-export", help="导出一张地图（map.json + 贴图 + 渲染预览图）",
                       epilog="输出: .exports/maps/<id>/（--preview 时进 .cache/map/<id>/）。\n"
                              "map.json: life(刷怪表)/foothold(地面)/ladderRope(绳梯)/portal。\n"
                              "--mobs: 预览图直接把怪随机画上去（preview_mobs.png）。\n"
                              "--json 里的 life_points+overlay 可在任何客户端复现刷怪点。")
    p.add_argument("id", help="9 位地图 id（链接图自动转向目标图）")
    p.add_argument("--preview", action="store_true", help="只出预览缓存，不进 .exports")
    p.add_argument("--mobs", action="store_true",
                   help="预览图合成怪物（沿地面随机散布、贴地不悬空；动作状态也随机：站/走/攻击/受击/死亡等，"
                        "默认每次随机，--seed 可复现；大 BOSS 一律不合成）")
    p.add_argument("--drops", action="store_true",
                   help="再撒一层掉落物（preview_drops.png）：只掉本图怪的掉落表里的东西、"
                        "掉落点跟刷怪点走；金币 4 档 + chance 加权道具（掉落表来自服务端）")
    p.add_argument("--players", action="store_true",
                   help="再合入 1~5 个随机玩家（preview_players.png）：随机肤色/发型/脸型/装备"
                        "+随机名字，沿 foothold 贴地、50% 随机朝向；名字画进图（--seed 可复现）")
    p.add_argument("--seed", metavar="S", help="配合 --mobs：固定随机种子（同 seed 同布局；缺省每次随机）")
    p.add_argument("--density", type=int, default=1, metavar="N",
                   help="配合 --mobs：怪物密度倍数（1-10，默认 1；每个刷怪点放 N 只，左右散开）")
    p.add_argument("--no-bg", action="store_true",
                   help="不画背景（天幕/云/山等 back 层全跳过，画布透明），只出地形+建筑+物件；"
                        "输出 window_nobg.png")
    p.add_argument("--view", metavar="X,Y",
                   help="导出窗口左上角的世界坐标（默认地图正中）。导出图固定 1366×768 —— "
                        "和客户端画面同尺寸，只此一种；含负数时必须用 = 形式，如 --view=-1500,300")
    _j(p); p.set_defaults(fn=cmd_map_export)

    p = sub.add_parser("map-all",
                       help="全库出图：批量建好所有地图的预览缓存（之后浏览任何图都秒开）",
                       epilog="与 WebUI「预览」同一条链路（nxmap → map.json+贴图 → preview.png），\n"
                              "跑完等于把全库「能否正常显示」也验了一遍。断点续跑：\n"
                              "已完成记录在 .build/mapall/results.jsonl，中断后重跑自动跳过。\n"
                              "工作区第一次「转换」时也会自动跑这一步（进度在转换任务里显示）。\n"
                              "workers 默认 2：8 个会抢 23G Map.nx 和 CPU，网页浏览新图会卡几分钟。")
    p.add_argument("--workers", type=int, default=2, metavar="N",
                   help="并发进程数（默认 2；浏览无感，8 个抢资源）")
    _j(p); p.set_defaults(fn=cmd_map_all)

    p = sub.add_parser("map-minimap",
                       help="渲染游戏内小地图（miniMap canvas + 标记）→ PNG",
                       epilog="与客户端 UIMiniMap 同款算法：WZ 预烘焙的 miniMap/canvas\n"
                              "按 2^mag 换算坐标，叠上传送门(蓝圈)/NPC(黄点)/刷怪点(红橙品红)。\n"
                              "数据走 map-export 缓存（.cache/map/<id>/），二次调用秒回。")
    p.add_argument("id", help="9 位地图 id（链接图自动转向目标图）")
    p.add_argument("--zoom", type=int, default=2, metavar="N",
                   help="canvas 放大倍数（1-8，默认 2；1x=WZ 原始尺寸）")
    p.add_argument("--out", default="", metavar="PATH", help="输出 PNG 路径（默认 .exports/maps/<id>/minimap.png）")
    p.add_argument("--preview", action="store_true", help="数据只进预览缓存，不落 .exports")
    _j(p); p.set_defaults(fn=cmd_map_minimap)

    p = sub.add_parser("clear-cache", help="清空 .cache 预览缓存（.exports 不动）")
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
    # 不带任何参数 = 双击 wztool.exe 的场景：直接启动 Web 页面（等效 wztool web）
    if not argv and len(sys.argv) == 1:
        args = ap.parse_args(["web"])
    else:
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
