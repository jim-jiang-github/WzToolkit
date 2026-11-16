# -*- coding: utf-8 -*-
r"""YOLO 全库导出驱动：不走 web 服务，直接调 _yolo_run。

用法（根目录）：
  set PYTHONPATH=.pylibs&& set PYTHONNOUSERSITE=1&& .pyruntime\python.exe scripts\yolo_export_all.py [--layouts 4] [--sample 3]

--sample N  只导前 N 张图（试跑估时/估盘用）
进度每 30s 写 .cache\yolo-export-status.json，日志追加 .cache\yolo-export.log
"""
import sys
import json
import time
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import wztool  # noqa: E402

LAYOUTS = 4
FLAGS = {"mob": True, "boss": True, "drop": True, "player": True}
VAL_PCT = 15
SIZE = (1366, 768)

argv = sys.argv[1:]
sample = 0
if "--sample" in argv:
    sample = int(argv[argv.index("--sample") + 1])

cache = ROOT / ".cache"
cache.mkdir(exist_ok=True)
logf = (cache / "yolo-export.log").open("a", encoding="utf-8")


def log(msg: str) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    logf.write(line + "\n")
    logf.flush()


maps = [m["id"] for m in wztool.c_cls_maps()["maps"] if m.get("mob")]
log(f"怀旧服有怪地图 {len(maps)} 张；layouts={LAYOUTS} flags={FLAGS} sample={sample or '全部'}")
if sample:
    maps = maps[:sample]

err = []


def run():
    try:
        wztool._yolo_run(maps, "cls", LAYOUTS, [SIZE], VAL_PCT, FLAGS,
                         wztool.EXPORTS / "yolo", None, False)
    except Exception as e:                     # noqa: BLE001
        err.append(str(e))
        log(f"异常退出：{e}")


th = threading.Thread(target=run, daemon=True)
th.start()

last_done = -1
while th.is_alive():
    time.sleep(30)
    j = wztool.c_yolo_status()
    if j["done"] != last_done:
        last_done = j["done"]
        log(f"stage={j['stage']} done={j['done']}/{j['total']} "
            f"images={j['images']} boxes={j['boxes']} failed={len(j['failed'])}")
    (cache / "yolo-export-status.json").write_text(
        json.dumps({k: v for k, v in j.items() if k != "log"},
                   ensure_ascii=False, default=str), encoding="utf-8")

j = wztool.c_yolo_status()
log(f"结束 stage={j['stage']} error={j['error'] or (err[0] if err else '')} "
    f"images={j['images']} out={j['out']}")
