"""怀旧服 WZJS 地图 → nxmap 兼容 map_dir 转换器（大地图渲染的数据源）。

输入：json_573635e1*.bundle 的 WZJS 地图对象 + 分类页（Obj/Tile/Back），
外加 vendor/clssprite.py 建的「WZ 路径 → 贴图」注册表。
输出：与 nxmap 完全同构的 map_dir（map.json + img/*.png），下游
vendor/render_map.py、WebUI 预览/窗口导出/怪物叠加全部照用不误。

绘制语义（与 nxmap 一一对应）：
  obj  = Map/Obj/<oS>/<l0>/<l1>[/<l2>]/<canvas>；origin 取 canvas 节点的
         origin 子（vector2，已验证与 NX origin 逐项一致）；
  tile = Map/Tile/<tS>/<u>/<no>，canvas 节点即 <no> 本身；
  back = Map/Back/<bS>/back|ani/<no>[/帧]，canvas 同上；
  帧贴图 = 注册表条目指向的 spritesheet 大图上的 (x,y,w,h) 区域。
"""
from __future__ import annotations

import json
import re
import struct
from pathlib import Path


def safe_name(key: str) -> str:
    """nxmap 同款 img 文件名：路径分隔符全部换 _。"""
    return re.sub(r"[/\\:]", "_", key) + ".png"


class BundleTex:
    """按注册表条目从 spritesheet bundle 里裁帧（进程内缓存大图）。

    贴图对象名会重名（Back/Tile 的 dryRock_0 是两张不同的图），所以优先按
    注册表里记的 pathID（``p``）精确定位，取不到再退回按名（唯一时）。

    解码后的大图按 RGBA 估算字节数记账，超过上限（~1.2GB）就 FIFO 淘汰最旧
    的——全库 1750 张地图转换会摸到几百张 atlas（全留下要好几个 GB），
    单张地图只引用十几张，淘汰后按需重解码即可。
    """

    MAX_BYTES = 1_200_000_000

    def __init__(self, w_dir: Path):
        self.w = w_dir                  # …/StreamingAssets/aa 目录
        self._objs: dict[str, tuple[dict, dict]] = {}   # bundle → (pid→obj, 名→[obj])
        self._imgs: dict[tuple, object] = {}            # (bundle, pid) → PIL Image
        self._bytes = 0                 # 当前缓存图总字节数（RGBA 估算）

    def _load(self, bundle: str):
        if bundle in self._objs:
            return self._objs[bundle]
        import UnityPy
        by_pid: dict[int, object] = {}
        by_name: dict[str, list] = {}
        env = UnityPy.load(str(self.w / "w" / bundle))
        for o in env.objects:
            if o.type.name != "Texture2D":
                continue
            nm = o.read().m_Name
            by_pid[o.path_id] = o
            by_name.setdefault(nm, []).append(o)
        self._objs[bundle] = (by_pid, by_name)
        return self._objs[bundle]

    def sheet(self, bundle: str, tex: str, pid: int | None = None):
        """整张（已解码）大图；找不到返回 None。"""
        k = (bundle, pid if pid is not None else tex)
        if k in self._imgs:
            return self._imgs[k]
        by_pid, by_name = self._load(bundle)
        o = by_pid.get(pid) if pid is not None else None
        if o is None:
            cands = by_name.get(tex) or []
            o = cands[0] if cands else None
        if o is None:
            return None
        im = o.read().image.convert("RGBA")
        k_bytes = im.width * im.height * 4
        while self._bytes + k_bytes > self.MAX_BYTES and self._imgs:
            old_k, old_im = next(iter(self._imgs.items()))
            self._bytes -= old_im.width * old_im.height * 4
            del self._imgs[old_k]
        self._imgs[k] = im
        self._bytes += k_bytes
        return im

    def frame(self, entry: dict):
        """注册表条目 → 裁好的帧（PIL RGBA）；缺图/越界返回 None。

        **描述符的帧 y 是 Unity 底部原点**（Unity 贴图/精灵矩形约定），落盘
        的 image 是自上而下的，必须换算 y_top = H - y - h 再裁。直接用 y 裁
        会取到镜像位置的另一块图——tile 的草皮跑到砖底、obj 整块错位。
        实测判据：bottom 裁法 vs 经典 NX 素材同像素率 33~51%，top 裁法 0~5%。
        """
        im = self.sheet(entry["b"], entry["t"], entry.get("p"))
        if im is None:
            return None
        x, w, h = entry["x"], entry["w"], entry["h"]
        y = im.height - entry["y"] - h
        if y < 0:                       # 帧比贴图还高：按顶部对齐兜底
            y = 0
        x2, y2 = min(x + w, im.width), min(y + h, im.height)
        if x2 <= x or y2 <= y:
            return None
        return im.crop((x, y, x2, y2))


class ClsMapSource:
    """json_573635e1 bundle 的 WZJS 页集合（1750 地图 + 218 分类页）。

    注意分类页的 m_Name 不唯一：dryRock 既有 Back 页也有 Tile 页（root 分别是
    Map/Back/dryRock、Map/Tile/dryRock），所以按 **root_path** 索引，不按名。
    """

    def __init__(self, w_dir: Path, registry: dict):
        self.w = w_dir
        self.reg = registry
        self._raws: list[tuple[str, bytes]] | None = None   # (m_Name, raw)
        self._by_root: dict[str, dict] = {}                  # root_path → 解析结果
        self._by_name: dict[str, list[bytes]] = {}

    # ---- bundle 装载 ----
    def _load_names(self) -> dict[str, list[bytes]]:
        if self._by_name:
            return self._by_name
        import UnityPy
        for b in sorted((self.w / "w").glob("json_573635e1*.bundle")):
            env = UnityPy.load(str(b))
            for o in env.objects:
                if o.type.name != "MonoBehaviour":
                    continue
                r = o.get_raw_data()
                if len(r) < 0x20:
                    continue
                nl = struct.unpack_from("<I", r, 0x1C)[0]
                nm = r[0x20:0x20 + nl].decode("utf-8", "replace")
                self._by_name.setdefault(nm, []).append(r)
        return self._by_name

    def _parse(self, raw: bytes) -> dict:
        from vendor.wzjs import parse_blob
        return parse_blob(raw)

    def page(self, name: str) -> dict | None:
        """地图页：name = 地图 id（数字，唯一）。"""
        raws = self._load_names().get(name)
        if not raws:
            return None
        for r in raws:
            d = self._parse(r)
            if d["root_path"].startswith("Map/Map/"):
                return d
        return None

    def catpage(self, kind: str, name: str) -> dict | None:
        """分类页：kind ∈ {Obj, Tile, Back}，name = 集名（oS/tS/bS）。
        按 root_path（Map/<kind>/<name>）精确定位，避免同名集互相覆盖。"""
        root_path = f"Map/{kind}/{name}"
        if root_path in self._by_root:
            return self._by_root[root_path]
        for r in self._load_names().get(name, ()):
            d = self._parse(r)
            self._by_root[d["root_path"]] = d
            if d["root_path"] == root_path:
                return d
        return None

    def map_ids(self) -> list[str]:
        return sorted(k for k in self._load_names() if k.isdigit())

    # ---- 节点导航 ----
    @staticmethod
    def _child(page: dict, node, key: str):
        nodes = page["nodes"]
        for ci in node.children:
            c = nodes[ci]
            if c.name == key:
                return c
        return None

    @staticmethod
    def _props(page: dict, node) -> dict:
        nodes = page["nodes"]
        return {nodes[ci].name: nodes[ci].value for ci in node.children}

    @staticmethod
    def _get(props: dict, key: str, default=None):
        v = props.get(key)
        return default if v is None else v

    def _canvas(self, page: dict, node, via_ani: bool = False):
        """node 子树里找第一个 type18 canvas（先直接子，再 ani/<帧>/<canvas>）。"""
        from vendor.wzjs import TYPE_CANVAS
        nodes = page["nodes"]
        for ci in node.children:
            if nodes[ci].type == TYPE_CANVAS:
                return nodes[ci]
        if via_ani:
            ani = self._child(page, node, "ani")
            if ani is not None:
                for fi in ani.children:
                    fnode = nodes[fi]
                    for ci in fnode.children:
                        if nodes[ci].type == TYPE_CANVAS:
                            return nodes[ci]
        return None

    @staticmethod
    def _origin_z(page: dict, canvas):
        ox = oy = 0
        for ci in canvas.children:
            c = page["nodes"][ci]
            if c.name == "origin" and isinstance(c.value, tuple):
                ox, oy = c.value
        return ox, oy

    @staticmethod
    def _tile_z(tpg: dict, canvas, entry_props: dict):
        """瓦片绘制层级：优先瓦片图节点的 z → zM → 地图条目的 zM → 0。

        客户端 Gameplay/MapleMap/Tile.cpp 的写法是 z = dsrc["z"]，为 0 再取
        dsrc["zM"]（dsrc = Map/Tile/<tS>/<u>/<no> 画布节点）。经典地图条目
        里的 zM 几乎恒 0（如 101030110 的 169 块墙砖），用条目排序会让墙体
        永远画在 obj 之上，整间屋子只剩墙砖格。
        """
        if canvas is not None:
            props = {tpg["nodes"][ci].name: tpg["nodes"][ci].value
                     for ci in canvas.children}
            for k in ("z", "zM"):
                v = props.get(k)
                if isinstance(v, (int, float)) and v:
                    return int(v)
        for k in ("zM", "z"):
            v = entry_props.get(k)
            if isinstance(v, (int, float)) and v:
                return int(v)
        return 0

    def _texture(self, page: dict, canvas, tex: BundleTex,
                 outdir: Path, key: str | None = None,
                 overwrite: bool = False):
        """canvas 节点 → 裁帧并落盘 outdir/img/<safe>.png；返回文件名（空=失败）。

        overwrite=True 时强制重写（注册表/atlas 语义修正后的重建必需——
        img/ 里缓存的旧 PNG 是按错的 atlas 裁的，不重写会一直错下去）。
        """
        if canvas is None:
            return ""
        full = f"{page['root_path']}/{canvas.path}" if key is None else key
        entry = self.reg.get(full)
        if entry is None:
            # ani 帧名兜底：注册表里有但帧号不是 0（Map/Back/<bS>/ani/<no>/<k>）
            cands = sorted(k for k in self.reg if k.startswith(full + "/"))
            if cands:
                entry = self.reg[cands[0]]
        if entry is None:
            return ""
        im = tex.frame(entry)
        if im is None:
            return ""
        fname = safe_name(full)
        p = outdir / "img" / fname
        if overwrite or not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            im.save(p)
        return fname

    # ---- 主转换 ----
    def convert(self, mid: str, outdir: Path, tex: BundleTex,
                names: dict | None = None, overwrite: bool = False) -> dict:
        """mid（地图 id，如 101040000）→ map.json（并写 img/ 贴图）。"""
        from vendor.wzjs import TYPE_CANVAS, TYPE_SUB
        outdir.mkdir(parents=True, exist_ok=True)
        page = self.page(mid)
        if page is None:
            raise KeyError(f"怀旧服数据里没有地图 {mid}")
        nodes = page["nodes"]
        root = next(n for n in nodes if n.parent < 0)
        names = names or {}

        def gmid(v):
            return str(v).lstrip("0") or "0"

        m: dict = {"id": mid,
                   "name": (names.get(gmid(mid)) or {}).get("name", ""),
                   "streetName": (names.get(gmid(mid)) or {}).get("street", ""),
                   "info": {}, "back": [], "layers": [], "foothold": [],
                   "ladderRope": [], "portal": [], "miniMap": None, "life": []}

        # ---- info ----
        info = self._child(page, root, "info")
        if info is not None:
            for ci in info.children:
                c = nodes[ci]
                if c.type != TYPE_SUB and c.value is not None:
                    m["info"][c.name] = c.value

        # ---- back ----
        back = self._child(page, root, "back")
        if back is not None:
            for ci in back.children:
                b = nodes[ci]
                p = self._props(page, b)
                bs = str(self._get(p, "bS", ""))
                no = int(self._get(p, "no", 0) or 0)
                sub = "ani" if int(self._get(p, "ani", 0) or 0) else "back"
                # 在分类页里找：page(bS) → back|ani/<no>（该节点本身就是 canvas）
                bpg = self.catpage("Back", bs)
                canvas = None
                if bpg is not None:
                    broot = next(n for n in bpg["nodes"] if n.parent < 0)
                    lvl1 = self._child(bpg, broot, sub)
                    if lvl1 is not None:
                        node = self._child(bpg, lvl1, str(no))
                        if node is not None:
                            canvas = (node if node.type == TYPE_CANVAS
                                      else self._canvas(bpg, node, via_ani=(sub == "ani")))
                img = self._texture(bpg, canvas, tex, outdir, overwrite=overwrite) if canvas is not None else ""
                m["back"].append({
                    "bS": bs, "no": no,
                    "x": self._get(p, "x", 0), "y": self._get(p, "y", 0),
                    "rx": self._get(p, "rx", 0), "ry": self._get(p, "ry", 0),
                    "type": self._get(p, "type", 0),
                    "a": self._get(p, "a", 255),
                    "front": self._get(p, "front", 0),
                    "ani": self._get(p, "ani", 0),
                    "f": self._get(p, "f", 0),
                    "cx": self._get(p, "cx", 0), "cy": self._get(p, "cy", 0),
                    "img": img})

        # ---- layers 0..7 ----
        for li in range(8):
            lay = self._child(page, root, str(li))
            layer = {"index": li, "tile": [], "obj": []}
            if lay is not None:
                tS = ""
                linf = self._child(page, lay, "info")
                if linf is not None:
                    tS = str(self._get(self._props(page, linf), "tS", "") or "")
                tl = self._child(page, lay, "tile")
                if tl is not None:
                    for ci in tl.children:
                        t = nodes[ci]
                        p = self._props(page, t)
                        u = str(self._get(p, "u", "") or "")
                        no = int(self._get(p, "no", 0) or 0)
                        tpg = self.catpage("Tile", tS) if tS else None
                        canvas = None
                        if tpg is not None:
                            troot = next(n for n in tpg["nodes"] if n.parent < 0)
                            lvl1 = self._child(tpg, troot, u)
                            if lvl1 is not None:
                                canvas = self._child(tpg, lvl1, str(no))
                                if canvas is not None and canvas.type != TYPE_CANVAS:
                                    canvas = self._canvas(tpg, canvas)
                        ox, oy = (self._origin_z(tpg, canvas)
                                   if canvas is not None else (0, 0))
                        img = self._texture(tpg, canvas, tex, outdir, overwrite=overwrite) \
                            if canvas is not None else ""
                        layer["tile"].append({
                            "u": u, "no": no,
                            "x": self._get(p, "x", 0), "y": self._get(p, "y", 0),
                            # z 取**瓦片图节点**的 z/zM（客户端 Tile.cpp: z =
                            # dsrc["z"]，为 0 再取 zM）——地图条目的 zM 恒 0，
                            # 拿它排序会把所有地面瓦片排到最低层、盖住同层 obj
                            "z": self._tile_z(tpg, canvas, p),
                            "ox": ox, "oy": oy, "img": img})
                ob = self._child(page, lay, "obj")
                if ob is not None:
                    for ci in ob.children:
                        o = nodes[ci]
                        p = self._props(page, o)
                        oS = str(self._get(p, "oS", "") or "")
                        l0 = str(self._get(p, "l0", "") or "")
                        l1 = str(self._get(p, "l1", "") or "")
                        l2 = str(self._get(p, "l2", "") or "")
                        opg = self.catpage("Obj", oS) if oS else None
                        canvas = None
                        if opg is not None:
                            oroot = next(n for n in opg["nodes"] if n.parent < 0)
                            node = self._child(opg, oroot, l0)
                            if node is not None and l1:
                                node = self._child(opg, node, l1)
                            if node is not None and l2:
                                node = self._child(opg, node, l2)
                            canvas = self._canvas(opg, node, via_ani=True) \
                                if node is not None else None
                        ox, oy = self._origin_z(opg, canvas) \
                            if canvas is not None else (0, 0)
                        img = self._texture(opg, canvas, tex, outdir, overwrite=overwrite) \
                            if canvas is not None else ""
                        layer["obj"].append({
                            "oS": oS, "l0": l0, "l1": l1, "l2": l2, "no": 0,
                            "x": self._get(p, "x", 0), "y": self._get(p, "y", 0),
                            "z": self._get(p, "z", self._get(p, "zM", 0)),
                            "f": self._get(p, "f", 0),
                            "ox": ox, "oy": oy, "img": img})
            m["layers"].append(layer)

        # ---- foothold（三层：foothold/<layer>/<group>/<seg>）----
        fh = self._child(page, root, "foothold")
        if fh is not None:
            for ai in fh.children:
                layer = int(nodes[ai].name)
                for bi in nodes[ai].children:
                    grp = nodes[bi]
                    for ci in grp.children:
                        seg = nodes[ci]
                        p = self._props(page, seg)
                        m["foothold"].append({
                            "id": int(seg.name) if seg.name.isdigit() else 0,
                            "x1": self._get(p, "x1", 0), "y1": self._get(p, "y1", 0),
                            "x2": self._get(p, "x2", 0), "y2": self._get(p, "y2", 0),
                            "prev": self._get(p, "prev", 0), "next": self._get(p, "next", 0),
                            "layer": layer})

        # ---- ladderRope ----
        lr = self._child(page, root, "ladderRope")
        if lr is not None:
            for ci in lr.children:
                p = self._props(page, nodes[ci])
                m["ladderRope"].append({
                    "x": self._get(p, "x", 0),
                    "y1": self._get(p, "y1", 0), "y2": self._get(p, "y2", 0),
                    "l": self._get(p, "l", 0)})

        # ---- portal ----
        pt = self._child(page, root, "portal")
        if pt is not None:
            for ci in pt.children:
                p = nodes[ci]
                d = self._props(page, p)
                tm = self._get(d, "tm", 0)
                m["portal"].append({
                    "pn": str(self._get(d, "pn", "") or ""),
                    "pt": self._get(d, "pt", 0),
                    "x": self._get(d, "x", 0), "y": self._get(d, "y", 0),
                    "tm": tm, "tn": str(self._get(d, "tn", "") or ""),
                    "tmn": (names.get(gmid(tm)) or {}).get("name", "") if tm else "",
                    "index": p.name})

        # ---- miniMap ----
        mm = self._child(page, root, "miniMap")
        if mm is not None:
            canvas = next((nodes[ci] for ci in mm.children
                           if nodes[ci].type == TYPE_CANVAS), None)
            img = self._texture(page, canvas, tex, outdir, key=None,
                                overwrite=overwrite) \
                if canvas is not None else ""
            if img:
                im = (outdir / "img" / img)
                from PIL import Image
                with Image.open(im) as t:
                    cw, ch = t.size
                m["miniMap"] = {"img": img, "cw": cw, "ch": ch,
                                "w": self._get(self._props(page, mm), "width", cw),
                                "h": self._get(self._props(page, mm), "height", ch),
                                "centerX": self._get(self._props(page, mm), "centerX", 0),
                                "centerY": self._get(self._props(page, mm), "centerY", 0),
                                "mag": self._get(self._props(page, mm), "mag", 0)}

        # ---- life ----
        lf = self._child(page, root, "life")
        if lf is not None:
            for ci in lf.children:
                d = self._props(page, nodes[ci])
                m["life"].append({
                    "type": str(self._get(d, "type", "") or ""),
                    "id": str(self._get(d, "id", "") or ""),
                    "x": self._get(d, "x", 0), "y": self._get(d, "y", 0),
                    "cy": self._get(d, "cy", 0), "fh": self._get(d, "fh", 0),
                    "rx0": self._get(d, "rx0", 0), "rx1": self._get(d, "rx1", 0),
                    "mobTime": self._get(d, "mobTime", 0)})

        (outdir / "map.json").write_text(
            json.dumps(m, ensure_ascii=False), encoding="utf-8")
        return m
