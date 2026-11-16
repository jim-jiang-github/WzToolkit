# -*- coding: utf-8 -*-
r"""合并 yolo-par0/1/2... 各分片的导出产物为一个最终数据集。

用法（根目录，全部分片跑完后）：
  .pyruntime\python.exe scripts\yolo_merge_runs.py [--src yolo-par0,yolo-par1,..] [--out yolo]
每个分片目录取其中最新时间戳的 run；合并 images/labels/annotations.jsonl，
重写 dataset.yaml 的 path，拷一份 _preview.jpg。
"""
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPORTS = ROOT / ".exports"

argv = sys.argv[1:]
srcs = (argv[argv.index("--src") + 1].split(",")
        if "--src" in argv else
        sorted(p.name for p in EXPORTS.iterdir()
               if p.is_dir() and p.name.startswith("yolo-par")))
outname = argv[argv.index("--out") + 1] if "--out" in argv else "yolo"
if not srcs:
    raise SystemExit("没有找到 yolo-par* 分片目录")

runs = []
for s in srcs:
    d = EXPORTS / s
    subs = sorted(p for p in d.iterdir() if p.is_dir() and p.name[0].isdigit())
    if not subs:
        print(f"跳过 {s}：没有 run")
        continue
    runs.append(subs[-1])
    print(f"{s}: {subs[-1].name}")
if not runs:
    raise SystemExit("所有分片都为空")

out = EXPORTS / outname / (time.strftime("%Y%m%d-%H%M%S") + "-merged")
for sp in ("images/train", "images/val", "labels/train", "labels/val"):
    (out / sp).mkdir(parents=True, exist_ok=True)

n = 0
with (out / "annotations.jsonl").open("w", encoding="utf-8") as ann:
    for r in runs:
        for split in ("train", "val"):
            src_i, src_l = r / "images" / split, r / "labels" / split
            dst_i, dst_l = out / "images" / split, out / "labels" / split
            for f in sorted(src_i.glob("*.jpg")):
                shutil.move(str(f), dst_i / f.name)
                lf = src_l / (f.stem + ".txt")
                if lf.exists():
                    shutil.move(str(lf), dst_l / lf.name)
                n += 1
        ap = r / "annotations.jsonl"
        if ap.exists():
            ann.write(ap.read_text(encoding="utf-8"))
        if not (out / "_preview.jpg").exists():
            pv = r / "_preview.jpg"
            if pv.exists():
                shutil.copy2(pv, out / "_preview.jpg")

shutil.copy2(runs[0] / "classes.txt", out / "classes.txt")
n_val = len(list((out / "images/val").glob("*.jpg")))
(out / "dataset.yaml").write_text(
    f"path: {out.as_posix()}\ntrain: images/train\nval: images/val\n"
    f"nc: 4\nnames: [\"mob\", \"boss\", \"drop\", \"player\"]\n",
    encoding="utf-8")
print(f"合并 {len(runs)} 个分片 → {out}")
print(f"图 {n} 张（val {n_val}），{sum(f.stat().st_size for f in out.rglob('*.jpg'))/1e9:.2f} GB")
