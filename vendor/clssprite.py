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
          （x 为区域左边界；**y 是 Unity 底部原点**，自上而下裁时要换成
          H - y - h；piv 为归一化轴心，0.5=中心，可越界）
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

    贴图解析要点：bundle 里贴图对象名是 ``<m_Name>_<atlas_idx>``，但**会重名**
    （Map/Back/dryRock 与 Map/Tile/dryRock 各有一张 dryRock_0，内容完全不同，
    全库共 62 组）。所以注册表给每个条目记下贴图的 pathID（``p``）：重名时按
    「帧区域全部落在贴图内 + 区域实际有像素」逐个候选解码打分，选出正确的那张。

    progress: 可选回调 (stage_str, done, total)。
    返回 {"<wzpath/framesub>": {b, t, p, x, y, w, h, px, py}}
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
        # 贴图名 → [(pathID, w, h, obj)]
        texs: dict[str, list] = {}
        for o in env.objects:
            if o.type.name != "Texture2D":
                continue
            d = o.read()
            texs.setdefault(d.m_Name, []).append((o.path_id, d.m_Width, d.m_Height, o))
        chosen: dict[tuple, int] = {}           # (name, atlas_idx, 帧签名) → pathID 缓存
        img_cache: dict[int, object] = {}

        def pick(tex_name: str, frames: list):
            """在同名候选里选正确贴图：先按区域越界筛，再按内容覆盖率。"""
            cands = texs.get(tex_name) or []
            if not cands:
                return None
            if len(cands) == 1:
                return cands[0]
            fit = [c for c in cands
                   if all(c[1] >= x + w and c[2] >= y + h
                          for _, _, x, y, w, h, _, _ in frames)]
            pool = fit or cands
            if len(pool) == 1:
                return pool[0]

            def score(c):
                pid, tw, th, obj = c
                try:
                    if pid not in img_cache:
                        img_cache[pid] = obj.read().image.convert("RGBA")
                    im = img_cache[pid]
                except Exception:               # noqa: BLE001
                    return -1
                good = 0
                for _, _, x, y, w, h, _, _ in frames:
                    if x + w > tw or y + h > th:
                        continue
                    # 帧 y 是 Unity 底部原点（见 clsmap.BundleTex.frame）
                    yy = th - y - h
                    if yy < 0:
                        continue
                    box = im.crop((x, yy, x + w, yy + h))
                    if box.getchannel("A").getextrema()[1] > 8:
                        good += 1
                return good

            return max(pool, key=score)

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
            by_atlas: dict[int, list] = {}
            for fr in d["frames"]:
                by_atlas.setdefault(fr[0], []).append(fr)
            resolved: dict[int, tuple] = {}
            for idx, frames in by_atlas.items():
                tex_name = f"{d['name']}_{idx}"
                c = pick(tex_name, frames)
                if c is not None:
                    resolved[idx] = c
            for atlas_idx, sub, x, y, w, h, px, py in d["frames"]:
                c = resolved.get(atlas_idx)
                if c is None:
                    continue                        # 贴图不存在：跳过该帧
                key = f"{d['wzpath']}/{sub}" if sub else d["wzpath"]
                entry = {"b": bpath.name, "t": f"{d['name']}_{atlas_idx}",
                         "p": c[0], "x": x, "y": y, "w": w, "h": h,
                         "px": round(px, 4), "py": round(py, 4)}
                if key in reg and reg[key] != entry:
                    dup += 1
                reg[key] = entry
    if dup and progress:
        progress(f"警告：{dup} 个键跨包重复", len(bundles), len(bundles))
    return reg
