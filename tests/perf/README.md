# Performance and data-loss scripts

Reusable versions of the ad hoc checks behind decisions 21, 22 and 23 - not
automated tests. Each is a standalone script with its own `--help`, meant to
be rerun by hand: after a driver update, on a different machine, or when a
future change to `FrameHub`/`VideoWriter` needs the same question asked again.

**None of these run under `make check` or plain `pytest tests/`.** `pytest`
only auto-collects `test_*.py`, and nothing here is named that on purpose -
`pyproject.toml`'s own test configuration says why: "the camera admits one
process at a time... a suite that needed [a device] could not run beside the
server." Two of these three need the device attached and nothing else using
it; the third needs neither, but still takes real wall-clock time to mean
anything.

| Script | Needs a device? | What it reproduces |
|---|---|---|
| `soak_record.py` | Yes | "Does this combination of streams and codecs hold close to 30 fps with nothing dropped, for as long as it runs?" - decisions 21/22/23's core question, for any combination `rrr.tools.record` accepts. |
| `sqlite_write_benchmark.py` | No | Decision 22's SQLite/WAL insert-throughput finding: compressed-size blobs (~600 KB) insert well inside budget, raw-size blobs (~1.8 MB) do not - independent of any encoding cost. |
| `frame_number_gaps.py` | Yes | Decision 21's premise check: the SDK's own per-stream `frame_number` never skips, on this hardware, in either auto-exposure state - so the earlier discard policy was throwing away good frames, not protecting against lost ones. |

## Usage

```bash
# the combination decision 23 settled on for this Windows machine
uv run python tests/perf/soak_record.py --session soak-color-raw \
    --seconds 600 --no-depth --no-infrared --color-codec raw

# the full six-image set, compressed - decision 22's worst case
uv run python tests/perf/soak_record.py --session soak-full-compressed \
    --seconds 60

# no camera needed
uv run python tests/perf/sqlite_write_benchmark.py

# needs the camera and nothing else holding it
uv run python tests/perf/frame_number_gaps.py --seconds 60
uv run python tests/perf/frame_number_gaps.py --seconds 60 --auto-exposure off
```

Every one exits non-zero on failure, so a shell script chaining several of
these together (or a future device-equipped CI runner) can trust the exit
code rather than parsing the printed numbers.
