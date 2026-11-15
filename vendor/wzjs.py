"""WZJS v5 二进制格式解析器（怀旧服 mxdclassic 客户端）。

来源与证据链（2026-11-15 逆向）：
- 数据在 ``json_573635e1*.bundle`` 的 2000 个 MonoBehaviour 里；
  raw = MonoBehaviour 头(m_Name=根名) + 36 个 u32 类字段 + [u32 len][byte[] 数据]。
  byte[] 以 b"WZJS" + u32 版本(5) 开头。
- 类名 = IL2CPP 混淆哈希 ba5edfa6c3d38dc527b4c6e68fea6c60eab25dd5955e12994065c8595cdf574
  (type index 2317, 65 个方法)；解析器语法由数据侧 + 路径表 oracle 推导，
  并经 2000 个对象父链验证（见 scripts/wzjs_validate.py）。

格式总览（所有偏移相对 byte[] blob）：
  [0x00] "WZJS" u32 版本 5
  [0x08] 节点记录流：N * 8 个 i32
  ...   11 个类型值区（列式存储，每区 (count, offset) 由类字段给出）
  ...   置换数组 perm[N-1]（DFS 序缓存，解析用不到）
  ...   [名字 blob][名字边界表 N+1+终止0]
        [路径 blob][路径边界表 cnt+1+终止0]   ← 比节点数多出的条目是 $spritesheetitem
        [字符串值 blob][字符串值边界表 cnt+1+终止0]

节点记录 8 个 i32:
  [0] type     2=子树 6=int 8=float 11=string 14=vector2 18=canvas/sprite
  [1] name     名字表索引（root 为图名）
  [2] val      类型值区索引；子树节点恒 -1；type18 恒 -1（sprite=路径本身）
  [3] first    首孩子节点索引；叶子 -1
  [4] ccnt     孩子数
  [5] parent   父节点索引；root -1
  [6] path     自身路径索引（root=完整 WZ 路径如 Map/Obj/holloween，
               其余为相对 root 的路径）
  [7] dfs      顶子树 DFS 缓存位（同顶子树恒定，解析用不到）

路径规则：root 用 path[col6]；root 的孩子 = 名字本身；再深 = 父路径 + "/" + 名字。
type18(canvas) 的贴图引用 = 自身路径 + "/$spritesheetitem"，
在 spritesheet_*.bundle 的 MonoBehaviour 描述符（内含 WZ 路径/图集哈希/帧路径）
中查表解析。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"WZJS"
VERSION = 5

TYPE_SUB = 2      # 子树（imgdir）
TYPE_INT = 6      # int 值区 = 列 3
TYPE_FLOAT = 8    # float 值区 = 列 5
TYPE_STR = 11     # 字符串值区（类字段 32..34 独立段，不在 11 列内）
TYPE_VEC = 14     # vector2 值区 = 列 6（x,y 两个 u32）
TYPE_CANVAS = 18  # canvas/$spritesheetitem：贴图引用 = 路径 + "/$spritesheetitem"


class WzjsError(ValueError):
    pass


@dataclass
class Node:
    idx: int
    type: int
    name: str
    path: str
    value: object          # int / float / str / (x, y) / None
    parent: int
    first_child: int
    child_count: int

    @property
    def is_dir(self) -> bool:
        return self.type == TYPE_SUB

    @property
    def children(self) -> range:
        return range(self.first_child, self.first_child + self.child_count)

    @property
    def sprite_path(self) -> str | None:
        """type18 canvas 的贴图引用路径。"""
        return self.path + "/$spritesheetitem" if self.type == TYPE_CANVAS else None


def parse_blob(raw: bytes) -> dict:
    """解析 MonoBehaviour raw（get_raw_data()），返回 {name, root, nodes, extra_paths}。

    nodes: list[Node]；nodes[i] 的孩子为 nodes[first_child .. first_child+child_count)。
    extra_paths: 路径表中未被节点 col6 引用的条目（$spritesheetitem 等）。
    """
    nl = struct.unpack_from("<I", raw, 0x1C)[0]
    name = raw[0x20:0x20 + nl].decode("ascii", "replace")
    pos = (0x20 + nl + 3) & ~3          # 36 个类字段的起点（随名字长度变化）
    f = struct.unpack_from("<36I", raw, pos)
    # f[35] = byte[] 数据长度；数据紧跟 36 字段之后（起点 pos+0x90）
    blob = raw[pos + 0x90:pos + 0x90 + f[35]]
    if blob[:4] != MAGIC:
        raise WzjsError(f"bad magic {blob[:4]!r}")
    if struct.unpack_from("<I", blob, 4)[0] != VERSION:
        raise WzjsError(f"unsupported version {blob[4:8]!r}")
    n, rec_off, perm_cnt, perm_off = f[0], f[1], f[2], f[3]
    cols = [(f[4 + 2 * i], f[5 + 2 * i]) for i in range(11)]
    # 列序与 TYPE 对应：列3=int 列5=float 列6=vector2（计数与类型分布严格一致）
    int_cnt, int_off = cols[3]
    flt_cnt, flt_off = cols[5]
    vec_cnt, vec_off = cols[6]
    name_cnt, name_off, name_tab = f[26], f[27], f[28]
    path_cnt, path_off, path_tab = f[29], f[30], f[31]
    str_cnt, str_off, str_tab = f[32], f[33], f[34]

    def blob_table(base: int, tab: int, cnt: int) -> list[str]:
        if cnt == 0:
            return []
        bounds = [struct.unpack_from("<I", blob, tab + 4 * i)[0] for i in range(cnt + 1)]
        return [blob[base + bounds[i]:base + bounds[i + 1]].decode("utf-8", "replace")
                for i in range(cnt)]

    names = blob_table(name_off, name_tab, name_cnt)
    paths = blob_table(path_off, path_tab, path_cnt)
    strs = blob_table(str_off, str_tab, str_cnt)

    def ints(i: int) -> int:
        return struct.unpack_from("<i", blob, int_off + 4 * i)[0]

    def flts(i: int) -> float:
        return struct.unpack_from("<f", blob, flt_off + 4 * i)[0]

    def vec(i: int) -> tuple[int, int]:
        # 值区按 (x,y) 对打包；col2 是对索引
        return (struct.unpack_from("<i", blob, vec_off + 8 * i)[0],
                struct.unpack_from("<i", blob, vec_off + 8 * i + 4)[0])

    root_path = ""
    nodes: list[Node] = []
    used_paths = set()
    recs = []
    for i in range(n):
        t, nm, val, fc, cc, par, pidx, dfs = struct.unpack_from("<8i", blob, rec_off + 32 * i)
        recs.append((t, nm, val, fc, cc, par, pidx, dfs))
        used_paths.add(pidx)
        if par < 0:
            root_path = paths[pidx] if paths else names[nm]
    for i, (t, nm, val, fc, cc, par, pidx, dfs) in enumerate(recs):
        nm_str = names[nm] if nm >= 0 else ""
        if par < 0:
            path = root_path
        elif recs[par][5] < 0:      # 父是 root → 路径 = 名字
            path = nm_str
        else:
            path = nodes[par].path + "/" + nm_str
        if t == TYPE_SUB:
            v = None
        elif t == TYPE_INT:
            v = ints(val)
        elif t == TYPE_FLOAT:
            v = flts(val)
        elif t == TYPE_STR:
            v = strs[val]
        elif t == TYPE_VEC:
            v = vec(val)
        elif t == TYPE_CANVAS:
            v = None
        else:
            v = val               # 未知类型保留原始索引
        nodes.append(Node(i, t, nm_str, path, v, par, fc, cc))

    # 校验：父链拼接与路径表一致（路径表是天然 oracle）
    if paths:
        for nd in nodes:
            actual = paths[recs[nd.idx][6]]
            if nd.path != actual:
                raise WzjsError(f"path mismatch node{nd.idx}: {nd.path!r} != {actual!r}")
    extra_paths = [p for i, p in enumerate(paths) if i not in used_paths]
    return {"name": name, "root_path": root_path, "nodes": nodes,
            "extra_paths": extra_paths, "raw_fields": f}


def find(root: dict, path: str) -> Node | None:
    """按相对路径查节点，支持 a/b/c 与数字段。"""
    parts = path.strip("/").split("/")
    node = None
    for nd in root["nodes"]:
        if nd.parent < 0:
            node = nd
            break
    for p in parts:
        nxt = None
        for ci in node.children:
            c = root["nodes"][ci]
            if c.name == p:
                nxt = c
                break
        if nxt is None:
            return None
        node = nxt
    return node


def get(root: dict, path: str, default=None):
    """取值节点；子树节点返回 default。"""
    nd = find(root, path)
    if nd is None:
        return default
    return nd.value if nd.value is not None and nd.type != TYPE_SUB else default
