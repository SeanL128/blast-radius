"""Per sample commit: changed lines, runtime-coverage candidates vs failing."""
import gzip
import json
import re
import ast
import os
import sys
from functools import lru_cache

W = sys.argv[1]
m = json.load(gzip.open(f"{W}/work/map.json.gz"))
T = {t["number"]: t for t in json.load(open(f"{W}/kit/sample/tests.json"))["tests"]}
A = json.load(open(f"{W}/kit/sample/answers.json"))["commits"]


def changed(patch):
    """file -> set of old-side line numbers touched (removed lines, plus neighbours of insertions)."""
    out = {}
    f = None
    for line in patch.splitlines():
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            f = line[6:] if line.startswith("+++ b/") else None
            out.setdefault(f, set())
            continue
        h = re.match(r"@@ -(\d+)(?:,\d+)? \+", line)
        if h:
            old = int(h.group(1))
            continue
        if f is None or not line:
            continue
        c = line[0]
        if c == " ":
            old += 1
        elif c == "-":
            out[f].add(old)
            old += 1
        elif c == "+":
            out[f].update({old - 1, old})
    return out


@lru_cache(None)
def stmts(f):
    """list of (lo, hi) line ranges: simple statements fully, compound statements header only."""
    path = f"{W}/sqlglot/{f}"
    if not f.endswith(".py") or not os.path.exists(path):
        return []
    out = []
    for n in ast.walk(ast.parse(open(path).read())):
        if isinstance(n, ast.stmt):
            hi = n.end_lineno
            body = getattr(n, "body", None)
            if isinstance(body, list) and body:
                hi = min(hi, body[0].lineno - 1, *(d.lineno - 1 for d in getattr(n, "decorator_list", []) if d.lineno > n.lineno))
                hi = max(hi, n.lineno)
            out.append((n.lineno, hi))
    return out


def expand(f, ls):
    res = set(ls)
    for lo, hi in stmts(f):
        if any(lo <= l <= hi for l in ls):
            res.update(range(lo, hi + 1))
    return res


COST = {}
for cid, ans in sorted(A.items()):
    patch = open(f"{W}/kit/sample/commits/{cid}.patch").read()
    ch = changed(patch)
    cand = set()
    for f, ls in ch.items():
        fl = m["lines"].get(f, {})
        for l in expand(f, ls):
            cand.update(fl.get(str(l), []))
        cand.update(m["opens"].get(f, []))
    fail = set(ans["failing"])
    miss = fail - cand
    secs = sum(T[n]["seconds"] for n in cand)
    COST[cid] = (cand, fail)
    print(f"{cid} {ans['kind']:8} fail={len(fail):2} cand={len(cand):4} {secs:6.1f}s miss={sorted(miss)} {dict((f, sorted(l)) for f, l in ch.items())}")

import pickle
pickle.dump(COST, open(f"{W}/work/cand.pkl", "wb"))
