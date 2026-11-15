"""怀旧服 spritesheet 描述符解析与「WZ 路径 → 贴图」注册表。

数据源：12 个 spritesheet_*.bundle 里的 MonoBehaviour 描述符（IL2CPP 客户端，
无类型树）。描述符与它引用的贴图 **同 bundle**：贴图对象名 = `<m_Name>_<atlas_idx>`，
帧是贴图上的一个矩形区域。

描述符 raw 布局（2026-11-15 逆向，经 12 包全部描述符验证）::

    0x10-0x1B  m_Script PPtr（该类在所有包里都是 01 00 00 00 0f 10 bb fe …）
    0x1C       u32 名称长度
    0x20..     m_Name（贴图条目名，如 211040102 / EmotionEffect）
    (对齐)
    u32 WZ 路径长度；WZ 路径（如 Map/Map/Map2/211040102）
    u32 atlas 数 n；每项: [u32 哈希长度=32][32 字节哈希][8 字节保留(恒 0)]
    u32 帧数
    每帧: [u32 子路径长度][子路径][对齐]
          7 个值: atlas_idx, x, y, W, H, pivX, pivY
          （x,y 为贴图上区域左上角；piv 为归一化轴心，0.5=中心，可越界）
    尾部 12 字节保留

注册表键 = WZ 路径 + "/" + 帧子路径（与 WZJS type18 节点 full path 一致，
去掉 ``/$spritesheetitem`` 后缀即可查表）。
"""
from __future__ import annotations

import struct
from pathlib import Path

SCRIPT_SIG = bytes.fromhex("010000000f10bbfe")   # m_Script（fileId=1 高 4B + pathID 低 4B 头）


def parse_desc(raw: bytes) -> dict:
    """解析一个描述符。返回 {name, wzpath, atlases, frames}。

    frames: list[(atlas_idx, subpath, x, y, w, h, pivx, pivy)]
    解析失败抛 ValueError。
    """
    if len(raw) < 0x20 or raw[0x10:0x14] != SCRIPT_SIG[:4] or raw[0x14:0x18] != SCRIPT_SIG[4:]:
        raise ValueError("not a spritesheet descriptor")
    nl = struct.unpack_from("<I", raw, 0x1C)[0]
    name = raw[0x20:0x20 + nl].decode("utf-8", "replace")
    p = (0x20 + nl + 3) & ~3
    pl = struct.unpack_from("<I", raw, p)[0]
    p += 4
    wz = raw[p:p + pl].decode("utf-8", "replace")
    p = (p + pl + 3) & ~3
    n_atlas = struct.unpack_from("<I", raw, p)[0]
    p += 4
    atlases = []
    for _ in range(n_atlas):
        hl = struct.unpack_from("<I", raw, p)[0]
        p += 4
        atlases.append(raw[p:p + hl].decode("ascii", "replace"))
        p = (p + hl + 3) & ~3
        p += 8                     # 每 atlas 后 8 字节保留（恒 0）
    fc = struct.unpack_from("<I", raw, p)[0]
    p += 4
    frames = []
    for _ in range(fc):
        fl = struct.unpack_from("<I", raw, p)[0]
        p += 4
        sub = raw[p:p + fl].decode("utf-8", "replace")
        p = (p + fl + 3) & ~3
        v = struct.unpack_from("<7f", raw, p)
        p += 28
        frames.append((int(v[0]), sub, int(v[1]), int(v[2]),
                       int(v[3]), int(v[4]), v[5], v[6]))
    return {"name": name, "wzpath": wz, "atlases": atlases, "frames": frames,
            "end": p}               # end: 主结构结束位置（尾部保留字节之前）


def build_registry(w_dir: Path, progress=None) -> dict:
    """扫描全部 spritesheet_*.bundle，建立注册表。

    progress: 可选回调 (stage_str, done, total)。
    返回 {"<wzpath/framesub>": {b: bundle 文件名, t: 贴图对象名, x, y, w, h, px, py}}
    """
    import UnityPy                              # 延迟导入：只有建库时才需要

    bundles = sorted(w_dir.glob("spritesheet_*.bundle"),
                    key=lambda p: p.stat().st_size, reverse=True)
    reg: dict[str, dict] = {}
    dup = 0
    for bi, bpath in enumerate(bundles):
        if progress:
            progress(f"spritesheet {bi + 1}/{len(bundles)}：{bpath.name[12:22]}…", bi, len(bundles))
        env = UnityPy.load(str(bpath))
        for o in env.objects:
            if o.type.name != "MonoBehaviour":
                continue
            raw = o.get_raw_data()
            if len(raw) < 0x20:
                continue
            try:
                d = parse_desc(raw)
            except ValueError:
                continue
            for atlas_idx, sub, x, y, w, h, px, py in d["frames"]:
                key = f"{d['wzpath']}/{sub}" if sub else d["wzpath"]
                entry = {"b": bpath.name, "t": f"{d['name']}_{atlas_idx}",
                         "x": x, "y": y, "w": w, "h": h, "px": round(px, 4),
                         "py": round(py, 4)}
                if key in reg and reg[key] != entry:
                    dup += 1
                reg[key] = entry
    if dup and progress:
        progress(f"警告：{dup} 个键跨包重复", len(bundles), len(bundles))
    return reg
