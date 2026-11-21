# TODO

- Get v1 (8bdf369) board score: real CPU seconds and miss count. Compare with the local estimate (~30 s CPU, 137.5 s simulated).
- Class-attribute narrowing (4d63) probed 2026-11-21: not worth it. The only per-instance read of `NUMERIC_ESCAPES` is in `Tokenizer.__init__` (tokens.py:606), which covers all 273 instantiators. The real consumers sit behind a renamed kwarg (`numeric_escapes` in tokenizer_core.py) and would need dataflow tracking. Dropped unless Sean wants it.
- Possible gap (decision pending): nested-class attribute edits only select instantiators of the nested class. generator.py:3283 reads `dialect.tokenizer_class.NUMERIC_ESCAPES` in 34 Snowflake tests that never build a Snowflake.Tokenizer. Mutations (key removed, dict emptied) failed no tests, so no miss is shown yet. Adding the owner class's instantiators fixes it, at 9.1 -> 12.7 s on 4d63. Ground-truth harness: scratchpad truth.py (recreate if gone).
- Heavy test added when a sentinel fails (sample 535z added test 1224, `test_generator_memory`). Decide whether to run it instead of including it blind.
- Sentinel rule is a bet: a heavy test (>= 2 s) is skipped when cheap tests sharing every one of its reasons pass. Watch the miss count on the board; `HEAVY_T=1e9` turns it off.
- Map was built natively on macOS. Consider rebuilding it inside the linux/amd64 image (tools/covmap.py + tools/buildmap.py).
