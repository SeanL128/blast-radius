# TODO

- Get v1 (8bdf369) board score: real CPU seconds and miss count. Compare with the local estimate (~30 s CPU, 137.5 s simulated).
- Class-attribute edits (e.g. sample 4d63, `Snowflake.Tokenizer.NUMERIC_ESCAPES`) select every instantiator of the class (273 tests here). Narrow to tests covering lines that read the attribute, and fall back when the attribute is only read at class creation (metaclass).
- Heavy test added when a sentinel fails (sample 535z added test 1224, `test_generator_memory`). Decide whether to run it instead of including it blind.
- Sentinel rule is a bet: a heavy test (>= 2 s) is skipped when cheap tests sharing every one of its reasons pass. Watch the miss count on the board; `HEAVY_T=1e9` turns it off.
- Map was built natively on macOS. Consider rebuilding it inside the linux/amd64 image (tools/covmap.py + tools/buildmap.py).
