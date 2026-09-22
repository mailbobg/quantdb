"""Refresh: work out which keys a table is missing, fetch them from the first source that answers, upsert.

    refresh("cn.moneyflow")              # incremental: only keys not yet in meta["done"]
    refresh("cn.fina", start="2024-01-01")
    refresh("cn.baostock", symbols=[...])
    refresh_all(namespace="cn")

Key planning per key kind (see schema.Table):

    day       trading days of the calendar from ``start`` to today, minus ``done``
    period    quarter ends from ``start``; those whose season is still open (today - end < open_days) are
              fetched again
    week      Mondays from ``start`` to today (+ahead_days); the last ``open_weeks`` are fetched again
    symbol    every instrument of the table's universe; the key carries the resume date (``CODE@YYYY-MM-DD``)
    snapshot  one key, ``all``; the table is replaced

Progress is reported through ``report(event: dict)`` so a CLI or a server can relay it.
"""
from datetime import date, timedelta

import pandas as pd

from . import schema
from .sources import Config, make
from .store import Store

DEFAULT_START = "2015-01-01"


class Recorder:
    def __init__(self, store: Store | None = None, config: Config | None = None, report=None):
        self.store = store or Store()
        self.config = config or Config(self.store.root)
        self.report = report or (lambda event: None)
        self._sources = {}

    def source(self, name):
        if name not in self._sources:
            self._sources[name] = make(name, self.config)
        return self._sources[name]

    # ---- key planning -----------------------------------------------------------------------------------------
    def calendar(self, table: schema.Table):
        from .sources.qlib_bridge import calendar

        market = "us" if table.namespace == "us" else "cn"
        try:
            cal = calendar(self.config, market)
        except FileNotFoundError:
            return pd.bdate_range(DEFAULT_START, date.today())
        tail = pd.bdate_range(cal[-1] + pd.Timedelta(days=1), date.today())  # the provider lags a day or two
        return cal.append(tail) if len(tail) else cal

    def keys(self, table: schema.Table, start=None, end=None, symbols=None) -> list[str]:
        meta = self.store.meta(table.name)
        done = set(meta.get("done", []))
        start = pd.Timestamp(start or meta.get("plan_start") or DEFAULT_START)
        today = pd.Timestamp(end or date.today())
        if table.key == "day":
            days = [d for d in self.calendar(table) if start <= d <= today]
            return [d.strftime("%Y%m%d") for d in days if d.strftime("%Y%m%d") not in done]
        if table.key == "period":
            ends = pd.date_range(start - pd.offsets.QuarterEnd(1), today, freq="QE")  # the season open at ``start`` too
            out = []
            for e in ends:
                key = e.strftime("%Y%m%d")
                if key not in done or (today - e).days < table.open_days:
                    out.append(key)
            return out
        if table.key == "week":
            first = start - timedelta(days=start.weekday())
            last = today + timedelta(days=table.ahead_days)
            mondays = pd.date_range(first, last, freq="W-MON")
            recent = set(m.strftime("%Y%m%d") for m in mondays[-table.open_weeks:]) if table.open_weeks else set()
            return [m.strftime("%Y%m%d") for m in mondays if m.strftime("%Y%m%d") not in done or m.strftime("%Y%m%d") in recent]
        if table.key == "symbol":
            names = list(symbols) if symbols is not None else self.universe(table.universe)
            last = self._last_dates(table.name) if self.store.table_path(table.name).is_file() else {}
            out = []
            for s in names:
                since = last.get(s)
                out.append(f"{s}@{(since + timedelta(days=1)).date()}" if since is not None else s)
            return out
        return ["all"]

    def _last_dates(self, name):
        frame = self.store.sql(f'SELECT symbol, max(date) AS last FROM "{name.split(".")[0]}"."{name.split(".", 1)[1]}" GROUP BY symbol')
        return dict(zip(frame["symbol"], pd.to_datetime(frame["last"])))

    def universe(self, name: str) -> list[str]:
        """Instrument lists the symbol-keyed tables iterate over."""
        from .sources.qlib_bridge import members

        if name == "cn.all":
            m = members(self.config, "cn")
            return sorted(s for s in m["symbol"] if s[:2] in ("SH", "SZ"))
        if name == "us.all":
            return sorted(members(self.config, "us")["symbol"])
        if name == "cn.indices":
            return ["000300.SH", "000905.SH", "000852.SH", "000001.SH", "399001.SZ", "399006.SZ", "000016.SH", "000688.SH", "932000.CSI"]
        if name == "cb.all":
            if not self.store.table_path("cb.basic").is_file():
                self.refresh("cb.basic")
            return sorted(self.store.read("cb.basic")["symbol"].unique())
        if name == "fut.cffex_contracts":
            return self._cffex_contracts()
        raise KeyError(f"unknown universe {name!r}")

    def _cffex_contracts(self):
        raw = self.source("tushare")._query("fut_basic", {"exchange": "CFFEX"})
        codes = raw[raw["fut_code"].isin(["IF", "IH", "IC", "IM"])]["ts_code"]
        return sorted(c for c in codes if c[2:4].isdigit())

    # ---- refresh ----------------------------------------------------------------------------------------------
    def refresh(self, name: str, start=None, end=None, symbols=None, sources=None, limit=None) -> dict:
        table = schema.get(name)
        keys = self.keys(table, start, end, symbols)
        if limit:
            keys = keys[:limit]
        names = list(sources or table.sources)
        self.report({"table": name, "event": "plan", "keys": len(keys), "sources": names})
        if start and not self.store.meta(name).get("plan_start"):
            record = self.store.meta(name); record["plan_start"] = str(pd.Timestamp(start).date()); self.store._write_meta(name, record)
        done, failed, rows = [], [], 0
        batch, batch_keys = [], []
        for i, key in enumerate(keys, 1):
            frame, used, error = self._fetch(table, names, key)
            if error is not None:
                failed.append((key, str(error)[:200]))
                self.report({"table": name, "event": "fail", "key": key, "error": str(error)[:200]})
                continue
            if len(frame):
                batch.append(frame); rows += len(frame)
            if len(frame) or not self._recent(table, key):  # an empty answer for the last few days may just be unpublished yet
                batch_keys.append(key.split("@")[0] if table.key == "symbol" else key)
            self.report({"table": name, "event": "key", "key": key, "rows": len(frame), "source": used, "i": i, "n": len(keys)})
            if len(batch_keys) >= 50 or i == len(keys):
                self._commit(table, batch, batch_keys, used)
                done.extend(batch_keys); batch, batch_keys = [], []
        if batch_keys:
            self._commit(table, batch, batch_keys, None)
            done.extend(batch_keys)
        summary = {"table": name, "keys": len(keys), "done": len(done), "failed": failed, "rows": rows}
        self.report({"table": name, "event": "end", **summary})
        return summary

    @staticmethod
    def _recent(table, key, days=4):
        if table.key not in ("day", "week"):
            return False
        return (pd.Timestamp(date.today()) - pd.Timestamp(key)).days <= days

    def _fetch(self, table, names, key):
        last = None
        for src_name in names:
            try:
                src = self.source(src_name)
            except Exception as error:  # noqa: BLE001  (missing secret, missing package)
                last = error
                continue
            if table.name not in src.tables():
                continue
            try:
                return src.fetch(table.name, key), src_name, None
            except Exception as error:  # noqa: BLE001
                last = error
        return None, None, last or RuntimeError("no source serves this table")

    def _commit(self, table, frames, keys, source):
        if table.key == "snapshot":
            if frames:
                self.store.replace(table.name, pd.concat(frames, ignore_index=True), source=source)
            return
        frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["date", "symbol"])
        extra = [c for c in EXTRA_KEYS.get(table.name, ()) if c in frame.columns]
        self.store.upsert(table.name, frame, keys=("date", "symbol", *extra), done=keys, source=source)

    def refresh_all(self, namespace=None, **kw):
        out = []
        for name, table in schema.TABLES.items():
            if namespace and table.namespace != namespace:
                continue
            out.append(self.refresh(name, **kw))
        return out


# Tables where (date, symbol) is not unique: the extra columns that make a row identity.
EXTRA_KEYS = {
    "cn.unlock": ("holder_name",), "cn.block": ("price", "vol", "buyer", "seller"), "cn.toplist": ("reason",), "cn.holders": ("end_date",),
    "cn.forecast": ("ann_date", "type"), "cn.insider": ("changer", "change_shares"), "us.form4": ("accession", "trans_date", "trans_code", "shares", "price"),
    "us.ark_trades": ("fund", "direction"), "alt.appstore_top": (), "cb.premium": (), "fut.cffex": (),
}


def refresh(name, **kw):
    return Recorder().refresh(name, **kw)
