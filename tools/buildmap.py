"""Merge covmap per-process dumps into map.json.gz.

map = {
  "lines": {file: {line: [test numbers]}},   # runtime line -> tests (collect bucket excluded)
  "opens": {file: [test numbers]},           # non-py files read during a test
  "collect_opens": [files],                  # files read during collection (affects whole test file)
}
usage: buildmap.py COVOUT TESTS_JSON OUT
"""
import glob
import gzip
import json
import sys
from collections import defaultdict

covout, tests_json, out = sys.argv[1:4]
num = {t["id"]: t["number"] for t in json.load(open(tests_json))["tests"]}
byclass = defaultdict(list)
for i, n in num.items():
    byclass[i.rsplit("::", 1)[0]].append(n)
lines = defaultdict(lambda: defaultdict(set))
opens = defaultdict(set)
classes = defaultdict(set)
collect_opens = set()
seen = set()
def records():
    for fn in glob.glob(f"{covout}/*.json"):
        yield from json.load(open(fn)).items()
    for fn in glob.glob(f"{covout}/child-*.jsonl"):
        for line in open(fn):
            yield from json.loads(line).items()


for bucket, v in records():
    if True:
        if bucket == "<collect>":
            collect_opens.update(v["opens"])
            continue
        nodeid, _, phase = bucket.partition("\0")
        n = num.get(nodeid)
        if n is None:
            print("unknown test", bucket, file=sys.stderr)
            continue
        seen.add(n)
        # setup phase may hold setUpClass work: it affects every test of the class
        targets = byclass[nodeid.rsplit("::", 1)[0]] if phase else [n]
        for f, ls in v["lines"].items():
            for l in ls:
                lines[f][l].update(targets)
        for c in v.get("classes", ()):
            classes[c].update(targets)
        for f in v["opens"]:
            if not f.endswith((".py", ".pyc")):
                opens[f].update(targets)
print(f"tests seen {len(seen)}/{len(num)}", file=sys.stderr)
res = {
    "lines": {f: {str(l): sorted(s) for l, s in d.items()} for f, d in lines.items()},
    "opens": {f: sorted(s) for f, s in opens.items()},
    "collect_opens": sorted(collect_opens),
    "classes": {c: sorted(s) for c, s in classes.items()},
}
with gzip.open(out, "wt") as f:
    json.dump(res, f, separators=(",", ":"))
