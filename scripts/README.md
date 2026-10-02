# Scripts

Local tools that work on recordings. They are not part of the `rrr` package and
nothing imports them except their tests: the package provides the means -
reading a session on one clock (`rrr.playback`), cross-checks
(`rrr.inspection`), the offset measurement (`rrr.offset`), drawing parts
(`rrr.visualization`) - and each script decides what to do with them and what to
write out. See `docs/decisions.md` 31.

Run them from the repository root, against the installed package:

```bash
uv run python scripts/<name>.py --help
```

A script may import a sibling by its file name (`export.py` imports
`validate_export`), since Python puts the script's own directory first on the
path. Tests do the same through pytest's `pythonpath = ["scripts"]`. That is
also why no script may be named after a standard library module:
`inspect_session.py`, not `inspect.py`.

## Recording, and working on a recording

| Script | What it does |
|---|---|
| `record.py` | Records a session from the terminal, through the same recorder the server uses. |
| `inspect_session.py` | Re-reads a session and makes its files argue with each other. |
| `calibrate.py` | Measures the audio-to-video offset from handclaps; `--apply` stores it. |
| `export.py` | Writes a session as the neutral layout that consumers outside this repository read. |
| `validate_export.py` | Checks an export. Imports nothing from `rrr`, so it can be copied to a consumer. |
| `render_mp4.py` | A review movie with sound and the array's direction. |
| `render_gif.py` | A short GIF, optionally with loudness and waveform strips. |

`export.py` and `validate_export.py` define the export format together; the
`rrr` package knows nothing of its layout.

## Performance and data loss

Reusable versions of the ad hoc checks behind decisions 21, 22 and 23 - not
automated tests. Each is meant to be rerun by hand: after a driver update, on a
different machine, or when a future change to `FrameHub`/`VideoWriter` needs the
same question asked again. None is named `test_*.py`, so `pytest` never collects
them: the camera admits one process at a time, and a suite that needed it could
not run beside the server (`docs/design.md`, Module boundaries).

| Script | Needs a device? | What it reproduces |
|---|---|---|
| `soak_record.py` | Yes | "Does this combination of streams and codecs hold close to 30 fps with nothing dropped, for as long as it runs?" - decisions 21/22/23's core question, for any combination `record.py` accepts. |
| `sqlite_write_benchmark.py` | No | Decision 22's SQLite/WAL insert-throughput finding: compressed-size blobs (~600 KB) insert well inside budget, raw-size blobs (~1.8 MB) do not - independent of any encoding cost. |
| `frame_number_gaps.py` | Yes | Decision 21's premise check: the SDK's own per-stream `frame_number` never skips, on this hardware, in either auto-exposure state - so the earlier discard policy was throwing away good frames, not protecting against lost ones. |

```bash
# the combination decision 23 settled on for this Windows machine
uv run python scripts/soak_record.py --session soak-color-raw \
    --seconds 600 --no-depth --no-infrared --color-codec raw

# the full six-image set, compressed - decision 22's worst case
uv run python scripts/soak_record.py --session soak-full-compressed \
    --seconds 60

# no camera needed
uv run python scripts/sqlite_write_benchmark.py

# needs the camera and nothing else holding it
uv run python scripts/frame_number_gaps.py --seconds 60
uv run python scripts/frame_number_gaps.py --seconds 60 --auto-exposure off
```

Every one exits non-zero on failure, so a shell script chaining several of
these together (or a future device-equipped CI runner) can trust the exit code
rather than parsing the printed numbers.
