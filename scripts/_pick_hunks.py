"""按关键字挑 git diff 的 hunk 写 patch（二进制安全，避免 CRLF 翻译）。
用法: python scripts/_pick_hunks.py <pattern1> <pattern2> ...  (任一命中即选中)
patch 写到 .build/pick.patch 并 git apply --cached。"""
import subprocess, sys, os

pats = sys.argv[1:]
out = subprocess.run(["git", "diff", "-U3", "--", "src/wztool.py"],
                     capture_output=True, check=True)
raw = out.stdout
lines = raw.split(b"\n")
header, hunks, cur = [], [], None
for ln in lines:
    if ln.startswith(b"@@"):
        if cur is not None:
            hunks.append(cur)
        cur = [ln]
    elif cur is None:
        header.append(ln)
    else:
        cur.append(ln)
if cur is not None:
    hunks.append(cur)

sel = []
for h in hunks:
    body = b"\n".join(h)
    if any(p.encode("utf-8") in body for p in pats):
        sel.append(h)

patch = b"\n".join(header) + b"\n" + b"\n".join(b"\n".join(h) for h in sel) + b"\n"
os.makedirs(".build", exist_ok=True)
with open(".build/pick.patch", "wb") as f:
    f.write(patch)
r = subprocess.run(["git", "apply", "--cached", ".build/pick.patch"],
                   capture_output=True)
print(r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace"))
sys.exit(r.returncode)
