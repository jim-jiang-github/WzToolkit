"""过夜全流程编排：导出 → 切分 → 训练 → 验证 → 弱类补数据 → 重训 → 终验 → 报告。

用法：python overnight_run.py --maps 350 --epochs 100 --imgsz 960
全程状态写 progress.log（在脚本目录），跑完写 train_report.md。
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
YDIR = ROOT / "exports" / "yolo"
LOG = HERE / "progress.log"
REPORT = HERE / "train_report.md"


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def sh(cmd: list) -> int:
    log("$ " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(ROOT))
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--maps", type=int, default=350)
    ap.add_argument("--win", type=int, default=0,
                    help="全覆盖网格之外的额外随机窗口数/图（默认 0）")
    ap.add_argument("--epochs", type=int, default=100)
    ap.add_argument("--imgsz", type=int, default=960)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--target", type=float, default=0.90, help="弱类 mAP50 目标线")
    ap.add_argument("--rounds", type=int, default=2, help="最多训练几轮（含补数据重训）")
    ap.add_argument("--tag", default="ov")
    a = ap.parse_args()
    t0 = time.time()
    LOG.write_text("", encoding="utf-8")
    log(f"过夜流程启动: maps={a.maps} win={a.win} epochs={a.epochs} imgsz={a.imgsz}")

    # ---- 1) 批量导出（正样本 + 15% 负样本）
    rc = sh([sys.executable, str(HERE / "batch_export.py"),
             "--maps", str(a.maps), "--win", str(a.win), "--tag", a.tag])
    if rc != 0:
        log(f"批量导出失败 rc={rc}")
        return 1

    # ---- 2) 切分 train/val
    rc = sh([sys.executable, str(HERE / "prep_split.py"), "0.2"])
    if rc != 0:
        log("切分失败")
        return 1

    # ---- 3~5) 训练 + 验证（最多 rounds 轮，弱类补数据）
    from ultralytics import YOLO
    data_yaml = str(YDIR / "dataset.yaml")
    run_dirs = []
    rounds_info = []
    for rnd in range(1, a.rounds + 1):
        log(f"===== 训练第 {rnd} 轮（epochs={a.epochs}）=====")
        model = YOLO("yolov8n.pt")
        res = model.train(data=data_yaml, epochs=a.epochs, imgsz=a.imgsz,
                          batch=a.batch, device="cpu", patience=25,
                          project=str(HERE / "runs"), name=f"round{rnd}",
                          exist_ok=True, verbose=True)
        run_dirs.append(str(res.save_dir))
        # 验证：per-class 指标
        m = YOLO(str(Path(res.save_dir) / "weights" / "best.pt"))
        val = m.val(data=data_yaml, imgsz=a.imgsz, split="val")
        pc = val.box.maps          # 每类 AP50-95
        pc50 = val.box.ap50        # 每类 AP50
        names = val.names          # dict id->name
        per = {names[i]: {"ap50": round(float(pc50[i]), 4),
                          "ap": round(float(pc[i]), 4)}
               for i in range(len(names)) if not _is_nan(pc[i])}
        weak = {k: v for k, v in per.items() if v["ap50"] < a.target}
        rounds_info.append({"round": rnd, "map50": round(float(val.box.map50), 4),
                            "map": round(float(val.box.map), 4), "per_class": per,
                            "weak": weak})
        log(f"第 {rnd} 轮: mAP50={val.box.map50:.4f} mAP50-95={val.box.map:.4f} "
            f"弱类(<{a.target}): {list(weak)[:10]}{'…' if len(weak) > 10 else ''}")
        if not weak or rnd == a.rounds:
            break
        # ---- 弱类补数据：把含这些怪的图多导几个窗口
        log(f"补数据: {len(weak)} 个弱类，导出额外窗口…")
        sh([sys.executable, str(HERE / "boost_weak.py"), json.dumps(list(weak)),
            "--win", "3", "--tag", f"b{rnd}"])
        sh([sys.executable, str(HERE / "prep_split.py"), "0.2"])

    # ---- 6) 终验：全新测试图推理 + 截图
    best = Path(run_dirs[-1]) / "weights" / "best.pt"
    sh([sys.executable, str(HERE / "predict_test.py"), str(best), "--n", "8"])

    # ---- 7) 报告
    rep = _report(a, rounds_info, t0)
    REPORT.write_text(rep, encoding="utf-8")
    log(f"全部完成，报告: {REPORT}")
    return 0


def _is_nan(x) -> bool:
    return x != x


def _report(a, rounds_info, t0) -> str:
    YDIR_ = YDIR
    reg = json.loads((YDIR_ / "classes.json").read_text(encoding="utf-8"))
    n_tr = len(list((YDIR_ / "images" / "train").glob("*.png")))
    n_va = len(list((YDIR_ / "images" / "val").glob("*.png")))
    try:
        match = json.loads((HERE / "test_out" / "match_report.json")
                           .read_text(encoding="utf-8"))
    except Exception:
        match = {}
    tot = match.get("totals", {})
    L = []
    L.append("# WzToolkit 怪物识别模型训练报告\n")
    L.append(f"- 训练数据: train {n_tr} 张 / val {n_va} 张（1366×768，含 15% 负样本）")
    L.append(f"- 类别数: {len(reg)}（怪 id_名字__状态 + rope/ladder/ground/slope）")
    L.append(f"- 训练配置: YOLOv8n · epochs={a.epochs} · imgsz={a.imgsz} · CPU")
    L.append(f"- 总耗时: {(time.time() - t0) / 3600:.1f} 小时\n")
    for r in rounds_info:
        L.append(f"## 第 {r['round']} 轮: mAP50={r['map50']:.4f} mAP50-95={r['map']:.4f}\n")
        rows = sorted(r["per_class"].items(), key=lambda kv: kv[1]["ap50"])
        L.append("| 类别 | AP50 | AP50-95 |")
        L.append("|---|---|---|")
        for nm, v in rows:
            L.append(f"| {nm} | {v['ap50']:.3f} | {v['ap']:.3f} |")
        if r["weak"]:
            L.append(f"\n弱类（<{a.target}）: {', '.join(r['weak'])}")
        L.append("")
    if tot:
        L.append("## 全新测试图实测（模型没见过的随机导出）\n")
        L.append(f"- 精确率 {tot.get('precision', 0):.1%} · 召回率 {tot.get('recall', 0):.1%}"
                 f" · F1 {tot.get('f1', 0):.3f}")
        L.append(f"- GT {tot.get('gt')} · 预测 {tot.get('pred')} · 命中 {tot.get('matched')}\n")
        L.append("### 每类命中")
        L.append("| 类别 | GT | 预测 | 命中 | 召回 |")
        L.append("|---|---|---|---|---|")
        for nm, v in sorted(match.get("per_class", {}).items()):
            L.append(f"| {nm} | {v['gt']} | {v['pred']} | {v['hit']} | {v.get('recall')} |")
    L.append("\n测试截图: .cache/_check/test_out/pred_*.png")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
