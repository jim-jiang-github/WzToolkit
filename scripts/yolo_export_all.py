# -*- coding: utf-8 -*-
r"""YOLO 全库导出驱动：不走 web 服务，直接调 _yolo_run。

用法（根目录）：
  set PYTHONPATH=.pylibs&& set PYTHONNOUSERSITE=1&& .pyruntime\python.exe scripts\yolo_export_all.py
      [--layouts 4] [--layout-offset 2] [--sample 3] [--shard i --nshards N] [--maxviews 32]

--sample N    只导前 N 张图（试跑估时/估盘用）
--layout-offset K  布局序号从 K 起（补导第 K 套布局，seed 错开不与前套重复）
--shard/--nshards  分片并行：本进程只导 maps[i::N]，输出到 .exports\yolo-par<i>
分片跑完后再跑 scripts\yolo_merge_runs.py 合并成最终数据集。
进度每 30s 写 .cache\yolo-export-status<suffix>.json，日志追加 .cache\yolo-export.log
"""
import sys
import json
import time
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import wztool  # noqa: E402

LAYOUTS = 2          # 用户 2026-11-17 00:26：布局 4 体积太大，改 2（减半）
FLAGS = {"mob": True, "boss": True, "drop": True, "player": True}
VAL_PCT = 0          # 用户 2026-11-17：验证集取消，全部进 train（校对走训后本地生成对比）
SIZE = (1366, 768)
MAX_VIEWS = 32

argv = sys.argv[1:]
sample = 0
if "--sample" in argv:
    sample = int(argv[argv.index("--sample") + 1])
if "--layouts" in argv:
    LAYOUTS = int(argv[argv.index("--layouts") + 1])
if "--maxviews" in argv:
    MAX_VIEWS = int(argv[argv.index("--maxviews") + 1])
shard = int(argv[argv.index("--shard") + 1]) if "--shard" in argv else 0
nshards = int(argv[argv.index("--nshards") + 1]) if "--nshards" in argv else 1
li_off = int(argv[argv.index("--layout-offset") + 1]) if "--layout-offset" in argv else 0
if not (0 <= shard < max(1, nshards)):
    raise SystemExit("--shard 必须在 [0, nshards) 内")

cache = ROOT / ".cache"
cache.mkdir(exist_ok=True)
# .cache 下的 python 写入近两天频发 PermissionError（原因未明，curl/Write 工具正常），
# 日志和状态改写到 .exports\_runlogs\（图片一直在 .exports 下写、从没出过事）
rlog = ROOT / ".exports" / "_runlogs"
rlog.mkdir(parents=True, exist_ok=True)
tag = f"-l{li_off}" if li_off else ""
logf = (rlog / f"yolo-export{tag}#{shard}of{nshards}.log").open("a", encoding="utf-8")


def log(msg: str) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}#{shard}] {msg}"
    print(line, flush=True)
    logf.write(line + "\n")
    logf.flush()


maps = [m["id"] for m in wztool.c_cls_maps()["maps"] if m.get("mob")]
if sample:
    maps = maps[:sample]
maps = maps[shard::nshards]
out_root = wztool.EXPORTS / ("yolo" if nshards <= 1 else f"yolo-par{shard}")
log(f"怀旧服有怪地图 共{len(maps)}张(本分片) layouts={LAYOUTS} "
    f"max_views={MAX_VIEWS} flags={FLAGS} out={out_root.name}")

err = []


def run():
    try:
        wztool._yolo_run(maps, "cls", LAYOUTS, [SIZE], VAL_PCT, FLAGS,
                         out_root, None, False, MAX_VIEWS, li_off)
    except Exception as e:                     # noqa: BLE001
        err.append(str(e))
        log(f"异常退出：{e}")


th = threading.Thread(target=run, daemon=True)
th.start()

last_done = -1
sfx = ("" if nshards <= 1 else str(shard)) + tag
while th.is_alive():
    time.sleep(30)
    j = wztool.c_yolo_status()
    if j["done"] != last_done:
        last_done = j["done"]
        log(f"stage={j['stage']} done={j['done']}/{j['total']} "
            f"images={j['images']} boxes={j['boxes']} failed={len(j['failed'])}")
    # 状态文件写入失败不致命（环境偶发 PermissionError 不该杀掉导出）
    try:
        (rlog / f"yolo-export-status{sfx}.json").write_text(
            json.dumps({k: v for k, v in j.items() if k != "log"},
                       ensure_ascii=False, default=str), encoding="utf-8")
    except OSError:
        pass

j = wztool.c_yolo_status()
log(f"结束 stage={j['stage']} error={j['error'] or (err[0] if err else '')} "
    f"images={j['images']} out={j['out']}")
