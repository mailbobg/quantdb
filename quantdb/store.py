"""The store: one Parquet file per table under ``QUANTDB_HOME`` (default ``~/.quantdb``), read through DuckDB.

Layout::

    ~/.quantdb/
      tables/cn.moneyflow.parquet      the table, sorted by (date, symbol)
      meta/cn.moneyflow.json           source, key kind, coverage, keys done, last refresh
      snapshots/cn.moneyflow/<stamp>.parquet   the table as it was before each refresh (last SNAPSHOTS kept)
      .env                             secrets for the sources (never in a repository)

A table is a pandas frame with columns ``date`` (datetime64), ``symbol`` (str) and its fields; ``upsert``
replaces every stored row whose ``keys`` columns match a row of the new frame (by default the whole date), so
re-fetching a key is safe and a name may have several rows a day (block trades, unlock lots, restatements).
Keys already fetched are remembered in the meta record (``done``), which is what makes refreshes incremental.
"""
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

SNAPSHOTS = 5


def home() -> Path:
    return Path(os.environ.get("QUANTDB_HOME") or Path.home() / ".quantdb").expanduser()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else home()
        for sub in ("tables", "meta", "snapshots"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self._con = None

    # ---- paths ------------------------------------------------------------------------------------------------
    def table_path(self, name) -> Path:
        return self.root / "tables" / f"{name}.parquet"

    def meta_path(self, name) -> Path:
        return self.root / "meta" / f"{name}.json"

    # ---- meta -------------------------------------------------------------------------------------------------
    def meta(self, name) -> dict:
        path = self.meta_path(name)
        if not path.is_file():
            return {"table": name, "done": [], "rows": 0}
        return json.loads(path.read_text())

    def _write_meta(self, name, record):
        self.meta_path(name).write_text(json.dumps(record, ensure_ascii=False, indent=1, default=str))

    def tables(self):
        return sorted(p.stem for p in (self.root / "tables").glob("*.parquet"))

    # ---- write ------------------------------------------------------------------------------------------------
    def upsert(self, name, frame: pd.DataFrame, keys=("date",), done=None, source=None, note=None):
        """Merge ``frame`` into the table, replacing rows with the same ``keys``; record which fetch keys are done."""
        frame = _normalise(frame)
        path = self.table_path(name)
        if path.is_file():
            self._snapshot(name)
            old = pd.read_parquet(path)
            old, frame = _align(old, frame)
            if len(frame):
                keys = list(keys)
                mask = old[keys[0]].isin(frame[keys[0]]) if len(keys) == 1 else pd.MultiIndex.from_frame(old[keys]).isin(pd.MultiIndex.from_frame(frame[keys]))
                old = old[~mask]
            merged = pd.concat([old, frame], ignore_index=True)
        else:
            merged = frame
        merged = merged.sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)
        merged.to_parquet(path, index=False)
        record = self.meta(name)
        record.update({"rows": int(len(merged)), "columns": list(merged.columns), "updated": _now(),
                       "start": str(merged["date"].min().date()) if len(merged) else None,
                       "end": str(merged["date"].max().date()) if len(merged) else None,
                       "symbols": int(merged["symbol"].nunique()) if len(merged) else 0})
        if source:
            record["source"] = source
        if note:
            record["note"] = note
        if done:
            record["done"] = sorted(set(record.get("done", [])) | set(map(str, done)))
        self._write_meta(name, record)
        self._con = None
        return record

    def replace(self, name, frame: pd.DataFrame, source=None, note=None):
        """Overwrite the whole table (snapshot tables)."""
        frame = _normalise(frame)
        path = self.table_path(name)
        if path.is_file():
            self._snapshot(name)
        frame = frame.sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)
        frame.to_parquet(path, index=False)
        record = {"table": name, "done": [], "rows": int(len(frame)), "columns": list(frame.columns), "updated": _now(),
                  "start": str(frame["date"].min().date()) if len(frame) else None, "end": str(frame["date"].max().date()) if len(frame) else None,
                  "symbols": int(frame["symbol"].nunique()) if len(frame) else 0}
        if source:
            record["source"] = source
        if note:
            record["note"] = note
        self._write_meta(name, record)
        self._con = None
        return record

    def forget(self, name, keys):
        """Drop fetch keys from ``done`` so the next refresh fetches them again (rows stay until replaced)."""
        record = self.meta(name)
        record["done"] = sorted(set(record.get("done", [])) - set(map(str, keys)))
        self._write_meta(name, record)

    def _snapshot(self, name):
        folder = self.root / "snapshots" / name
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        shutil.copy2(self.table_path(name), folder / f"{stamp}.parquet")
        for old in sorted(folder.glob("*.parquet"))[:-SNAPSHOTS]:
            old.unlink()

    # ---- read -------------------------------------------------------------------------------------------------
    def connection(self):
        """A DuckDB connection with every table exposed as a view named like the table (dots become schemas)."""
        import duckdb

        if self._con is None:
            con = duckdb.connect()
            for name in self.tables():
                namespace, short = name.split(".", 1)
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{namespace}"')
                path = str(self.table_path(name)).replace("'", "''")
                con.execute(f'CREATE OR REPLACE VIEW "{namespace}"."{short}" AS SELECT * FROM read_parquet(\'{path}\')')
            self._con = con
        return self._con

    def sql(self, query: str) -> pd.DataFrame:
        return self.connection().execute(query).df()

    def read(self, name, start=None, end=None, symbols=None, columns=None, where=None) -> pd.DataFrame:
        """Rows of a table, optionally cut to a date range, a symbol list, some columns and a SQL predicate."""
        if not self.table_path(name).is_file():
            raise FileNotFoundError(f"table {name} has no data yet (quantdb refresh {name})")
        namespace, short = name.split(".", 1)
        cols = ", ".join(f'"{c}"' for c in ["date", "symbol", *[c for c in columns if c not in ("date", "symbol")]]) if columns else "*"
        clauses, params = [], []
        if start is not None:
            clauses.append("date >= ?"); params.append(pd.Timestamp(start).to_pydatetime())
        if end is not None:
            clauses.append("date <= ?"); params.append(pd.Timestamp(end).to_pydatetime())
        if symbols is not None:
            symbols = list(symbols)
            clauses.append("symbol IN (" + ", ".join("?" * len(symbols)) + ")"); params.extend(symbols)
        if where:
            clauses.append(f"({where})")
        sql = f'SELECT {cols} FROM "{namespace}"."{short}"' + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY date, symbol"
        return self.connection().execute(sql, params).df()

    def wide(self, name, field, start=None, end=None, symbols=None) -> pd.DataFrame:
        """One field as a (date × symbol) frame."""
        long = self.read(name, start, end, symbols, columns=[field])
        return long.pivot_table(index="date", columns="symbol", values=field, aggfunc="last")

    def status(self) -> pd.DataFrame:
        rows = []
        for name in self.tables():
            m = self.meta(name)
            rows.append({"table": name, "rows": m.get("rows"), "symbols": m.get("symbols"), "start": m.get("start"), "end": m.get("end"),
                         "keys_done": len(m.get("done", [])), "source": m.get("source"), "updated": m.get("updated")})
        return pd.DataFrame(rows)


def _align(old: pd.DataFrame, new: pd.DataFrame):
    """Make shared columns agree on dtype (a source that once gave ints may give strings next time)."""
    for c in new.columns.intersection(old.columns):
        if old[c].dtype == new[c].dtype or c in ("date", "symbol"):
            continue
        if pd.api.types.is_numeric_dtype(old[c]) and not pd.api.types.is_numeric_dtype(new[c]):
            coerced = pd.to_numeric(new[c], errors="coerce")
            if coerced.notna().sum() == new[c].notna().sum():
                new[c] = coerced
                continue
        if pd.api.types.is_numeric_dtype(new[c]) and not pd.api.types.is_numeric_dtype(old[c]):
            coerced = pd.to_numeric(old[c], errors="coerce")
            if coerced.notna().sum() == old[c].notna().sum():
                old[c] = coerced
                continue
        if not (pd.api.types.is_numeric_dtype(old[c]) and pd.api.types.is_numeric_dtype(new[c])):
            old[c] = old[c].astype("string"); new[c] = new[c].astype("string")
    return old, new


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    if "date" not in frame.columns or "symbol" not in frame.columns:
        raise ValueError("a quantdb table needs 'date' and 'symbol' columns")
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"]).astype("datetime64[ns]")
    out["symbol"] = out["symbol"].astype(str)
    return out.dropna(subset=["date", "symbol"])
