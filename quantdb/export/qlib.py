"""Write a Qlib provider directory from quantdb's A-share tables, so the backtest engine keeps reading the
format it knows while quantdb is the only place data is fetched into.

    calendars/day.txt            trading days = the complete dates in cn.daily from ``start`` (a day still
                                 arriving is left out: ``through`` defaults to the last day marked done)
    instruments/all.txt          every name with its first and last trading day in the window
    instruments/csi300.txt …     index membership spans from cn.index_members (month ends → spans)
    features/<code>/<field>.day.bin   float32, first value = the index of the first day in the calendar
    features/sh000300/ …         index bars from cn.index_daily (benchmarks and hedges; not in any universe file)

Fields follow the community snapshot's conventions so factor code keeps its meaning: ``$open $high $low $close``
are adjusted prices (raw × factor), ``$factor`` = adj_factor / adj_factor on the name's first day in the window
(raw = adjusted / factor), ``$volume`` = 手 / factor (adjusted lots), ``$amount`` = yuan (unadjusted turnover).

The directory is rebuilt whole into a staging folder and swapped in; at ~5,500 names × 7 fields it takes well
under a minute, which is simpler and safer than appending to the bins.
"""
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

FIELDS = ("open", "high", "low", "close", "volume", "factor", "amount")
INDEXES = {"000300.SH": "csi300", "000905.SH": "csi500", "000852.SH": "csi1000"}


def complete_through(store, table="cn.daily"):
    """The last trading day the store considers finished (its key is marked done), so a day still arriving does
    not reach the provider as a short one."""
    done = store.meta(table).get("done") or []
    keys = [k for k in done if k.isdigit() and len(k) == 8]
    return pd.Timestamp(max(keys)) if keys else None


def export_qlib(store, out_dir, start="2015-01-01", through=None, report=lambda msg: None) -> dict:
    out_dir = Path(out_dir).expanduser()
    through = pd.Timestamp(through) if through is not None else complete_through(store)
    staging = out_dir.parent / f"{out_dir.name}.new"
    if staging.exists():
        shutil.rmtree(staging)
    for sub in ("calendars", "instruments", "features"):
        (staging / sub).mkdir(parents=True)

    report("reading cn.daily and cn.adj_factor")
    daily = store.read("cn.daily", start=start, end=through, columns=["open", "high", "low", "close", "vol", "amount"])
    adj = store.read("cn.adj_factor", start=start, end=through, columns=["adj_factor"])
    frame = daily.merge(adj, on=["date", "symbol"], how="left")
    frame = frame[frame["symbol"].str[:2].isin(["SH", "SZ"])].sort_values(["symbol", "date"])
    frame["adj_factor"] = frame.groupby("symbol")["adj_factor"].ffill()
    base = frame.groupby("symbol")["adj_factor"].transform("first")
    frame["factor"] = frame["adj_factor"] / base
    for column in ("open", "high", "low", "close"):
        frame[column] = frame[column] * frame["factor"]
    frame["volume"] = frame["vol"] / frame["factor"]
    frame["amount"] = frame["amount"] * 1000.0

    calendar = pd.DatetimeIndex(sorted(frame["date"].unique()))
    (staging / "calendars" / "day.txt").write_text("\n".join(d.strftime("%Y-%m-%d") for d in calendar) + "\n")
    position = pd.Series(np.arange(len(calendar)), index=calendar)

    report(f"writing {frame['symbol'].nunique()} names × {len(FIELDS)} fields over {len(calendar)} days")
    spans = []
    for symbol, rows in frame.groupby("symbol", sort=True):
        folder = staging / "features" / symbol.lower()
        folder.mkdir()
        first, last = position[rows["date"].iloc[0]], position[rows["date"].iloc[-1]]
        full = pd.DataFrame(index=calendar[first:last + 1]).join(rows.set_index("date")[list(FIELDS)])
        for field in FIELDS:
            values = np.concatenate([[np.float32(first)], full[field].to_numpy(dtype="<f4")])
            values.astype("<f4").tofile(folder / f"{field}.day.bin")
        spans.append(f"{symbol}\t{calendar[first].date()}\t{calendar[last].date()}")
    (staging / "instruments" / "all.txt").write_text("\n".join(spans) + "\n")

    # Indices (benchmarks, hedges): features only, never members of a stock universe. Unadjusted, factor 1.
    if store.table_path("cn.index_daily").is_file():
        index = store.read("cn.index_daily", start=calendar[0], end=calendar[-1], columns=["open", "high", "low", "close", "vol", "amount"])
        for symbol, rows in index.groupby("symbol", sort=True):
            rows = rows.set_index("date").reindex(calendar)
            rows = rows.loc[rows["close"].first_valid_index():]
            if rows.empty:
                continue
            folder = staging / "features" / symbol.lower()
            folder.mkdir(exist_ok=True)
            first = position[rows.index[0]]
            fields = {"open": rows["open"], "high": rows["high"], "low": rows["low"], "close": rows["close"],
                      "volume": rows["vol"], "factor": rows["close"] * 0 + 1.0, "amount": rows["amount"] * 1000.0}
            for field, values in fields.items():
                np.concatenate([[np.float32(first)], values.to_numpy(dtype="<f4")]).astype("<f4").tofile(folder / f"{field}.day.bin")
        report(f"wrote {index['symbol'].nunique()} indices")

    written = {"all": len(spans)}
    if store.table_path("cn.index_members").is_file():
        members = store.read("cn.index_members", columns=["con_code"])
        for code, name in INDEXES.items():
            rows = members[members["symbol"] == code]
            if rows.empty:
                continue
            lines = membership_spans(rows, calendar)
            (staging / "instruments" / f"{name}.txt").write_text("\n".join(lines) + "\n")
            written[name] = len(lines)
    report("swapping the directory")
    backup = out_dir.parent / f"{out_dir.name}.old"
    if backup.exists():
        shutil.rmtree(backup)
    if out_dir.exists():
        out_dir.rename(backup)
    staging.rename(out_dir)
    shutil.rmtree(backup, ignore_errors=True)
    return {"dir": str(out_dir), "days": len(calendar), "through": str(through.date()) if through is not None else None, "start": str(calendar[0].date()), "end": str(calendar[-1].date()), "instruments": written}


def membership_spans(rows, calendar):
    """Month-end membership lists → Qlib spans, point-in-time: the list published at month end d holds from the
    next trading day until the next list's date (the first list also covers its own date; the last one runs to
    the calendar's end). Consecutive spans of a name are merged."""
    dates = sorted(rows["date"].unique())
    lists = {d: set(rows.loc[rows["date"] == d, "con_code"]) for d in dates}
    spans = {}
    for i, d in enumerate(dates):
        after = calendar.searchsorted(d, side="right")
        span_start = calendar[calendar.searchsorted(d)] if i == 0 else calendar[min(after, len(calendar) - 1)]
        span_end = calendar[-1] if i == len(dates) - 1 else calendar[min(calendar.searchsorted(dates[i + 1]), len(calendar) - 1)]
        for code in lists[d]:
            current = spans.get(code)
            if current and current[-1][1] >= span_start - pd.Timedelta(days=45):
                current[-1][1] = span_end
            else:
                spans.setdefault(code, []).append([span_start, span_end])
    return [f"{code}\t{s.date()}\t{e.date()}" for code in sorted(spans) for s, e in spans[code]]
