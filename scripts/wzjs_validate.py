"""对 json_573635e1 bundle 的全部 2000 个 WZJS 对象做大规模验证。

用 vendor/wzjs.py 逐对象解析 + 路径表 oracle 校验（解析器内部自带），
统计通过率、类型分布、值可读率。
"""
import sys, os, time
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import UnityPy
from vendor.wzjs import parse_blob, TYPE_SUB

W = r"C:\Program Files\上海数龙科技有限公司\冒险岛online\mxdclassic\Maplestory_Classic_Data\StreamingAssets\aa\w"


def main():
    t0 = time.time()
    env = UnityPy.load(W + r"\json_573635e18aaf09bb1af0a8f165b71c75.bundle")
    objs = [o for o in env.objects if o.type.name == "MonoBehaviour"]
    ok, fail = 0, 0
    types = Counter()
    numeric_maps = 0
    samples = []
    for o in objs:
        try:
            raw = o.get_raw_data()
            d = parse_blob(raw)
            ok += 1
            for nd in d["nodes"]:
                types[nd.type] += 1
            if d["name"].isdigit():
                numeric_maps += 1
                if len(samples) < 5:
                    v = d["nodes"]
                    info = {}
                    root = next(nd for nd in v if nd.parent < 0)
                    for nd in v:
                        if nd.path == "info/tS":
                            info["tS"] = nd.value
                        elif nd.path == "info/bgM":
                            info["bgM"] = nd.value
                        elif nd.path == "info/swim":
                            info["swim"] = nd.value
                    samples.append((d["name"], root.path, len(v), info))
        except Exception as e:
            fail += 1
            if fail <= 5:
                print("FAIL:", repr(e)[:160])
    print(f"解析通过 {ok} / {ok + fail}  耗时 {time.time()-t0:.1f}s")
    print(f"数字地图对象: {numeric_maps}  分类页: {ok - numeric_maps}")
    print(f"节点类型分布: {dict(sorted(types.items()))}")
    for s in samples:
        print("  样本:", s)


if __name__ == "__main__":
    main()
