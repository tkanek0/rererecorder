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

Checks to rerun by hand after a driver update, on a different machine, or when
`FrameHub`/`VideoWriter` changes. None is named `test_*.py`, so `pytest` never
collects them: the camera admits one process at a time (`docs/design.md`,
Module boundaries). Each exits non-zero on failure.

| Command | Needs a device? | What it reproduces |
|---|---|---|
| `record.py --min-fps <fps>` | Yes | Whether a stream/codec combination holds its rate with nothing dropped (decisions 21-23): exit status 2 on any drop or a lower rate. |
| `sqlite_write_benchmark.py` | No | Decision 22's SQLite/WAL insert throughput: compressed-size blobs (~600 KB) insert well inside budget, raw-size blobs (~1.8 MB) do not. |

```bash
# color alone, raw - the combination decision 23 settled on for Windows
uv run python scripts/record.py --seconds 600 --no-depth --no-infrared \
    --color-codec raw --min-fps 29.5

# the full six-image set, compressed - decision 22's worst case
uv run python scripts/record.py --seconds 60 --min-fps 29.5

# no camera needed
uv run python scripts/sqlite_write_benchmark.py
```
