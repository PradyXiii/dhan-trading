# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
atomic_io.py — atomic-write helpers for state files.

Every state file in this system (CSVs, JSONs, patched .py files) must use
these helpers. The previous truncate+rewrite pattern silently corrupts state
on crash mid-write or under concurrent writers — this module wraps each
write in tmpfile + fsync + os.replace, which is atomic on POSIX.
"""
import csv
import json
import os
import tempfile
from typing import Iterable


def write_atomic_text(path: str, text: str) -> None:
    """Write text to `path` atomically. Crash-safe on POSIX."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_atomic_json(path: str, obj, indent: int = 2) -> None:
    """JSON dump atomically."""
    write_atomic_text(path, json.dumps(obj, indent=indent, default=str))


def write_atomic_csv(path: str, fieldnames: list, rows: Iterable[dict]) -> None:
    """Write list-of-dicts as CSV atomically."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=d, suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_atomic_dataframe(path: str, df, **to_csv_kwargs) -> None:
    """pandas DataFrame.to_csv() atomically. Wraps any to_csv kwargs."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=d, suffix=".csv")
    os.close(fd)
    try:
        df.to_csv(tmp, **to_csv_kwargs)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
