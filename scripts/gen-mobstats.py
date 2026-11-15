"""从服务端 Odin 格式 WZ (imgdir XML) 提取怪物数值表 → vendor/mobstats.json。

服务端（../MapleStory/server/wz/Mob.wz/*.img.xml，HeavenMS 系 imgdir XML）是唯一带
怪物等级/血量/经验的数据源 —— 客户端国服 NX 里 Mob 只有贴图，数值全在服务端
（nxmob.cpp 注释同款结论）。表提交进仓库，工具运行时不再依赖服务端目录。

用法：python scripts/gen-mobstats.py [--src <Mob.wz 目录>] [--out <输出 json>]
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = ROOT.parent / "MapleStory" / "server" / "wz" / "Mob.wz"
DEFAULT_OUT = ROOT / "vendor" / "mobstats.json"


def parse_one(xml_path: Path) -> dict:
    """取一个 mob img XML 的 info 数值 + link 目标（link 需要后续解析补全）。"""
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return {"ints": {}, "link": ""}
    ints: dict[str, int] = {}
    link = ""
    for child in root:
        if child.tag != "imgdir" or child.get("name") != "info":
            continue
        for n in child:
            if n.tag == "int" and n.get("name") and n.get("value") is not None:
                try:
                    ints[n.get("name")] = int(n.get("value"))
                except ValueError:
                    pass
            if n.get("name") == "link" and n.get("value") is not None:
                link = n.get("value").lstrip("0")
    return {"ints": ints, "link": link}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC, help="服务端 Mob.wz 目录")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出 JSON 路径")
    args = ap.parse_args()

    src = args.src
    if not src.is_dir():
        print(f"找不到服务端 Mob.wz 目录：{src}", file=sys.stderr)
        return 1
    xmls = sorted(src.glob("*.img.xml"))
    if not xmls:
        print(f"{src} 下没有 *.img.xml", file=sys.stderr)
        return 1

    parsed: dict[str, dict] = {}
    for p in xmls:
        mid = p.name.split(".")[0].lstrip("0")
        if not mid:
            continue
        parsed[mid] = parse_one(p)

    # link 链解析：数值借用目标怪（服务端 MapleLifeFactory 同款语义）
    def resolve(mid: str, depth: int = 0) -> dict:
        if mid not in parsed or depth > 4:
            return {}
        info = parsed[mid]
        if info["ints"].get("level", 0) > 0:
            return info["ints"]
        tgt = info["link"]
        return resolve(tgt, depth + 1) if tgt else {}

    table: dict[str, list] = {}
    for mid, info in parsed.items():
        ints = resolve(mid)
        lv = ints.get("level", 0)
        if lv <= 0:
            continue
        table[mid] = [lv, ints.get("maxHP", 0), ints.get("exp", 0),
                      1 if ints.get("boss") else 0]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(table, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    print(f"共 {len(xmls)} 只怪，有数值的 {len(table)} 只 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
