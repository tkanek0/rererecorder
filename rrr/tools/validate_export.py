"""Check a neutral export without importing or opening the recording archive.

uv run python -m rrr.tools.validate_export export/<session>
"""

from __future__ import annotations

import argparse

from rrr.tools.export import validate_export


def main(argv: list[str] | None = None) -> int:
    """Validate one exported session and return a shell-friendly status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", help="exported session directory")
    args = parser.parse_args(argv)

    problems = validate_export(args.directory)
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
