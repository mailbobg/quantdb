"""Read-only bridge to the local Qlib providers: the instrument master (meta.instruments) and trading calendars.
Prices stay in Qlib; nothing is copied. Provider paths: QLIB_CN (default ~/.qlib/qlib_data/cn_data) and
QLIB_US (default ~/.qlib/qlib_data/us_ndx)."""
from pathlib import Path

import pandas as pd

from .base import EMPTY, Source


def provider(config, market):
    default = {"cn": "~/.qlib/qlib_data/cn_data", "us": "~/.qlib/qlib_data/us_ndx"}[market]
    return Path(config.get(f"QLIB_{market.upper()}", default)).expanduser()


def calendar(config, market="cn"):
    path = provider(config, market) / "calendars" / "day.txt"
    return pd.DatetimeIndex(sorted(pd.Timestamp(l.strip()) for l in path.read_text().splitlines() if l.strip()))


def members(config, market, universe="all"):
    """Every instrument in a Qlib universe file with its listing spans."""
    path = provider(config, market) / "instruments" / f"{universe}.txt"
    rows = []
    for line in path.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows.append({"symbol": parts[0].strip(), "start": pd.Timestamp(parts[1]), "end": pd.Timestamp(parts[2])})
    return pd.DataFrame(rows)


class QlibBridge(Source):
    name = "qlib"

    def tables(self):
        return ["meta.instruments"]

    def fetch(self, table, key):
        frames = []
        for market in ("cn", "us"):
            try:
                m = members(self.config, market)
            except FileNotFoundError:
                continue
            m["market"] = market; m["date"] = m["start"]
            frames.append(m)
        if not frames:
            return EMPTY.copy()
        return pd.concat(frames, ignore_index=True)
