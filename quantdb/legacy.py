"""Load caches other programs already fetched (CSV / parquet / pickle) into a table, so nothing is fetched twice.

Mapping of the existing RD-Agent Studio caches to tables lives in ``import_studio``; anything else goes through
``import_files`` with explicit column names.
"""
from pathlib import Path

import pandas as pd

from . import schema
from .recorders import EXTRA_KEYS
from .sources.base import qlib_code
from .store import Store


def _read(path: Path) -> pd.DataFrame:
    if path.suffix == ".csv":
        return pd.read_csv(path, dtype={"ts_code": str, "symbol": str, "code": str}, low_memory=False)
    if path.suffix in (".parquet", ".pq"):
        return pd.read_parquet(path)
    if path.suffix in (".pkl", ".pickle"):
        return pd.read_pickle(path)
    raise ValueError(f"cannot read {path}")


def import_files(store: Store, table: str, paths, date_col="date", symbol_col="symbol", source="legacy", done=None, transform=None):
    t = schema.get(table)
    frames, stems = [], []
    for p in paths:
        p = Path(p).expanduser()
        files = sorted(p.glob("*.csv")) + sorted(p.glob("*.parquet")) if p.is_dir() else [p]
        for f in files:
            raw = _read(f)
            if transform:
                raw = transform(raw, f)
            if len(raw):
                frames.append(raw)
            stems.append(f.stem)
    if not frames:
        raise FileNotFoundError(f"nothing to import from {paths}")
    frame = pd.concat(frames, ignore_index=True)
    if date_col != "date":
        frame["date"] = frame[date_col]
    if symbol_col != "symbol":
        frame["symbol"] = frame[symbol_col]
    frame["date"] = pd.to_datetime(frame["date"].astype(str).str.replace("-", ""), format="%Y%m%d", errors="coerce")
    frame = frame.dropna(subset=["date", "symbol"])
    keys = None
    if done == "from-names":  # one file per fetch key, named after it
        keys = stems
    elif done == "from-dates":
        if t.key == "day":
            keys = sorted(frame["date"].dt.strftime("%Y%m%d").unique())
        elif t.key == "period":
            keys = sorted(frame["date"].dt.strftime("%Y%m%d").unique())
        elif t.key == "week":
            keys = sorted((frame["date"] - pd.to_timedelta(frame["date"].dt.weekday, unit="D")).dt.strftime("%Y%m%d").unique())
        elif t.key == "symbol":
            keys = sorted(frame["symbol"].unique())
    elif done:
        keys = done.split(",")
    if t.key == "snapshot":
        return store.replace(table, frame, source=source, note="imported")
    extra = [c for c in EXTRA_KEYS.get(table, ()) if c in frame.columns]
    record = store.upsert(table, frame.drop_duplicates(["date", "symbol", *extra]), keys=("date", "symbol", *extra), done=keys, source=source, note="imported")
    if keys and t.key in ("day", "period", "week") and not record.get("plan_start"):
        record["plan_start"] = str(pd.Timestamp(min(keys)).date())  # refreshes continue from where the cache began
        store._write_meta(table, record)
    return record


# ---- RD-Agent Studio caches ----------------------------------------------------------------------------------
STUDIO_TUSHARE = {  # cache folder name -> table
    "moneyflow": "cn.moneyflow", "margin": "cn.margin", "chips": "cn.chips", "basic": "cn.basic", "toplist": "cn.toplist", "block": "cn.block",
    "fina": "cn.fina", "forecast": "cn.forecast", "express": "cn.express", "holders": "cn.holders", "unlock": "cn.unlock",
}
DATE_COLS = {"cn.fina": "end_date", "cn.forecast": "end_date", "cn.express": "end_date", "cn.holders": "ann_date", "cn.unlock": "float_date"}


def _tushare_frame(raw, path):
    out = raw.copy()
    if "ts_code" in out.columns:
        out["symbol"] = out["ts_code"].map(qlib_code)
        out = out.drop(columns=["ts_code"])
    return out


def import_studio_tushare(store: Store, root, report=print):
    """``root`` is Studio's extra/tushare cache folder: one sub-folder per table, one CSV per key."""
    root = Path(root).expanduser()
    out = {}
    for folder, table in STUDIO_TUSHARE.items():
        src = root / folder
        if not src.is_dir() or not any(src.iterdir()):
            continue
        record = import_files(store, table, [src], date_col=DATE_COLS.get(table, "trade_date"), source="tushare", done="from-names", transform=_tushare_frame)
        report(f"{table}: {record['rows']} rows {record['start']}..{record['end']}")
        out[table] = record
    return out


def import_studio_baostock(store: Store, root, report=print):
    """Studio's baostock cache: one CSV per instrument named like SZ000001.csv."""
    root = Path(root).expanduser()

    def tf(raw, path):
        out = raw.copy()
        if "symbol" not in out.columns:
            out["symbol"] = path.stem
        if "code" in out.columns:
            out = out.drop(columns=["code"])
        return out

    files = [f for f in sorted(root.glob("*.csv")) if f.stem[:2] in ("SH", "SZ")]
    record = import_files(store, "cn.baostock", files, source="baostock", done="from-names", transform=tf)
    report(f"cn.baostock: {record['rows']} rows {record['start']}..{record['end']}")
    return record
