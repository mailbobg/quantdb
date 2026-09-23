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
import threading
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
        self._trading_days = None

    def source(self, name):
        if name not in self._sources:
            self._sources[name] = make(name, self.config)
        return self._sources[name]

    # ---- key planning -----------------------------------------------------------------------------------------
    def calendar(self, table: schema.Table):
        """The trading days a day-keyed table is planned over: quantdb's own exchange calendar when it holds one,
        else the local Qlib provider, else plain business days. The store's calendar comes first so planning never
        depends on the provider quantdb itself exports."""
        market = "us" if table.namespace == "us" else "cn"
        if market == "cn":
            own = self.trading_days()
            if own is not None:
                return own
        from .sources.qlib_bridge import calendar

        try:
            cal = calendar(self.config, market)
        except FileNotFoundError:
            return pd.bdate_range(DEFAULT_START, date.today())
        tail = pd.bdate_range(cal[-1] + pd.Timedelta(days=1), date.today())  # the provider lags a day or two
        return cal.append(tail) if len(tail) else cal

    def trading_days(self):
        """A-share trading days from cn.trade_cal, or None when it has not been fetched."""
        if self._trading_days is None:
            if not self.store.table_path("cn.trade_cal").is_file():
                return None
            days = self.store.sql('SELECT DISTINCT date FROM "cn"."trade_cal" WHERE is_open = 1 ORDER BY date')
            self._trading_days = pd.DatetimeIndex(pd.to_datetime(days["date"]))
        return self._trading_days

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
    def refresh(self, name: str, start=None, end=None, symbols=None, sources=None, limit=None, workers=1) -> dict:
        """``workers`` > 1 fetches keys concurrently (each thread with its own source instances); commits stay
        in this thread, in key order."""
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
        typical = self._typical_rows(table) if table.key == "day" else None
        if workers > 1:
            from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

            pool = ThreadPoolExecutor(workers)
            local = threading.local()

            def fetch(key):
                if not hasattr(local, "sources"):
                    local.sources = {}
                return self._fetch(table, names, key, local.sources)

            results = pool.map(fetch, keys)
        else:
            results = (self._fetch(table, names, key) for key in keys)
        for i, (key, (frame, used, error)) in enumerate(zip(keys, results), 1):
            if error is not None:
                failed.append((key, str(error)[:200]))
                self.report({"table": name, "event": "fail", "key": key, "error": str(error)[:200]})
                continue
            if len(frame):
                batch.append(frame); rows += len(frame)
            if self._complete(table, key, frame, typical):  # recent days may be unpublished or half-published: keep the rows, fetch again next time
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

    def _complete(self, table, key, frame, typical):
        """Whether a fetched key can be marked done. Old keys always are; a key from the last few days is not
        when it came back empty or, for day tables, with far fewer rows than a normal day (a source still
        publishing yesterday), so the next run replaces it."""
        if not self._recent(table, key):
            return True
        if not len(frame):
            return False
        if typical and len(frame) < 0.9 * typical:
            self.report({"table": table.name, "event": "partial", "key": key, "rows": len(frame), "typical": typical})
            return False
        return True

    def _typical_rows(self, table):
        """Median rows per date over the last 20 stored dates, or None when the table is new."""
        if not self.store.table_path(table.name).is_file():
            return None
        ns, short = table.name.split(".", 1)
        counts = self.store.sql(f'SELECT count(*) AS n FROM "{ns}"."{short}" GROUP BY date ORDER BY date DESC LIMIT 20')
        return float(counts["n"].median()) if len(counts) >= 5 else None

    def _fetch(self, table, names, key, sources=None):
        last = None
        for src_name in names:
            try:
                src = self.source(src_name) if sources is None else sources.setdefault(src_name, make(src_name, self.config))
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
        frame, thin = self._keep_fuller_days(table, frame)
        self.store.upsert(table.name, frame, keys=replace_keys(table), done=[k for k in keys if k not in thin], source=source)

    @staticmethod
    def _key_of(table, day):
        """The fetch key a stored date belongs to (a week table's key is its Monday)."""
        day = pd.Timestamp(day)
        if table.key == "week":
            day -= timedelta(days=day.weekday())
        return day.strftime("%Y%m%d")

    def _keep_fuller_days(self, table, frame):
        """Drop the dates where the fetch came back with fewer rows than the store already holds. A day, period or
        week fetch replaces its whole date, so a truncated or half-published answer would otherwise destroy a
        fuller stored day; the key stays unfinished either way, so the next run tries again."""
        if table.key == "symbol" or frame.empty or not self.store.table_path(table.name).is_file():
            return frame, set()
        ns, short = table.name.split(".", 1)
        stored = self.store.sql(f'SELECT date, count(*) AS n FROM "{ns}"."{short}" GROUP BY date')
        if stored.empty:
            return frame, set()
        have = dict(zip(pd.to_datetime(stored["date"]), stored["n"]))
        fetched = frame.groupby("date").size()
        thin = [day for day, n in fetched.items() if have.get(pd.Timestamp(day), 0) > n]
        if not thin:
            return frame, set()
        for day in thin:
            self.report({"table": table.name, "event": "thin", "key": pd.Timestamp(day).strftime("%Y%m%d"),
                         "rows": int(fetched[day]), "stored": int(have[pd.Timestamp(day)])})
        return frame[~frame["date"].isin(thin)], {self._key_of(table, day) for day in thin}

    def refresh_all(self, namespace=None, **kw):
        out = []
        for name, table in schema.TABLES.items():
            if namespace and table.namespace != namespace:
                continue
            out.append(self.refresh(name, **kw))
        return out


def replace_keys(table):
    """What a fetch replaces. Day, period and week fetches return every row of their dates, so the stored rows of
    those dates are replaced wholesale (a name can have several rows a day: block trades, unlock lots,
    restatements). Symbol fetches extend one instrument, so they replace by (date, symbol)."""
    return ("date", "symbol") if table.key == "symbol" else ("date",)


def refresh(name, **kw):
    return Recorder().refresh(name, **kw)
