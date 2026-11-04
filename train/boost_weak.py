"""针对弱类补数据：解析弱类里的怪物 id → 找含这些怪的地图 → 多导随机窗口。

用法：python boost_weak.py '["100100_Snail__move", ...]' --win 3 --tag b1
弱类是 rope/ladder 就直接多导一批随机窗口（每张图都有绳梯）。
"""
import argparse
import concurrent.futures as cf
import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "wztool.exe"
YDIR = ROOT / "exports" / "yolo"
W, H = 1366, 768


def run_cli(args):
    r = subprocess.run([str(EXE), *args], capture_output=True, cwd=str(ROOT))
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("weak_json")
    ap.add_argument("--win", type=int, default=3)
    ap.add_argument("--tag", default="b1")
    a = ap.parse_args()
    weak = json.loads(a.weak_json)
    mob_ids = sorted({w.split("__")[0].split("_")[0] for w in weak
                      if w not in ("rope", "ladder")})
    print(f"弱类怪 id: {mob_ids}", flush=True)

    # 找含这些怪的地图（mob-maps 每个 id 一次）
    want_maps = set()
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for mid in ex.map(_mob_maps, mob_ids):
            want_maps.update(mid)
    if not want_maps:
        print("没找到含弱类怪的地图")
        return 1
    print(f"涉及地图 {len(want_maps)} 张", flush=True)

    # 批量探测可用（够大不缩放）并导出
    rnd = random.Random(a.tag)
    r = subprocess.run([str(EXE), "probe"], input="\n".join(sorted(want_maps)).encode(),
                       capture_output=True, cwd=str(ROOT), timeout=1800)
    usable = []
    if r.returncode == 0:
        try:
            usable = [(x["id"], x["world"]) for x in json.loads(
                r.stdout.decode("utf-8", errors="replace"))
                if x.get("usable")]
        except ValueError:
            pass
    picked = []
    rnd.shuffle(usable)
    for mid, w in usable[:40]:
        picked.append((mid, w))
    jobs = []
    for mid, w in picked:
        for _ in range(a.win):
            x = round(rnd.uniform(w["x"], w["x"] + w["w"] - W))
            y = round(rnd.uniform(w["y"], w["y"] + w["h"] - H))
            seed = f"{a.tag}{rnd.randrange(1 << 24):06x}"
            jobs.append((mid, x, y, seed))
    print(f"补导出 {len(jobs)} 个窗口（全正样本）", flush=True)

    ok = 0
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for d in ex.map(_export, jobs):
            ok += bool(d)
    print(f"补导出成功 {ok}/{len(jobs)}", flush=True)
    return 0


def _mob_maps(mid: str):
    d = run_cli(["mob-maps", mid, "--json"])
    if d and d.get("maps"):
        return {m["mapid"] for m in d["maps"] if m.get("mapid")}
    return set()


def _export(job):
    mid, x, y, seed = job
    return run_cli(["map-export", mid, "--mobs", "--yolo", "--json",
                    f"--region={x},{y},{W},{H}", f"--density=2", f"--seed={seed}"])


if __name__ == "__main__":
    sys.exit(main())
