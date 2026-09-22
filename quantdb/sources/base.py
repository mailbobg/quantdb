"""What a data source is: something that fetches one key of one table and returns a standard long frame.

    class Source:
        name: str
        def tables(self) -> list[str]                       # the tables this source can serve
        def fetch(self, table: str, key: str) -> DataFrame  # one chunk: columns date, symbol, fields…

Keys are strings: ``20250102`` for a trading day, ``20250630`` for a report period, ``20250106`` for the Monday
of a week, an instrument code for symbol tables, ``all`` for snapshot tables. A source that cannot serve a key
returns an empty frame; one that fails raises, and the recorder tries the next source or records the failure.

Secrets come from ``Config`` (``~/.quantdb/.env`` and the process environment), never from the source itself.
"""
import os
import time
from pathlib import Path

import pandas as pd

EMPTY = pd.DataFrame(columns=["date", "symbol"])


class Config:
    """Key-value secrets and settings from QUANTDB_HOME/.env plus the environment (environment wins)."""

    def __init__(self, root: Path | None = None):
        from ..store import home

        self.values = {}
        env_file = (Path(root) if root else home()) / ".env"
        if env_file.is_file():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    self.values[k.strip()] = v.strip().strip("'\"")
        self.values.update({k: v for k, v in os.environ.items()})

    def get(self, key, default=None):
        return self.values.get(key, default)

    def require(self, *keys):
        missing = [k for k in keys if not self.values.get(k)]
        if missing:
            raise RuntimeError(f"missing settings {', '.join(missing)} (put them in {home_env()})")
        return [self.values[k] for k in keys]


def home_env():
    from ..store import home

    return home() / ".env"


class Source:
    name = "base"

    def __init__(self, config: Config):
        self.config = config

    def tables(self):
        return []

    def fetch(self, table: str, key: str) -> pd.DataFrame:
        raise NotImplementedError


def retry(fn, attempts=4, backoff=(5, 15, 45), on=(Exception,)):
    """Call ``fn`` until it returns; sleeps between attempts; re-raises the last error."""
    for i in range(attempts):
        try:
            return fn()
        except on as error:  # noqa: PERF203
            if i == attempts - 1:
                raise error
            time.sleep(backoff[min(i, len(backoff) - 1)])


def qlib_code(ts_code):
    """Tushare 000001.SZ → Qlib SZ000001; Beijing and non-A codes → None."""
    if not isinstance(ts_code, str) or "." not in ts_code:
        return None
    number, exchange = ts_code.split(".", 1)
    return f"{exchange}{number}" if exchange in ("SH", "SZ") else None


def ts_code(qlib):
    """Qlib SZ000001 → Tushare 000001.SZ."""
    return f"{qlib[2:]}.{qlib[:2]}"


def compact(day) -> str:
    """'2025-01-02' / Timestamp → '20250102'."""
    return str(pd.Timestamp(day).date()).replace("-", "")
