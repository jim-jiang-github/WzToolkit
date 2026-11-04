"""把 exports/yolo 摊平的 images/labels 收进 train/val（默认 8:2），重写 dataset.yaml。

幂等：wztool 每次导出都把图写在 images/ 根目录，这个脚本把「还没分过的」
按比例分进 train/val；已经分好的不动。负样本（label 只有绳梯或全空）一样参与。
"""
import json
import random
import sys
from pathlib import Path

YDIR = Path(__file__).resolve().parents[1] / "exports" / "yolo"


def main(ratio: float = 0.2, seed: int = 42) -> int:
    img_root, lbl_root = YDIR / "images", YDIR / "labels"
    if not img_root.exists():
        print("还没有导出数据（exports/yolo/images 不存在）")
        return 1
    for d in ("train", "val"):
        (img_root / d).mkdir(exist_ok=True)
        (lbl_root / d).mkdir(exist_ok=True)

    # 已分过的（在 train/val 里）就不动；根目录的散图按 stem 分流
    fresh = sorted(p for p in img_root.glob("*.png") if p.is_file())
    rnd = random.Random(seed)
    n_tr = n_va = 0
    for p in fresh:
        stem = p.stem
        lbl = lbl_root / f"{stem}.txt"
        if not lbl.exists():        # 图有 label 才收（label 缺失=损坏，跳过）
            print(f"跳过（无 label）: {p.name}")
            continue
        dst = "val" if rnd.random() < ratio else "train"
        shutil_move(p, img_root / dst / p.name)
        shutil_move(lbl, lbl_root / dst / lbl.name)
        if dst == "train":
            n_tr += 1
        else:
            n_va += 1

    # 统计
    counts = {}
    for d in ("train", "val"):
        imgs = list((img_root / d).glob("*.png"))
        lbls = list((lbl_root / d).glob("*.txt"))
        # 校验 label 与图一一对应
        missing = [p.stem for p in imgs
                   if not (lbl_root / d / f"{p.stem}.txt").exists()]
        counts[d] = {"images": len(imgs), "labels": len(lbls), "img_no_label": len(missing)}
        if missing:
            print(f"警告: {d} 有 {len(missing)} 张图缺 label（ultralytics 会当负样本）")

    # dataset.yaml
    reg = json.loads((YDIR / "classes.json").read_text(encoding="utf-8"))
    inv = sorted(reg.items(), key=lambda kv: kv[1])
    names = "\n".join(f"  {i}: {nm}" for nm, i in inv)
    (YDIR / "dataset.yaml").write_text(
        f"path: {YDIR.resolve().as_posix()}\ntrain: images/train\nval: images/val\n"
        f"names:\n{names}\n", encoding="utf-8")
    print(f"切分完成: train {counts['train']['images']} 张 · val {counts['val']['images']} 张"
          f"（本次新增 train {n_tr} / val {n_va}）· 类别 {len(inv)}")
    return 0


def shutil_move(src: Path, dst: Path):
    import shutil
    dst = Path(dst)
    if dst.exists():
        dst.unlink()
    shutil.move(str(src), str(dst))


if __name__ == "__main__":
    ratio = float(sys.argv[1]) if len(sys.argv) > 1 else 0.2
    sys.exit(main(ratio))
