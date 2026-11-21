"""pytest plugin: per-test line coverage + file reads via sys.monitoring.

Writes $COVMAP_OUT/<pid>.json: {bucket: {"lines": {file: [lines]}, "opens": [files]}}
bucket is "<collect>" or a test nodeid.
"""
import json
import os
import sys

ROOT = os.path.realpath(os.environ.get("COVMAP_ROOT", os.getcwd())) + os.sep
OUT = os.environ["COVMAP_OUT"]
mon = sys.monitoring
TOOL = mon.COVERAGE_ID
DISABLE = mon.DISABLE

data = {}
cur = {"lines": {}, "opens": set(), "classes": set()}
data["<collect>"] = cur
_ours = {}


def _is_ours(fn):
    r = _ours.get(fn)
    if r is None:
        rp = os.path.realpath(fn) if fn and not fn.startswith("<") else ""
        r = _ours[fn] = rp[len(ROOT):] if rp.startswith(ROOT) else False
    return r


def on_line(code, line):
    rel = _is_ours(code.co_filename)
    if rel:
        cur["lines"].setdefault(rel, set()).add(line)
    return DISABLE


def on_audit(event, args):
    if cur is None:
        return
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        try:
            p = os.fsdecode(args[0])
        except Exception:
            return
        rel = _is_ours(os.path.abspath(p))
        if rel:
            cur["opens"].add(rel)


_cls_cache = {}


def on_call(code, offset, fn, arg0):
    if isinstance(fn, type):
        names = _cls_cache.get(fn)
        if names is None:
            names = _cls_cache[fn] = [
                f"{k.__module__}:{k.__qualname__}"
                for k in fn.__mro__
                if (getattr(k, "__module__", "") or "").startswith("sqlglot")
            ]
        if names:
            cur["classes"].update(names)


mon.use_tool_id(TOOL, "covmap")
mon.register_callback(TOOL, mon.events.CALL, on_call)
mon.register_callback(TOOL, mon.events.LINE, on_line)
mon.set_events(TOOL, mon.events.LINE | mon.events.CALL)
sys.addaudithook(on_audit)


_curid = ["<collect>"]


def _child_call(nodeid, fn, *args, **kwargs):
    """Runs in a pool worker: attribute lines to the parent's current test."""
    global cur
    if _curid[0] != nodeid:
        _flush_child()
        _curid[0] = nodeid
        cur = data[nodeid] = {"lines": {}, "opens": set(), "classes": set()}
        mon.restart_events()
    try:
        return fn(*args, **kwargs)
    finally:
        _flush_child()


def _flush_child():
    if not data:
        return
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"child-{os.getpid()}.jsonl"), "a") as f:
        for k, v in data.items():
            if v["lines"] or v["opens"] or v["classes"]:
                f.write(json.dumps({k: {"lines": {a: sorted(b) for a, b in v["lines"].items()}, "opens": sorted(v["opens"]), "classes": sorted(v["classes"])}}) + "\n")
                v["lines"] = {}
                v["opens"] = set()
                v["classes"] = set()


import concurrent.futures as _cf

_orig_submit = _cf.ProcessPoolExecutor.submit


def _submit(self, fn, /, *args, **kwargs):
    return _orig_submit(self, _child_call, _curid[0], fn, *args, **kwargs)


_cf.ProcessPoolExecutor.submit = _submit


import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Setup phase (incl. setUpClass) gets its own bucket: buildmap spreads it to the whole class."""
    _curid[0] = item.nodeid + "\0setup"
    global cur
    cur = data[_curid[0]] = {"lines": {}, "opens": set(), "classes": set()}
    mon.restart_events()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    _curid[0] = item.nodeid
    global cur
    cur = data[item.nodeid] = {"lines": {}, "opens": set(), "classes": set()}


def pytest_sessionfinish(session):
    os.makedirs(OUT, exist_ok=True)
    out = {
        k: {"lines": {f: sorted(s) for f, s in v["lines"].items()}, "opens": sorted(v["opens"]), "classes": sorted(v["classes"])}
        for k, v in data.items()
    }
    with open(os.path.join(OUT, f"{os.getpid()}.json"), "w") as f:
        json.dump(out, f)
