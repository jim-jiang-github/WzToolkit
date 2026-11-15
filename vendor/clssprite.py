"""怀旧服 spritesheet 描述符解析与「WZ 路径 → 贴图」注册表。

数据源：12 个 spritesheet_*.bundle 里的 MonoBehaviour 描述符（IL2CPP 客户端，
无类型树）。描述符与它引用的贴图 **同 bundle**：贴图对象名 = `<m_Name>_<atlas_idx>`，
帧是贴图上的一个矩形区域。

描述符 raw 布局（2026-11-15 逆向，2026-11-16 修订两处关键语义，经 12 包验证）::

    0x10-0x1B  m_Script PPtr（该类在所有包里都是 01 00 00 00 0f 10 bb fe …）
    0x1C       u32 名称长度
    0x20..     m_Name（贴图条目名，如 211040102 / EmotionEffect）
    (对齐)
    u32 WZ 路径长度；WZ 路径（如 Map/Map/Map2/211040102）
    u32 atlas 数 n；每项: [u32 哈希长度=32][32 字节哈希][8 字节保留(恒 0)]
    u32 帧数
    每帧: [u32 子路径长度][子路径][对齐]
          u32 atlas_idx —— **裸 int，不是 float**！多 atlas 描述符（如
          Map/Obj/acc1 有 11 页）按 f32 读会得到反常规小数（1~10 → ~1e-44），
          int() 后全是 0，所有帧都被错配到第 0 页。
          6 个 float: x, y, W, H, pivX, pivY
          （x 为区域左边界；**y 是 Unity 底部原点**，自上而下裁时要换成
          H - y - h；piv 为归一化轴心，0.5=中心，可越界）
    u32 inlink 数 n1；每项: [u32 子路径长度][子路径][对齐][u32 目标帧号 v]
    u32 inlink(带 pivot 覆盖) 数 n2；每项: [u32 子路径长度][子路径][对齐]
          [u32 v][f32 pivX][f32 pivY]
    u32 保留（恒 0；空表时三段共 12 字节，即旧版记录的「尾部 12 字节保留」）

inlink = WZ 同图内引用（客户端类 WzSpriteSheetInLink）：子路径不在主帧
表里，其图像 = 主帧表第 v 帧的矩形；n2 列表额外覆盖 pivot。缺失素材统一
指向一张 1x1 占位帧（如 acc1 有 32 个子路径都指帧 711）。

注册表键 = WZ 路径 + "/" + 帧子路径（与 WZJS type18 节点 full path 一致，
去掉 ``/$spritesheetitem`` 后缀即可查表）。
"""
from __future__ import annotations

import struct
from pathlib import Path

SCRIPT_SIG = bytes.fromhex("010000000f10bbfe")   # m_Script（fileId=1 高 4B + pathID 低 4B 头）


def parse_desc(raw: bytes) -> dict:
    """解析一个描述符。返回 {name, wzpath, atlases, frames, inlinks, end}。

    frames: list[(atlas_idx, subpath, x, y, w, h, pivx, pivy)]
    inlinks: list[(subpath, v, px, py)]  px/py 为 None 表示不覆盖 pivot
    解析失败抛 ValueError。尾部 inlink 表解析失败不报错（能解多少算多少）。
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
        # atlas_idx 是裸 int（不是 float）——见模块 docstring
        ai = struct.unpack_from("<I", raw, p)[0]
        if ai >= n_atlas:          # 兜底：万一是 f32 编码则换算回来
            fv = struct.unpack_from("<f", raw, p)[0]
            ai = int(fv) if fv < n_atlas else 0
        x, y, w, h, px, py = struct.unpack_from("<6f", raw, p + 4)
        p += 28
        frames.append((ai, sub, int(x), int(y),
                       int(w), int(h), px, py))
    # ---- 尾部：两个 inlink 列表 + u32 保留 ----
    inlinks: list[tuple] = []

    def _list(pos: int, with_pivot: bool):
        """解一个 [u32 n][n × [len][path][对齐][u32 v]([2f])] 列表。

        返回 (条目列表, 下一偏移)；结构不合法返回 (已解条目, None)。
        """
        if pos + 4 > len(raw):
            return [], pos
        n = struct.unpack_from("<I", raw, pos)[0]
        pos += 4
        out = []
        for _ in range(n):
            if pos + 4 > len(raw):
                return out, None
            ln = struct.unpack_from("<I", raw, pos)[0]
            if ln == 0 or ln > 256 or pos + 4 + ln > len(raw):
                return out, None
            s = raw[pos + 4:pos + 4 + ln].decode("utf-8", "replace")
            pos = (pos + 4 + ln + 3) & ~3
            if pos + 4 > len(raw):
                return out, None
            v = struct.unpack_from("<I", raw, pos)[0]
            pos += 4
            px = py = None
            if with_pivot:
                if pos + 8 > len(raw):
                    return out, None
                px, py = struct.unpack_from("<2f", raw, pos)
                pos += 8
            out.append((s, v, px, py))
        return out, pos

    l1, p2 = _list(p, False)
    inlinks.extend(l1)
    if p2 is not None:
        l2, p3 = _list(p2, True)
        inlinks.extend(l2)
    return {"name": name, "wzpath": wz, "atlases": atlases, "frames": frames,
            "inlinks": inlinks, "end": p}     # end: 主结构结束位置


def build_registry(w_dir: Path, progress=None,
                   inlink_prefixes: tuple = ()) -> dict:
    """扫描全部 spritesheet_*.bundle，建立注册表。

    贴图解析要点：bundle 里贴图对象名是 ``<m_Name>_<atlas_idx>``，但**会重名**
    （Map/Back/dryRock 与 Map/Tile/dryRock 各有一张 dryRock_0，内容完全不同，
    全库共 62 组）。所以注册表给每个条目记下贴图的 pathID（``p``）：重名时按
    「帧区域全部落在贴图内 + 区域实际有像素」逐个候选解码打分，选出正确的那张。

    inlink_prefixes：只注册 wzpath 以任一前缀开头的描述符的 inlink 条目
    （全库 inlink 有 393 万条，其中发型占 350 万——不过滤会把注册表撑爆；
    地图渲染只需要 ("Map/",)，约 3800 条）。空 = 不过滤全部注册。

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
            # ---- inlink：主帧表里没有的子路径 → 复用第 v 帧的矩形 ----
            if inlink_prefixes and not d["wzpath"].startswith(inlink_prefixes):
                continue
            frs = d["frames"]
            for sub, v, px, py in d.get("inlinks", ()):
                if not sub or not (0 <= v < len(frs)):
                    continue
                key = f"{d['wzpath']}/{sub}"
                if key in reg:
                    continue                        # 主表（或前面的表）已有
                _, _, x, y, w, h, tpx, tpy = frs[v]
                c = resolved.get(frs[v][0])
                if c is None:
                    continue
                reg[key] = {"b": bpath.name, "t": f"{d['name']}_{frs[v][0]}",
                            "p": c[0], "x": x, "y": y, "w": w, "h": h,
                            "px": round(px if px is not None else tpx, 4),
                            "py": round(py if py is not None else tpy, 4)}
    if dup and progress:
        progress(f"警告：{dup} 个键跨包重复", len(bundles), len(bundles))
    return reg
