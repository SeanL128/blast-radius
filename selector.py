"""Blast Radius selector.

For each patch:
1. Static candidates from a map built at the pinned commit: per-test line coverage,
   per-test file reads, and per-test class instantiations.
2. Cheap candidates are run for real in a forked child of a warm process that has
   sqlglot and the test modules imported; expensive ones are selected blind.
3. Selected = failing cheap tests + blind expensive tests, packed onto workers.
"""

import ast
import builtins
import gzip
import importlib
import importlib.abc
import importlib.util
import io
import json
import os
import pickle
import re
import signal
import sys
import time
import tokenize
import traceback

BASE = os.environ.get("SQLGLOT_DIR", "/opt/sqlglot")
DATA = os.environ.get("DATA_DIR", "/data")
HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_COST = 2.0
RUN_OVERHEAD = 0.15  # fixed CPU seconds charged when a commit runs anything
HEAVY_T = float(os.environ.get("HEAVY_T", "2.0"))  # tests at least this slow are never run
CPU_FACTOR = float(os.environ.get("CPU_FACTOR", "1.0"))  # our CPU per recorded test second
# The run gets 180 s for ~40 commits (270 s for ~60 in the final): ~4.5 s per commit.
# We plan for SECS_PER_COMMIT and stop running tests (select candidates blind) when it runs out.
SECS_PER_COMMIT = float(os.environ.get("SECS_PER_COMMIT", "3.0"))
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:6.1f}]", *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- inputs

TESTS = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
NUM = {t["id"]: t["number"] for t in TESTS}
SECS = {t["number"]: t["seconds"] for t in TESTS}
ALL = frozenset(SECS)
BY_FILE = {}
for t in TESTS:
    BY_FILE.setdefault(t["file"], set()).add(t["number"])

MAP = json.load(gzip.open(os.path.join(HERE, "map.json.gz"), "rt"))
LINES = MAP["lines"]
OPENS = MAP["opens"]
COLLECT_OPENS = set(MAP["collect_opens"])
CLASSES = MAP["classes"]


def mod_name(path):
    p = path[:-3] if path.endswith(".py") else path
    p = p.replace("/", ".")
    return p[: -len(".__init__")] if p.endswith(".__init__") else p


def test_parts(tid):
    file, cls, meth = tid.split("::")
    return mod_name(file), cls, meth


# ---------------------------------------------------------------- patches


def parse_patch(text):
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        l = lines[i]
        if l.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "binary": False, "created": False, "deleted": False}
            files.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", l)
            if m:
                cur["old"], cur["new"] = m.groups()
        elif l.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if cur is None or cur["hunks"]:
                cur = {"old": None, "new": None, "hunks": [], "binary": False, "created": False, "deleted": False}
                files.append(cur)
            a = lines[i][4:].split("\t")[0]
            b = lines[i + 1][4:].split("\t")[0]
            cur["old"] = None if a == "/dev/null" else (a[2:] if a.startswith("a/") else a)
            cur["new"] = None if b == "/dev/null" else (b[2:] if b.startswith("b/") else b)
            cur["created"] = cur["old"] is None
            cur["deleted"] = cur["new"] is None
            i += 2
            continue
        elif cur is not None and l.startswith("rename from "):
            cur["old"] = l[len("rename from "):]
        elif cur is not None and l.startswith("rename to "):
            cur["new"] = l[len("rename to "):]
        elif cur is not None and l.startswith("new file mode"):
            cur["created"] = True
        elif cur is not None and l.startswith("deleted file mode"):
            cur["deleted"] = True
        elif cur is not None and (l.startswith("Binary files") or l.startswith("GIT binary patch")):
            cur["binary"] = True
        elif cur is not None and l.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", l)
            os_, oc = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            nc = int(m.group(4) if m.group(4) is not None else 1)
            body = []
            i += 1
            ro, rn = oc, nc
            while i < len(lines) and (ro > 0 or rn > 0):
                b = lines[i]
                if b.startswith("\\"):
                    i += 1
                    continue
                c = b[:1] or " "
                if c == " ":
                    ro -= 1
                    rn -= 1
                elif c == "-":
                    ro -= 1
                elif c == "+":
                    rn -= 1
                else:
                    break
                body.append((c, b[1:]))
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
            cur["hunks"].append((os_, oc, body))
            continue
        i += 1
    for f in files:
        if f["created"]:
            f["old"] = None
        if f["deleted"]:
            f["new"] = None
    return files


def apply_hunks(old_text, hunks):
    """-> (new_text, changed old line numbers (1-based); insertions mark both neighbours)."""
    old = old_text.split("\n") if old_text else []
    out, pos, changed = [], 0, set()
    for os_, oc, body in hunks:
        old_seg = [t for c, t in body if c in " -"]
        new_seg = [t for c, t in body if c in " +"]
        start = os_ - 1 if oc > 0 else os_
        if old[start:start + len(old_seg)] != old_seg:
            found = None
            for d in range(1, 400):
                for s in (start - d, start + d):
                    if s >= pos and old[s:s + len(old_seg)] == old_seg:
                        found = s
                        break
                if found is not None:
                    break
            if found is None:
                raise ValueError("hunk does not apply")
            start = found
        out.extend(old[pos:start])
        out.extend(new_seg)
        ln = start + 1  # 1-based number of the next old line
        for c, _ in body:
            if c == " ":
                ln += 1
            elif c == "-":
                changed.add(ln)
                ln += 1
            else:
                changed.update((ln - 1, ln))
        pos = start + len(old_seg)
    out.extend(old[pos:])
    return "\n".join(out), changed


def read_base(path):
    try:
        with open(os.path.join(BASE, path), encoding="utf-8", newline="") as f:
            return f.read()
    except FileNotFoundError:
        return None


def code_tokens(src):
    toks = []
    for t in tokenize.generate_tokens(io.StringIO(src).readline):
        if t.type in (tokenize.COMMENT, tokenize.NL):
            continue
        toks.append((t.type, t.string))
    return toks


# ---------------------------------------------------------------- static analysis

_ast_cache = {}


def analyze(path, src):
    """-> (stmt ranges [(lo, hi)], line -> ("func"|"import"|"doc", class qualname or None))."""
    if path in _ast_cache:
        return _ast_cache[path]
    tree = ast.parse(src)
    ranges = []
    kind = {}

    def body_start(n):
        body = getattr(n, "body", None)
        return body[0].lineno if isinstance(body, list) and body else n.end_lineno + 1

    def mark(lo, hi, k):
        for l in range(lo, hi + 1):
            kind.setdefault(l, k)

    def visit(node, cls, infunc):
        for c in ast.iter_child_nodes(node):
            if not isinstance(c, ast.stmt):
                continue
            lo = min([c.lineno] + [d.lineno for d in getattr(c, "decorator_list", [])])
            bs = body_start(c)
            hdr_hi = max(c.lineno, min(c.end_lineno, bs - 1))
            ranges.append((lo, hdr_hi if hasattr(c, "body") else c.end_lineno))
            body = getattr(c, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str) and isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                mark(body[0].lineno, body[0].end_lineno, ("doc", cls))
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                mark(lo, hdr_hi, ("func" if infunc else "import", cls))
                visit(c, cls, True)
                mark(lo, c.end_lineno, ("func", cls))
            elif isinstance(c, ast.ClassDef):
                q = c.name if cls is None else f"{cls}.{c.name}"
                mark(lo, hdr_hi, ("func" if infunc else "import", cls))
                visit(c, q, infunc)
                mark(lo, c.end_lineno, ("func" if infunc else "import", q))
            else:
                visit(c, cls, infunc)
                mark(lo, c.end_lineno, ("func" if infunc else "import", cls))

    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(getattr(tree.body[0], "value", None), ast.Constant):
        mark(tree.body[0].lineno, tree.body[0].end_lineno, ("doc", None))
    visit(tree, None, False)
    _ast_cache[path] = (ranges, kind)
    return ranges, kind


def users(path):
    s = set()
    for ts in LINES.get(path, {}).values():
        s.update(ts)
    return s


def classes_in(path):
    pre = mod_name(path) + ":"
    s = set()
    for k, ts in CLASSES.items():
        if k.startswith(pre):
            s.update(ts)
    return s


def file_candidates(f, base_src, groups):
    """-> (candidates, needs_reimport, note) for one file diff.

    Appends to `groups` one set of tests per specific reason (a changed line, a class,
    a data file). Notes starting with "weak" mean a broad fallback was used."""
    old, new = f["old"], f["new"]
    if f["binary"]:
        p = old or new
        if p and not p.endswith(".py"):
            return set(OPENS.get(p, ())) | (ALL if p in COLLECT_OPENS else set()), False, "weak-binary"
        return set(ALL), True, "weak-binary-py"
    path = old or new
    if old is not None and new is not None and old != new:
        # rename: old module gone, new one appears
        if old.endswith(".py"):
            return users(old) | classes_in(old) | (ALL if old.startswith(("sqlglot/", "tests/")) else set()), True, "weak-rename"
        return set(OPENS.get(old, ())), False, "weak-rename-data"
    if not path.endswith(".py"):
        if f["created"] or base_src is None:
            return set(), False, "new-data"
        if path in COLLECT_OPENS:
            return set(ALL), True, "weak-collect-data"
        groups.append(set(OPENS.get(path, ())))
        return set(groups[-1]), False, "data"
    if f["created"] or base_src is None:
        # a new module only matters if something imports it, which another hunk would show
        return set(), True, "new-py"
    if f["deleted"]:
        return users(path) | classes_in(path) | (ALL if path.startswith("sqlglot/") else set()), True, "weak-deleted-py"
    try:
        new_src, changed = apply_hunks(base_src, f["hunks"])
    except ValueError:
        return set(ALL), True, "weak-apply-failed"
    try:
        if code_tokens(base_src) == code_tokens(new_src):
            return set(), False, "comment-only"
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass
    try:
        ranges, kind = analyze(path, base_src)
    except SyntaxError:
        return set(ALL), True, "weak-base-unparsable"
    cand, reimport, note = set(), False, "py"
    fl = LINES.get(path, {})
    is_test_file = path.startswith("tests/")
    for l in changed:
        k, cls = kind.get(l, ("import", None))
        if k == "doc":
            g = set(BY_FILE.get("tests/test_docs.py", set()))
            groups.append(g)
            cand |= g
            continue
        if k == "func":
            lines = {l}
            for lo, hi in ranges:
                if lo <= l <= hi:
                    lines.update(range(lo, hi + 1))
            g = set()
            for x in lines:
                g.update(fl.get(str(x), ()))
            groups.append(g)
            cand |= g
            continue
        reimport = True
        if is_test_file:
            if os.path.basename(path).startswith("test_"):
                g = set(BY_FILE.get(path, set()))
                groups.append(g)
                cand |= g
            else:
                cand |= ALL
                note = "weak-test-helper"
            continue
        if cls:
            key = f"{mod_name(path)}:{cls}"
            g = set(CLASSES.get(key, ())) | set(fl.get(str(l), ()))
        else:
            g = users(path) | classes_in(path)
        groups.append(g)
        cand |= g
    return cand, reimport, note


# ---------------------------------------------------------------- packing


def pack(nums):
    """Best LPT split over 1..8 workers -> (cost, workers)."""
    nums = sorted(nums, key=lambda n: -SECS[n])
    if not nums:
        return 0.0, []
    best = None
    for k in range(1, min(8, len(nums)) + 1):
        w = [[] for _ in range(k)]
        load = [0] * k
        for n in nums:
            i = load.index(min(load))
            w[i].append(n)
            load[i] += round(SECS[n] * 1000)
        cost = max(load) / 1000 + WORKER_COST * k
        if best is None or cost < best[0]:
            best = (cost, w)
    return best


def conditional_heavies(cand, groups, notes):
    """Heavy test -> cheap sentinel tests. A heavy test is skipped when every reason it was
    picked for is shared by cheap tests that we run, and it is selected if any of them fails.
    ponytail: sentinel rule is a bet (miss = 300 s vs ~20 s saved); disable with HEAVY_T=1e9."""
    if any(":weak" in x or x.startswith("weak") for x in notes):
        return {}
    out = {}
    for h in cand:
        if SECS[h] < HEAVY_T:
            continue
        gs = [g for g in groups if h in g]
        if not gs:
            continue
        sn = set()
        for g in gs:
            cheap = {n for n in g if SECS[n] < HEAVY_T}
            if not cheap:
                break
            sn |= cheap
        else:
            out[h] = sn
    return out


def plan(cand):
    """Split candidates into (run, blind) minimizing run CPU + blind packing cost."""
    order = sorted(cand, key=lambda n: -SECS[n])
    run_cost = sum(SECS[n] for n in order) * CPU_FACTOR
    best = (run_cost + (RUN_OVERHEAD if order else 0), 0)
    for j in range(1, len(order) + 1):
        run_cost -= SECS[order[j - 1]] * CPU_FACTOR
        c = run_cost + (RUN_OVERHEAD if j < len(order) else 0) + pack(order[:j])[0]
        if c < best[0]:
            best = (c, j)
    j = best[1]
    return set(order[j:]), set(order[:j])


# ---------------------------------------------------------------- runner (child side)


class InlinePool:
    """ProcessPoolExecutor stand-in: runs inline, but keeps the pickle round trip."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **k):
        if initializer is not None:
            initializer, initargs = pickle.loads(pickle.dumps((initializer, initargs)))
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        from concurrent.futures import Future

        fut = Future()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            fut.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except TestTimeout:
            raise
        except BaseException as e:
            fut.set_exception(e)
        return fut

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [self.submit(fn, *a).result() for a in zip(*iterables)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestTimeout(BaseException):
    pass


class StopTest(Exception):
    pass


def install_inline_pool():
    import concurrent.futures
    import concurrent.futures.process

    concurrent.futures.ProcessPoolExecutor = InlinePool
    concurrent.futures.process.ProcessPoolExecutor = InlinePool


class PatchedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, sources):
        self.sources = sources  # module name -> (filename, source or None for deleted)

    def find_spec(self, name, path=None, target=None):
        if name not in self.sources:
            return None
        filename, src = self.sources[name]
        if src is None:
            raise ModuleNotFoundError(name)
        is_pkg = filename.endswith("__init__.py")
        return importlib.util.spec_from_loader(name, PatchedLoader(filename, src), origin=filename, is_package=is_pkg)


class PatchedLoader(importlib.abc.Loader):
    def __init__(self, filename, src):
        self.filename, self.src = filename, src

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__ = self.filename
        if self.filename.endswith("__init__.py"):
            module.__path__ = [os.path.dirname(self.filename)]
        exec(compile(self.src, self.filename, "exec"), module.__dict__)


def redirect_open(mapping):
    real_open = io.open

    def opener(file, *a, **k):
        try:
            p = os.path.abspath(os.fsdecode(file)) if isinstance(file, (str, bytes, os.PathLike)) else None
        except Exception:
            p = None
        if p in mapping:
            file = mapping[p]
        return real_open(file, *a, **k)

    builtins.open = opener
    io.open = opener


def iter_functions(module):
    """Yield every function object reachable from a module's namespace and its classes."""
    seen = set()
    modname = module.__name__

    def fn_of(v):
        if isinstance(v, (staticmethod, classmethod)):
            v = v.__func__
        while hasattr(v, "__wrapped__") and not hasattr(v, "__code__"):
            v = v.__wrapped__
        return v if hasattr(v, "__code__") else None

    def walk(ns):
        for v in list(ns.values()):
            if id(v) in seen:
                continue
            seen.add(id(v))
            if isinstance(v, type):
                if getattr(v, "__module__", None) == modname:
                    yield from walk(v.__dict__)
                continue
            if isinstance(v, property):
                for g in (v.fget, v.fset, v.fdel):
                    if g is not None and hasattr(g, "__code__"):
                        yield g
                continue
            f = fn_of(v)
            if f is not None and getattr(f, "__module__", None) == modname:
                yield f
                w = getattr(f, "__wrapped__", None)
                if w is not None and hasattr(w, "__code__"):
                    yield w

    yield from walk(module.__dict__)


def hot_swap(modname, filename, new_src):
    """Swap function code objects in a live module. False if it cannot be done safely."""
    module = sys.modules.get(modname)
    if module is None:
        return True  # not imported yet: the finder serves the new source on import
    new_code = compile(new_src, filename, "exec")
    by_q = {}
    stack = [new_code]
    while stack:
        c = stack.pop()
        for k in c.co_consts:
            if hasattr(k, "co_qualname"):
                by_q.setdefault(k.co_qualname, []).append(k)
                stack.append(k)
    for f in iter_functions(module):
        q = f.__code__.co_qualname
        cands = by_q.get(q)
        if not cands:
            return False
        if len(cands) > 1:
            if all(c == f.__code__ for c in cands):
                continue
            return False
        if cands[0] == f.__code__:
            continue
        try:
            f.__code__ = cands[0]
        except ValueError:
            return False
    return True


def child_run(commit, edits, run_nums, reimport, tmp):
    """In a forked child: apply edits, run tests, return failing test numbers."""
    mapping = {}
    sources = {}
    for path, (new_src, is_py) in edits.items():
        absp = os.path.join(BASE, path)
        if new_src is not None:
            dst = os.path.join(tmp, path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8", newline="") as fh:
                fh.write(new_src)
            mapping[absp] = dst
        if is_py:
            sources[mod_name(path)] = (absp, new_src)
    redirect_open(mapping)
    sys.meta_path.insert(0, PatchedFinder(sources))

    if not reimport:
        for name, (absp, src) in sources.items():
            if src is None or not hot_swap(name, absp, src):
                reimport = True
                break
    if reimport:
        for k in list(sys.modules):
            if k in ("sqlglot", "tests") or k.startswith(("sqlglot.", "tests.")):
                del sys.modules[k]
        importlib.invalidate_caches()

    import unittest

    failing = set()
    groups = {}
    for n in run_nums:
        mod, cls, meth = test_parts(TESTS[n]["id"])
        groups.setdefault((mod, cls), []).append((meth, n))
    suite = unittest.TestSuite()
    id2num = {}
    for (mod, cls), items in groups.items():
        try:
            m = importlib.import_module(mod)
            klass = getattr(m, cls)
        except BaseException:
            log(commit, "import failed", mod, traceback.format_exc(limit=1).strip().splitlines()[-1])
            failing.update(n for _, n in items)
            continue
        for meth, n in items:
            try:
                tc = klass(meth)
            except BaseException:
                failing.add(n)
                continue
            id2num[tc.id()] = n
            suite.addTest(tc)

    class Result(unittest.TestResult):
        def startTest(self, test):
            super().startTest(test)
            n = id2num.get(test.id())
            limit = max(5.0, 20 * SECS.get(n, 1.0))
            signal.setitimer(signal.ITIMER_REAL, limit)

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                self._check_timeout(err)
                raise StopTest()  # one failing subtest fails the test: skip the rest

        def addError(self, test, err):
            super().addError(test, err)
            self._check_timeout(err)

        def _check_timeout(self, err):
            if err[0] is TestTimeout:
                self.timed_out = True  # likely a hang: stop and fail everything rather than burn CPU
                self.stop()

        def stopTest(self, test):
            signal.setitimer(signal.ITIMER_REAL, 0)
            super().stopTest(test)

    def on_alarm(*_):
        raise TestTimeout()

    signal.signal(signal.SIGALRM, on_alarm)
    res = Result()
    try:
        suite.run(res)
    except BaseException:
        log(commit, "suite crashed", traceback.format_exc(limit=2))
        return set(run_nums)
    if getattr(res, "timed_out", False):
        log(commit, "test timed out: failing all")
        return set(run_nums)

    def to_num(test):
        tc = getattr(test, "test_case", test)  # _SubTest -> its TestCase
        n = id2num.get(tc.id())
        if n is not None:
            return {n}
        # class/module fixture errors (_ErrorHolder): fail every test it covers
        desc = str(test)
        hit = {num for tid, num in id2num.items() if any(part and part in desc for part in tid.rsplit(".", 1)[:1])}
        return hit or set(run_nums)

    for test, _ in res.errors + res.failures:
        failing |= to_num(test)
    for test in res.unexpectedSuccesses:
        failing |= to_num(test)
    return failing


def run_in_child(commit, edits, run_nums, reimport, budget):
    tmp = os.path.join("/tmp", "br", commit)
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.setpgid(0, 0)  # own group: killing it also kills subprocesses
        os.close(r)
        try:
            failing = child_run(commit, edits, run_nums, reimport, tmp)
            data = json.dumps(sorted(failing)).encode()
        except BaseException:
            log(commit, "child error", traceback.format_exc(limit=3))
            data = json.dumps(sorted(run_nums)).encode()
        try:
            os.write(w, data) if len(data) < 60000 else None
            if len(data) >= 60000:
                with os.fdopen(w, "wb") as fh:
                    fh.write(data)
        finally:
            sys.stderr.flush()
            os._exit(0)
    os.close(w)
    chunks = []
    deadline = time.time() + min(budget, max(10.0, 3 * sum(SECS[n] for n in run_nums) + 5))
    import select

    while True:
        left = deadline - time.time()
        if left <= 0:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                os.kill(pid, signal.SIGKILL)
            log(commit, "child timed out")
            chunks = None
            break
        ready, _, _ = select.select([r], [], [], left)
        if ready:
            b = os.read(r, 1 << 16)
            if not b:
                break
            chunks.append(b)
    os.close(r)
    os.waitpid(pid, 0)
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    if chunks is None:
        return set(run_nums)
    try:
        return set(json.loads(b"".join(chunks)))
    except ValueError:
        return set(run_nums)


# ---------------------------------------------------------------- main


def warm_up():
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    sys.path.insert(0, BASE)
    os.chdir(BASE)
    sys.dont_write_bytecode = True
    install_inline_pool()
    mods = sorted({test_parts(t["id"])[0] for t in TESTS})
    for m in mods:
        try:
            importlib.import_module(m)
        except BaseException:
            log("warm import failed", m, traceback.format_exc(limit=1))
    try:
        import sqlglot.dialects as d

        for name in getattr(d, "DIALECTS", []):
            try:
                getattr(d, name)
            except Exception:
                pass
    except Exception:
        pass


def main():
    patches = sorted(os.listdir(os.path.join(DATA, "commits")))
    jobs = []
    for p in patches:
        if not p.endswith(".patch"):
            continue
        commit = p[: -len(".patch")]
        try:
            text = open(os.path.join(DATA, "commits", p), encoding="utf-8", errors="replace").read()
            files = parse_patch(text)
        except Exception:
            log(commit, "unparsable patch")
            jobs.append((commit, set(ALL), {}, True, ["weak-unparsable"], []))
            continue
        cand, edits, reimport, notes, groups = set(), {}, False, [], []
        if not files:
            notes.append("empty")
        for f in files:
            path = f["old"] or f["new"]
            base_src = read_base(f["old"]) if f["old"] else None
            try:
                c, ri, note = file_candidates(f, base_src, groups)
            except Exception:
                log(commit, "analysis error", path, traceback.format_exc(limit=2))
                c, ri, note = set(ALL), True, "weak-error"
            cand |= c
            reimport |= ri
            notes.append(f"{path}:{note}:{len(c)}")
            # edits the child applies
            try:
                if f["binary"]:
                    continue
                if f["old"] and f["new"] and f["old"] != f["new"]:
                    edits[f["old"]] = (None, f["old"].endswith(".py"))
                if f["new"] is None:
                    edits[f["old"]] = (None, f["old"].endswith(".py"))
                else:
                    src = "" if f["created"] or base_src is None else base_src
                    new_src, _ = apply_hunks(src, f["hunks"])
                    edits[f["new"]] = (new_src, f["new"].endswith(".py"))
            except Exception:
                cand |= ALL
                reimport = True
                notes.append("weak-edit")
        jobs.append((commit, cand, edits, reimport, notes, groups))

    warm_up()
    log("warm")
    out = []
    t_end = T0 + SECS_PER_COMMIT * len(jobs) + 5
    for i, (commit, cand, edits, reimport, notes, groups) in enumerate(jobs):
        cond = conditional_heavies(cand, groups, notes)
        run, blind = plan(cand - set(cond))
        sentinels = set().union(*cond.values()) if cond else set()
        run |= sentinels
        blind -= sentinels
        # leave 0.5 s for each later commit; out of time -> run nothing, select candidates blind
        budget = t_end - time.time() - 0.5 * (len(jobs) - i - 1)
        if run and budget < 1.0:
            failing = set(run)
            notes.append("out-of-time")
        else:
            failing = run_in_child(commit, edits, sorted(run), reimport, budget) if run else set()
        sel = (failing & run) | blind
        added = {h for h, sn in cond.items() if sn & failing}
        sel |= added
        cost, workers = pack(sel)
        log(commit, f"cand={len(cand)} run={len(run)} blind={len(blind)} cond={len(cond)}(+{len(added)}) fail={len(failing)} sel={len(sel)} cost={cost:.1f}", " ".join(notes))
        out.append(json.dumps({"commit": commit, "workers": workers}))
    sys.stdout.write("".join(l + "\n" for l in out))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
