"""批量导出 YOLO 训练图（纯 CLI 驱动 wztool.exe）。

条件（训练要求）：
1. 地图必须刷怪（maps --json 的 life_maps）
2. 窗口固定 1366×768（客户端分辨率），且地图够大不会被缩放（scale==1）
3. 正样本：--mobs --yolo（图里合成随机动作状态的怪物 + 全部标注）
   负样本：--yolo 不带 --mobs（图里一只怪都没有，绳梯照常标注）——
   防止模型把地图装饰误检成怪；占比由 --neg 控制（默认 15%）

用法：python batch_export.py --maps 60 --win 4 --tag p1
产出：exports/yolo/{images,labels}/ + 本脚本目录 batch_<tag>_result.json
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


def log(msg: str):
    print(msg, flush=True)


def run_cli(args: list, timeout: int = 1800) -> dict | None:
    """跑 wztool --json。exe 的中文报错是 GBK 编码，用 errors=replace 兜住，
    解析失败时把 stderr 尾部打出来方便排查。"""
    r = subprocess.run([str(EXE), *args], capture_output=True,
                       cwd=str(ROOT), timeout=timeout)
    if r.returncode != 0:
        tail = r.stderr[-400:].decode("utf-8", errors="replace").strip()
        if tail:
            log(f"CLI rc={r.returncode}: {args[0]} {args[1] if len(args) > 1 else ''} … {tail[-200:]}")
        return None
    try:
        return json.loads(r.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        tail = r.stdout[-300:].decode("utf-8", errors="replace")
        log(f"JSON 解析失败: {args[:2]} … {tail[-200:]}")
        return None


def mob_class_ids() -> set:
    """classes.json 里非 rope/ladder 的都是怪类别（含带 __状态 后缀的）。"""
    f = YDIR / "classes.json"
    if not f.exists():
        return set()
    reg = json.loads(f.read_text(encoding="utf-8"))
    return {i for nm, i in reg.items() if nm not in ("rope", "ladder")}


def probe_all(ids: list) -> list:
    """一次 wztool probe 批量探测（单 exe 启动，进程内全查）。"""
    r = subprocess.run([str(EXE), "probe"], input="\n".join(ids).encode(),
                       capture_output=True, cwd=str(ROOT), timeout=1800)
    if r.returncode != 0:
        return []
    try:
        recs = json.loads(r.stdout)
    except ValueError:
        return []
    usable = [x for x in recs if x.get("usable")]
    return [(x["id"], x["world"]["x"], x["world"]["y"],
             x["world"]["w"], x["world"]["h"]) for x in usable]


def export(job) -> dict:
    mid, x, y, den, seed, neg = job
    args = ["map-export", mid, "--yolo", "--json",
            f"--region={x},{y},{W},{H}", f"--seed={seed}"]
    if not neg:
        args += ["--mobs", f"--density={den}"]
    d = run_cli(args)
    if not d or not d.get("yolo"):
        return {"id": mid, "seed": seed, "ok": False, "reason": "export 失败"}
    lbl = Path(d["yolo"]["labels"])
    img = Path(d["yolo"]["image"])
    mob_ids = mob_class_ids()
    n_mob = 0
    if lbl.exists():
        for ln in lbl.read_text(encoding="utf-8").splitlines():
            parts = ln.split()
            if parts and int(parts[0]) in mob_ids:
                n_mob += 1
    if neg and n_mob > 0:
        return {"id": mid, "seed": seed, "ok": False, "reason": "负样本里出现怪（异常）"}
    return {"id": mid, "seed": seed, "ok": True, "neg": neg, "mobs": n_mob,
            "boxes": d["yolo"]["boxes"], "states": d["yolo"].get("states", {}),
            "image": str(img), "label": str(lbl)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--maps", type=int, default=60, help="选多少张可用地图")
    ap.add_argument("--win", type=int, default=4, help="每张地图导几个随机窗口")
    ap.add_argument("--neg", type=float, default=0.15, help="负样本占比（0~1）")
    ap.add_argument("--tag", default="p1", help="seed 前缀 / 结果文件名标记")
    ap.add_argument("--probe-limit", type=int, default=2000, help="最多探测多少张")
    a = ap.parse_args()

    d = run_cli(["maps", "--json"])
    cands = list(d["life_maps"])
    random.shuffle(cands)
    log(f"有怪地图 {len(cands)} 张，批量探测（wztool probe，单次进程内全查）…")
    usable = probe_all(cands[:a.probe_limit])
    log(f"可用 {len(usable)} 张")
    if len(usable) < min(20, a.maps):
        log("可用地图太少，中止")
        return 1
    picked = usable[:a.maps]

    jobs = []
    rnd = random.Random(a.tag)
    for mid, minx, miny, bw, bh in picked[:a.maps]:
        for i in range(a.win):
            x = round(rnd.uniform(minx, minx + bw - W))
            y = round(rnd.uniform(miny, miny + bh - H))
            seed = f"{a.tag}{rnd.randrange(1 << 24):06x}"
            neg = rnd.random() < a.neg
            jobs.append((mid, x, y, rnd.choice((1, 2)), seed, neg))
    log(f"导出 {len(jobs)} 个窗口（4 路并行按地图分组，含 {sum(j[5] for j in jobs)} 个负样本）…")

    # 按地图分组：同一张图的窗口在同一个线程里串行导（渲染缓存天然复用，
    # 也避免多进程并发渲染/写同一张图的缓存互相踩）；不同地图之间并行
    by_map: dict[str, list] = {}
    for j in jobs:
        by_map.setdefault(j[0], []).append(j)
    results, done = [], 0

    def export_map(item):
        mid, js = item
        return [export(j) for j in js]

    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for res_list in ex.map(export_map, list(by_map.items())):
            for res in res_list:
                results.append(res)
                done += 1
                if res["ok"]:
                    tag = "负样本" if res["neg"] else f"怪框 {res['mobs']}"
                    log(f"[{done}/{len(jobs)}] {res['id']} seed={res['seed']} {tag} 总框 {res['boxes']}")
                else:
                    log(f"[{done}/{len(jobs)}] {res['id']} seed={res['seed']} 跳过: {res['reason']}")
                if done % 50 == 0:
                    Path(__file__).parent.joinpath(
                        f"batch_{a.tag}_result.json").write_text(
                        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    Path(__file__).parent.joinpath(f"batch_{a.tag}_result.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = [r for r in results if r["ok"]]
    negs = [r for r in ok if r["neg"]]
    pos = [r for r in ok if not r["neg"] and r["mobs"] > 0]
    total_mobs = sum(r["mobs"] for r in pos)
    states: dict[str, int] = {}
    for r in pos:
        for k, v in r["states"].items():
            states[k] = states.get(k, 0) + v
    log(f"\n完成：正样本 {len(pos)} 张 · 负样本 {len(negs)} 张 · 失败/跳过 {len(results) - len(ok)}")
    log(f"怪物框共 {total_mobs} · 状态分布: {dict(sorted(states.items()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
