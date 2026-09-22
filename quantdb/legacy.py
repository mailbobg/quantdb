"""Load caches other programs already fetched (CSV / parquet / pickle) into a table, so nothing is fetched twice.

Mapping of the existing RD-Agent Studio caches to tables lives in ``import_studio``; anything else goes through
``import_files`` with explicit column names.
"""
from pathlib import Path

import pandas as pd

from . import schema
from .recorders import replace_keys
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
    if done == "from-names":  # one file per fetch key, named after it (a prefix like daily_ is dropped)
        keys = [stem.rsplit("_", 1)[-1] for stem in stems]
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
    record = store.upsert(table, frame, keys=replace_keys(t), done=keys, source=source, note="imported")
    if keys and t.key in ("day", "period", "week") and not record.get("plan_start"):
        record["plan_start"] = str(frame["date"].min().date())  # refreshes continue from where the cache began
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


# ---- other caches: raw provider files already on disk -------------------------------------------------------
def import_tushare_csvs(store: Store, table: str, files, date_col="trade_date", source="tushare", report=print):
    """Tushare CSVs for non-A-share tables (cb.*, fut.*): ``symbol`` is the ts_code itself."""
    def tf(raw, path):
        return raw.rename(columns={"ts_code": "symbol"})

    keys = "from-names" if schema.get(table).key in ("day", "period", "week") else None
    record = import_files(store, table, files, date_col=date_col, source=source, done=keys, transform=tf)
    report(f"{table}: {record['rows']} rows {record['start']}..{record['end']}")
    return record


def import_form4_quarters(store: Store, root, report=print):
    """A folder of unpacked SEC insider data sets, one sub-folder per quarter (2017q1/SUBMISSION.tsv …)."""
    from .sources.sec import parse_form4

    root = Path(root).expanduser()
    frames = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and (p / "SUBMISSION.tsv").is_file()):
        frames.append(parse_form4(lambda name, f=folder: open(f / name, "rb"), folder.name))
    frame = pd.concat(frames, ignore_index=True).dropna(subset=["date", "symbol"])
    record = store.replace("us.form4", frame, source="sec", note=f"imported {len(frames)} quarters")
    report(f"us.form4: {record['rows']} rows {record['start']}..{record['end']}")
    return record


def import_eps_concepts(store: Store, root, report=print):
    """A folder of SEC companyconcept JSON files named <SYMBOL>.json."""
    import json

    from .sources.sec import parse_eps_concept

    root = Path(root).expanduser()
    frames, done = [], []
    for path in sorted(root.glob("*.json")):
        try:
            body = json.loads(path.read_text())
        except ValueError:
            continue
        if not isinstance(body, dict) or "units" not in body:
            continue
        frame = parse_eps_concept(body, path.stem)
        done.append(path.stem.upper())
        if len(frame):
            frames.append(frame)
    frame = pd.concat(frames, ignore_index=True)
    record = store.upsert("us.eps_xbrl", frame, keys=("date", "symbol"), done=done, source="sec", note="imported")
    report(f"us.eps_xbrl: {record['rows']} rows, {len(done)} symbols")
    return record


def import_ark_json(store: Store, files, report=print):
    """arkfunds.io trade responses saved as JSON ({"trades": [...]})."""
    import json

    from .sources.arkfunds import parse_trades

    raw = pd.concat([pd.DataFrame(json.loads(Path(f).read_text())["trades"]) for f in files], ignore_index=True)
    record = store.replace("us.ark_trades", parse_trades(raw).drop_duplicates(), source="arkfunds", note="imported")
    report(f"us.ark_trades: {record['rows']} rows {record['start']}..{record['end']}")
    return record


def import_eastmoney_csvs(store: Store, root, report=print):
    """akshare bond_zh_cov_value_analysis frames saved as <code>.csv (Chinese headers)."""
    from .sources.eastmoney import parse_value_analysis

    root = Path(root).expanduser()
    frames, done = [], []
    codes = {}
    if store.table_path("cb.basic").is_file():
        basic = store.read("cb.basic")
        codes = {sym.split(".")[0]: sym for sym in basic["symbol"]}
    for path in sorted(root.glob("*.csv")):
        raw = pd.read_csv(path)
        symbol = codes.get(path.stem, path.stem + (".SH" if path.stem.startswith(("11", "13")) else ".SZ"))
        frame = parse_value_analysis(raw, symbol)
        done.append(symbol)
        if len(frame):
            frames.append(frame)
    frame = pd.concat(frames, ignore_index=True)
    record = store.upsert("cb.premium", frame, keys=("date", "symbol"), done=done, source="eastmoney", note="imported")
    report(f"cb.premium: {record['rows']} rows, {len(done)} bonds")
    return record


def import_alphavantage_json(store: Store, root, report=print):
    """Alpha Vantage EARNINGS responses saved as <SYMBOL>.json."""
    import json

    from .sources.alphavantage import parse_earnings

    root = Path(root).expanduser()
    frames, done = [], []
    for path in sorted(root.glob("*.json")):
        body = json.loads(path.read_text())
        if "quarterlyEarnings" not in body:
            continue
        frame = parse_earnings(body, path.stem)
        done.append(path.stem.upper())
        if len(frame):
            frames.append(frame)
    frame = pd.concat(frames, ignore_index=True)
    record = store.upsert("us.earnings_av", frame, keys=("date", "symbol"), done=done, source="alphavantage", note="imported")
    report(f"us.earnings_av: {record['rows']} rows, {len(done)} symbols")
    return record


# ---- first load of the price tables --------------------------------------------------------------------------
def backfill_prices_by_symbol(store: Store, start="2015-01-01", symbols=None, report=print, workers=4):
    """Fill cn.daily and cn.adj_factor from ``start`` one instrument at a time through the Tushare REST server
    (whole-market days exceed its row cap, one name's history does not), then mark every trading day done so the
    daily refresh continues from there by trading day."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    from .recorders import Recorder
    from .sources.base import compact, ts_code
    from .sources.tushare_reseller import TushareReseller

    rec = Recorder(store)
    if symbols is not None:
        names = list(symbols)
    else:
        names = rec.universe("cn.all")
        if store.table_path("cn.stock_basic").is_file():  # names the Qlib list lacks (recent IPOs, delisted)
            names = sorted(set(names) | set(store.read("cn.stock_basic")["symbol"]))
    src = TushareReseller(rec.config)
    end = compact(pd.Timestamp.today())
    have = set()
    if store.table_path("cn.daily").is_file():
        have = set(store.sql('SELECT DISTINCT symbol FROM "cn"."daily"')["symbol"])
    todo = [s for s in names if s not in have]
    report(f"{len(todo)} names to fetch ({len(have)} already there)")

    def one(symbol):
        for attempt in range(4):
            try:
                d = src.main.query("daily", {"ts_code": ts_code(symbol), "start_date": compact(start), "end_date": end})
                a = src.main.query("adj_factor", {"ts_code": ts_code(symbol), "start_date": compact(start), "end_date": end})
                return symbol, d, a, None
            except Exception as error:  # noqa: BLE001
                time.sleep(3 * (attempt + 1)); last = error
        return symbol, None, None, last

    def shape(raw, symbol, drop):
        out = raw.copy()
        out["date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d"); out["symbol"] = symbol
        return out.drop(columns=[c for c in drop if c in out.columns])

    failed, batch_d, batch_a, n = [], [], [], 0
    with ThreadPoolExecutor(workers) as pool:
        for i, (symbol, d, a, error) in enumerate(pool.map(one, todo), 1):
            if error is not None:
                failed.append((symbol, str(error)[:120]))
            else:
                if d is not None and len(d):
                    batch_d.append(shape(d, symbol, ("ts_code",)))
                if a is not None and len(a):
                    batch_a.append(shape(a, symbol, ("ts_code",)))
            if i % 200 == 0 or i == len(todo):
                if batch_d:
                    store.upsert("cn.daily", pd.concat(batch_d, ignore_index=True), keys=("date", "symbol"), source="tushare")
                if batch_a:
                    store.upsert("cn.adj_factor", pd.concat(batch_a, ignore_index=True), keys=("date", "symbol"), source="tushare")
                n += len(batch_d); batch_d, batch_a = [], []
                report(f"  {i}/{len(todo)} names, {len(failed)} failed")
    # every trading day up to yesterday counts as fetched; today follows through the normal refresh
    for table in ("cn.daily", "cn.adj_factor"):
        if store.table_path(table).is_file():
            days = store.sql(f'SELECT DISTINCT date FROM "cn"."{table.split(".")[1]}" WHERE date < current_date')["date"]
            record = store.meta(table)
            record["done"] = sorted(set(record.get("done", [])) | {pd.Timestamp(d).strftime("%Y%m%d") for d in days})
            record["plan_start"] = str(pd.Timestamp(start).date())
            store._write_meta(table, record)
    return {"names": len(todo), "failed": failed}
