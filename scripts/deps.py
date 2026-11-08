"""把 requirements.txt 装进 <项目根>\\.pylibs\\，供打包和源码模式使用。

为什么不用系统 Python 的 site-packages：
    C:\\Python312 里装没装、装的是哪个版本，别人电脑上完全不可控。
    依赖只落在项目目录，clone 下来跑一次就齐了，也不会污染全局环境。

用法：
    python scripts\\deps.py            # 缺依赖或 requirements.txt 变了才装
    python scripts\\deps.py --force    # 清掉 .pylibs 重装
    python scripts\\deps.py --check    # 只检查不装（退出码 0=齐了 / 1=需要装）

.pylibs\\.stamp.json 记录 requirements.txt 的 sha256 + 解释器版本，任一变化就整目录
重装（旧版本文件残留会让 import 结果和 requirements 对不上，不如重来）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / "requirements.txt"
TARGET = ROOT / ".pylibs"
STAMP = TARGET / ".stamp.json"

# 打包/运行真正要 import 的包（import 名），装完逐个验一遍
MODULES = ["PyInstaller", "PIL", "numpy", "cv2"]

PROBE = (
    "import json\n"
    "out = {}\n"
    "for m in %r:\n"
    "    try:\n"
    "        mod = __import__(m)\n"
    "        out[m] = getattr(mod, '__version__', '?')\n"
    "    except Exception:\n"
    "        out[m] = None\n"
    "print(json.dumps(out))\n" % (MODULES,)
)


def log(msg: str = "") -> None:
    print(msg, flush=True)


def req_digest() -> str:
    h = hashlib.sha256()
    h.update(REQ.read_bytes() if REQ.exists() else b"")
    h.update(sys.version.encode("utf-8"))
    h.update(sys.platform.encode("utf-8"))
    return h.hexdigest()


def read_stamp() -> str:
    try:
        return json.loads(STAMP.read_text(encoding="utf-8")).get("digest", "")
    except Exception:
        return ""


def probe() -> dict:
    """在子进程里按「只有 .pylibs」的环境探测版本。

    必须在子进程跑：本进程一开始就 import 过的话会命中 sys.modules 缓存，
    拿到的其实是系统 Python 的旧版本（踩过）。
    """
    if not TARGET.is_dir():
        return {m: None for m in MODULES}
    env = dict(os.environ)
    env["PYTHONPATH"] = str(TARGET)
    env["PYTHONNOUSERSITE"] = "1"
    try:
        p = subprocess.run([sys.executable, "-c", PROBE], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        data = json.loads(p.stdout.decode("utf-8", "replace") or "{}")
    except Exception:
        data = {}
    return {m: data.get(m) for m in MODULES}


def main() -> int:
    force = "--force" in sys.argv
    check_only = "--check" in sys.argv

    if not REQ.exists():
        log(f"[错误] 找不到 {REQ}")
        return 1

    digest = req_digest()
    versions = probe()
    ready = (not force) and digest == read_stamp() and all(versions.values())

    if ready:
        log("[依赖] .pylibs 已就绪，跳过安装：")
        for k, v in versions.items():
            log(f"        {k:<12} {v}")
        return 0

    if check_only:
        log("[依赖] 需要安装（缺包或 requirements.txt 已变更）：")
        for k, v in versions.items():
            log(f"        {k:<12} {v or '缺失'}")
        return 1

    if TARGET.is_dir():
        log(f"[依赖] 清掉旧的 {TARGET.name}\\ ...")
        try:
            shutil.rmtree(TARGET)
        except Exception as exc:
            log(f"[警告] 删不干净（{exc}），继续覆盖安装。")
        STAMP.unlink(missing_ok=True)

    log(f"[依赖] 安装到 {TARGET}")
    log(f"       解释器 {sys.version.split()[0]}  {sys.executable}")
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(REQ),
           "--target", str(TARGET), "--no-warn-script-location",
           "--disable-pip-version-check"]
    rc = subprocess.call(cmd)
    if rc != 0:
        log(f"[错误] pip 退出码 {rc}；检查网络或 pip 源后重试。")
        return rc

    versions = probe()
    missing = [k for k, v in versions.items() if not v]
    if missing:
        log(f"[错误] 装完仍导入失败：{', '.join(missing)}")
        return 1

    STAMP.write_text(json.dumps({"digest": digest, "python": sys.version,
                                 "versions": versions}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    log("[依赖] 完成：")
    for k, v in versions.items():
        log(f"        {k:<12} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
