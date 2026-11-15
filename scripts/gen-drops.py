# -*- coding: utf-8 -*-
"""从服务端数据生成掉落表与道具名（随仓库分发，同 mobstats.json 模式）。

输入：
  ../MapleStory/server/pve/sql/db_drops.sql   —— drop_data 表的 INSERT（真源）
  ../MapleStory/server/wz/String.wz/*.img.xml —— 道具名（Consume/Eqp/Etc/Ins/Cash/Pet）

输出：
  vendor/drops.json     {mobid: [{"i": itemid, "q": [min, max], "c": chance}, ...]}
  vendor/itemnames.json {"<zfill8 id>": {"name": ..., "desc": ...}}
"""
import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT.parent / "MapleStory" / "server"
SQL = SERVER / "pve" / "sql" / "db_drops.sql"
SWZ = SERVER / "wz" / "String.wz"

ROW_RE = re.compile(r"\((\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)")


def parse_drops() -> dict:
    sql = SQL.read_text(encoding="utf-8", errors="replace")
    drops: dict[int, list] = {}
    for mob, item, qmin, qmax, quest, chance in ROW_RE.findall(sql):
        mob, item = int(mob), int(item)
        if item == 0:                     # 金币（meso）单独列，掉落合成用
            item = 0
        drops.setdefault(mob, []).append(
            {"i": item, "q": [int(qmin), int(qmax)], "c": int(chance)})
    # 同怪同道具去重（取最大 chance）
    for mob in drops:
        seen = {}
        for e in drops[mob]:
            k = e["i"]
            if k not in seen or e["c"] > seen[k]["c"]:
                seen[k] = e
        drops[mob] = sorted(seen.values(), key=lambda e: -e["c"])
    return drops


NAME_FILES = ("Consume.img.xml", "Eqp.img.xml", "Etc.img.xml",
              "Ins.img.xml", "Cash.img.xml", "Pet.img.xml")


def parse_names() -> dict:
    """String.wz 的 imgdir XML → {zfill8 id: {name, desc}}。

    Eqp.img 是三层嵌套（Eqp/<类别>/<id>），其余是两层（<文件>/<id>）。
    """
    out: dict[str, dict] = {}

    def walk(node: ET.Element):
        for ch in node:
            if ch.tag == "imgdir":
                walk(ch)
            elif ch.tag == "string" and ch.get("name") in ("name", "desc"):
                key = node.get("name", "")
                if key.isdigit():
                    ent = out.setdefault(f"{int(key):08d}", {})
                    ent[ch.get("name")] = ch.get("value", "")

    for fn in NAME_FILES:
        p = SWZ / fn
        if not p.exists():
            continue
        walk(ET.parse(p).getroot())
    return out


def main() -> None:
    if not SQL.exists():
        sys.exit(f"找不到 {SQL}")
    drops = parse_drops()
    n_items = sum(len(v) for v in drops.values())
    (ROOT / "vendor" / "drops.json").write_text(
        json.dumps(drops, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    names = parse_names()
    (ROOT / "vendor" / "itemnames.json").write_text(
        json.dumps(names, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    print(f"drops.json: {len(drops)} 个怪 / {n_items} 条掉落")
    print(f"itemnames.json: {len(names)} 个道具名")
    print("样例 mob=100100:", drops.get(100100, [])[:4])
    print("样例名 02000000:", names.get("02000000"))


if __name__ == "__main__":
    main()
