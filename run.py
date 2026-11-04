# -*- coding: utf-8 -*-
"""启动 WzToolkit WebUI。

    python run.py            # 默认 http://127.0.0.1:8791
    python run.py 9000       # 自定义端口
    双击 run.bat             # 等效：起服务 + 自动开浏览器

首次使用前先编译原生工具：powershell -File build.ps1
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for sub in ("bin", "exports", ".cache"):
    (ROOT / sub).mkdir(exist_ok=True)


def main() -> int:
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
    sys.path.insert(0, str(ROOT / "app"))
    import importlib
    mod = importlib.import_module("app.main")
    uvicorn.run(mod.app, host="127.0.0.1", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
