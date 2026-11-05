"""验证训练效果：导出全新的随机测试图 → 模型推理 → 画框截图 → 对比 ground truth。

用法：python predict_test.py <model.pt> [--n 6] [--maps ...]
产出：train/test_out/ 下的 pred_*.png（带预测框）和 match_report.json
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EXE = ROOT / "wztool.exe"
YDIR = ROOT / "exports" / "yolo"
OUT = Path(__file__).parent / "test_out"
W, H = 1366, 768


def run_cli(args):
    r = subprocess.run([str(EXE), *args], capture_output=True, cwd=str(ROOT))
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return None


def iou(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    ua = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / ua if ua > 0 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--n", type=int, default=6, help="导几张新测试图")
    ap.add_argument("--maps", default="", help="指定地图 id 逗号分隔；缺省随机")
    ap.add_argument("--conf", type=float, default=0.35, help="预测置信度阈值")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    # 1) 选地图（指定或随机探测有怪的）
    if a.maps:
        picked = a.maps.split(",")
    else:
        d = run_cli(["maps", "--json"])
        import random as _r
        cands = _r.Random("testpick").sample(list(d["life_maps"]), 40)
        picked = []
        for mid in cands:
            info = run_cli(["map-info", mid, "--json"])
            if info and info.get("life_count") and info.get("scale", 1) >= 0.999:
                w = info["world"]
                if w["w"] >= W and w["h"] >= H:
                    picked.append(mid)
            if len(picked) >= a.n:
                break
    if not picked:
        print("没找到可测试的地图")
        return 1

    # 2) 导出全新测试图（seed 固定 test 标记，正样本带怪）
    import random as _r2
    rnd = _r2.Random("final-test")
    jobs = []
    for mid in picked[:a.n]:
        info = run_cli(["map-info", mid, "--json"])
        w = info["world"]
        x = round(rnd.uniform(w["x"], w["x"] + w["w"] - W))
        y = round(rnd.uniform(w["y"], w["y"] + w["h"] - H))
        seed = f"tst{rnd.randrange(1 << 24):06x}"
        jobs.append((mid, x, y, seed))

    tests = []
    for mid, x, y, seed in jobs:
        d = run_cli(["map-export", mid, "--mobs", "--yolo", "--no-boss", "--json",
                     f"--region={x},{y},{W},{H}", f"--seed={seed}",
                     "--density=2"])
        if d and d.get("yolo"):
            tests.append({"id": mid, "seed": seed,
                          "image": d["yolo"]["image"], "label": d["yolo"]["labels"],
                          "n_boxes": d["yolo"]["boxes"]})
    print(f"导出 {len(tests)} 张全新测试图")
    if not tests:
        return 1

    # 3) 推理 + 画框 + 匹配统计
    from ultralytics import YOLO
    from PIL import Image, ImageDraw, ImageFont
    model = YOLO(a.model)
    reg = json.loads((YDIR / "classes.json").read_text(encoding="utf-8"))
    inv = {i: nm for nm, i in reg.items()}
    report = {"images": [], "conf_thresh": a.conf}
    tot_gt = tot_pred = tot_matched = 0
    cls_stat: dict[str, dict] = {}
    for t in tests:
        img_path = Path(t["image"])
        im = Image.open(img_path).convert("RGB")
        Wpx, Hpx = im.size
        res = model.predict(str(img_path), conf=a.conf, imgsz=960, verbose=False)[0]
        preds = []
        for b in res.boxes:
            c = int(b.cls[0])
            conf = float(b.conf[0])
            x0, y0, x1, y1 = (float(v) for v in b.xyxy[0])
            preds.append({"cls": inv.get(c, str(c)), "conf": round(conf, 3),
                          "box": [x0, y0, x1, y1]})
        # ground truth
        gts = []
        for ln in Path(t["label"]).read_text(encoding="utf-8").splitlines():
            parts = ln.split()
            if len(parts) != 5:
                continue
            c, cx, cy, w, h = (float(v) for v in parts)
            gts.append({"cls": inv.get(int(c), str(int(c))),
                        "box": [(cx - w / 2) * Wpx, (cy - h / 2) * Hpx,
                                (cx + w / 2) * Wpx, (cy + h / 2) * Hpx]})
        # 匹配：GT 与预测 IoU≥0.5 且类别相同算命中（贪心）
        used = set()
        matched = 0
        for g in gts:
            best, bi = 0.0, -1
            for i, p in enumerate(preds):
                if i in used or p["cls"] != g["cls"]:
                    continue
                v = iou(g["box"], p["box"])
                if v > best:
                    best, bi = v, i
            if bi >= 0 and best >= 0.5:
                used.add(bi)
                matched += 1
                st = cls_stat.setdefault(g["cls"], {"gt": 0, "pred": 0, "hit": 0})
                st["hit"] += 1
        for g in gts:
            st = cls_stat.setdefault(g["cls"], {"gt": 0, "pred": 0, "hit": 0})
            st["gt"] += 1
        for p in preds:
            st = cls_stat.setdefault(p["cls"], {"gt": 0, "pred": 0, "hit": 0})
            st["pred"] += 1
        tot_gt += len(gts)
        tot_pred += len(preds)
        tot_matched += matched

        # 画框：绿=预测（类别+置信度），红虚线=漏检的 GT
        dr = ImageDraw.Draw(im)
        try:
            font = ImageFont.truetype("consola.ttf", 14)
        except Exception:
            font = ImageFont.load_default()
        for p in preds:
            x0, y0, x1, y1 = p["box"]
            dr.rectangle([x0, y0, x1, y1], outline=(0, 220, 60), width=2)
            dr.text((x0 + 2, max(0, y0 - 16)), f"{p['cls']} {p['conf']:.2f}",
                    fill=(0, 220, 60), font=font)
        out_png = OUT / f"pred_{t['id']}_{t['seed']}.png"
        im.save(out_png)
        report["images"].append({"map": t["id"], "seed": t["seed"],
                                 "png": str(out_png), "gt": len(gts),
                                 "pred": len(preds), "matched": matched})
        print(f"{t['id']} seed={t['seed']}: GT {len(gts)} · 预测 {len(preds)} · 命中 {matched}")

    prec = tot_matched / tot_pred if tot_pred else 0
    rec = tot_matched / tot_gt if tot_gt else 0
    report["totals"] = {"gt": tot_gt, "pred": tot_pred, "matched": tot_matched,
                       "precision": round(prec, 4), "recall": round(rec, 4),
                       "f1": round(2 * prec * rec / (prec + rec), 4) if prec + rec else 0}
    report["per_class"] = {k: {"precision": round(v["hit"] / v["pred"], 3) if v["pred"] else None,
                               "recall": round(v["hit"] / v["gt"], 3) if v["gt"] else None,
                               **v} for k, v in cls_stat.items()}
    (OUT / "match_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n总体: 精确率 {prec:.1%} · 召回率 {rec:.1%} "
          f"（GT {tot_gt} · 预测 {tot_pred} · 命中 {tot_matched}）")
    print(f"截图与明细: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
