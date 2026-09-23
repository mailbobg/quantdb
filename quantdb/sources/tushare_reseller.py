"""Tushare Pro data through two reseller servers (settings TUSHARE_MIRROR_TOKEN / TUSHARE_MIRROR_URL for the
mirror that speaks the tushare protocol, DATAHUB_API_KEY / DATAHUB_BASE for the REST front with paging).

The mirror answers at most 6000 rows and cannot page; the main server pages 5000 at a time. A call the mirror
truncates is retried on the main server. Both throttle, so calls are paced per server. Row-heavy tables
(share_float: one row per holder) go straight to the paging server.

Serves: cn.moneyflow cn.margin cn.chips cn.basic cn.toplist cn.block (by trade day); cn.fina cn.forecast
cn.express (by report period, *_vip bulk interfaces); cn.holders cn.unlock (by week); cn.index_daily (by index
code); cb.basic (snapshot) cb.daily (by day); fut.cffex (by contract).
"""
import time
from datetime import timedelta

import pandas as pd

from .base import EMPTY, Source, compact, qlib_code, ts_code

MIRROR_CAP = 6000
ROW_CAP = 5000
MIRROR_PACE, MAIN_PACE = 1.5, 0.5
MAIN_ALIAS = {"express_vip": "express"}  # the REST front has no *_vip for express; its express takes period and pages
# Codes per call when a whole-market day exceeds a server's row cap. The REST front takes a comma-separated
# ts_code list on some interfaces only, and each has its own ceiling (above it the answer comes back empty).
SLICE = {"daily": 600, "adj_factor": 600, "moneyflow": 600, "daily_basic": 200, "cyq_perf": 200}

API = {  # table -> (tushare interface, how the key maps to parameters, paged-only)
    "cn.daily": ("daily", "day", False), "cn.adj_factor": ("adj_factor", "day", False),
    "cn.stock_basic": ("stock_basic", "stock_basic", False), "cn.trade_cal": ("trade_cal", "trade_cal", False), "cn.index_members": ("index_weight", "index_months", False),
    "cn.moneyflow": ("moneyflow", "day", False), "cn.margin": ("margin_detail", "day", False), "cn.chips": ("cyq_perf", "day", False),
    "cn.basic": ("daily_basic", "day", False), "cn.toplist": ("top_list", "day", False), "cn.block": ("block_trade", "day", False),
    "cn.fina": ("fina_indicator_vip", "period", False), "cn.forecast": ("forecast_vip", "period", False), "cn.express": ("express_vip", "period", False),
    "cn.holders": ("stk_holdernumber", "week", False), "cn.unlock": ("share_float", "week", True),
    "cn.index_daily": ("index_daily", "symbol", False), "cb.basic": ("cb_basic", "snapshot", False), "cb.daily": ("cb_daily", "day", False),
    "fut.cffex": ("fut_daily", "symbol", False),
}
DATE_COLUMN = {"day": "trade_date", "period": "end_date"}


class Capped(Exception):
    """The mirror answered its row cap: the answer is truncated and only the paging server can complete it."""


class Truncated(RuntimeError):
    """The REST server reported more rows than it returned (its paging returns nothing past the first page), so the
    key stays unfetched and the next run tries again, mirror first."""


class _Mirror:
    def __init__(self, token, url):
        import tushare as ts

        self.pro = ts.pro_api(token)
        self.pro._DataApi__http_url = url
        self.last = 0.0

    def query(self, api, params):
        wait = self.last + MIRROR_PACE - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            frame = self.pro.query(api, **params)
        finally:
            self.last = time.time()
        if len(frame) >= MIRROR_CAP:
            raise Capped(api)
        return frame


class _Main:
    def __init__(self, key, base):
        import requests

        self.session = requests.Session()
        self.session.headers["X-API-Key"] = key
        self.base = base.rstrip("/")
        self.last = 0.0
        self.limits = {}  # api -> page size the server accepts (learned from its 400 replies)

    def query(self, api, params):
        api = MAIN_ALIAS.get(api, api)
        rows, fields, offset = [], None, 0
        limit = self.limits.get(api, ROW_CAP)
        while True:
            wait = self.last + MAIN_PACE - time.time()
            if wait > 0:
                time.sleep(wait)
            reply = self.session.get(f"{self.base}/{api.replace('_', '-')}", params={**params, "limit": limit, "offset": offset}, timeout=60)
            self.last = time.time()
            if reply.status_code == 400 and "max_limit" in reply.text:  # high-cardinality interfaces page smaller
                limit = self.limits[api] = int(reply.json()["detail"]["max_limit"])
                continue
            reply.raise_for_status()
            body = reply.json()
            if body.get("code") != 0:
                raise RuntimeError(f"{api}: {body.get('msg') or body.get('code')}")
            data = body["data"]
            fields = fields or data["fields"]
            rows.extend(data["items"])
            if not data.get("has_more") or not data["items"]:
                break
            offset += len(data["items"])
        count = data.get("count")
        if isinstance(count, int) and count > len(rows):
            raise Truncated(f"{api} {params}: server holds {count} rows, returned {len(rows)}")
        return pd.DataFrame(rows, columns=fields)


class TushareReseller(Source):
    name = "tushare"

    def _stock_basic(self):
        """The master by exchange and status (each slice under the REST server's 5000-row cap)."""
        frames = []
        for exchange in ("SSE", "SZSE", "BSE"):
            for status in ("L", "D", "P"):
                frame = self._query("stock_basic", {"exchange": exchange, "list_status": status, "fields": "ts_code,symbol,name,area,industry,market,exchange,list_status,list_date,delist_date,is_hs"})
                if frame is not None and len(frame):
                    frames.append(frame)
        raw = pd.concat(frames, ignore_index=True)
        out = raw.copy()
        out["symbol"] = out["ts_code"].map(qlib_code)
        out["date"] = pd.to_datetime(out["list_date"], format="%Y%m%d", errors="coerce").fillna(pd.Timestamp("1900-01-01"))
        return out.dropna(subset=["symbol"]).drop(columns=["ts_code"])

    def _trade_cal(self):
        """Every calendar day of both exchanges from 2015 to the end of next year, a year at a time (a call is
        capped well below a year's rows)."""
        frames = []
        for year in range(2015, pd.Timestamp.today().year + 2):
            for exchange in ("SSE", "SZSE"):
                frame = self._query("trade_cal", {"exchange": exchange, "start_date": f"{year}0101", "end_date": f"{year}1231"})
                if frame is not None and len(frame):
                    frames.append(frame)
        raw = pd.concat(frames, ignore_index=True)
        return pd.DataFrame({"date": pd.to_datetime(raw["cal_date"], format="%Y%m%d"), "symbol": raw["exchange"].astype(str),
                             "exchange": raw["exchange"].astype(str), "is_open": pd.to_numeric(raw["is_open"], errors="coerce"),
                             "pretrade_date": raw.get("pretrade_date")}).dropna(subset=["date"])

    def _index_members(self, key):
        """Month-end constituents of one index from the resume date on (one call per month: a call is capped)."""
        code, _, since = key.partition("@")
        start = pd.Timestamp(since) if since else pd.Timestamp("2015-01-01")
        frames = []
        for month in pd.period_range(start.to_period("M"), pd.Timestamp.today().to_period("M"), freq="M"):
            first, last = month.start_time, month.end_time
            if last < start:
                continue
            frame = self._query("index_weight", {"index_code": code, "start_date": compact(first), "end_date": compact(last)})
            if frame is not None and len(frame):
                frames.append(frame)
        if not frames:
            return EMPTY.copy()
        raw = pd.concat(frames, ignore_index=True)
        out = pd.DataFrame({"date": pd.to_datetime(raw["trade_date"], format="%Y%m%d"), "symbol": raw["index_code"].astype(str),
                            "con_code": raw["con_code"].map(qlib_code), "weight": pd.to_numeric(raw["weight"], errors="coerce")})
        return out.dropna(subset=["con_code"]).reset_index(drop=True)

    def __init__(self, config):
        super().__init__(config)
        self.mirror = self.main = None
        self._universe = None
        if config.get("TUSHARE_MIRROR_TOKEN") and config.get("TUSHARE_MIRROR_URL"):
            self.mirror = _Mirror(config.get("TUSHARE_MIRROR_TOKEN"), config.get("TUSHARE_MIRROR_URL"))
        if config.get("DATAHUB_API_KEY") and config.get("DATAHUB_BASE"):
            self.main = _Main(config.get("DATAHUB_API_KEY"), config.get("DATAHUB_BASE"))
        if not (self.mirror or self.main):
            raise RuntimeError("no Tushare server configured (TUSHARE_MIRROR_TOKEN/URL or DATAHUB_API_KEY/BASE)")

    def tables(self):
        return list(API)

    def _query(self, api, params, paged_only=False):
        servers = ([self.main] if paged_only and self.main else []) or [s for s in (self.mirror, self.main) if s]
        last = None
        for server in servers:
            try:
                return server.query(api, params)
            except Capped as error:
                last = error
                continue  # the next server pages
            except Exception as error:  # noqa: BLE001
                last = error
                time.sleep(3)
        if isinstance(last, (Capped, Truncated)):  # no server could return the whole answer: the caller may slice it
            raise Truncated(f"{api} {params}: {last}") from last
        raise RuntimeError(f"{api} {params}: {last}")

    def day_universe(self):
        """Every A-share code the store knows, for slicing a whole-market day the servers cannot answer at once."""
        if self._universe is None:
            import quantdb

            store = quantdb.open()
            names = []
            for table in ("cn.stock_basic", "cn.daily"):
                if store.table_path(table).is_file():
                    names = sorted(set(store.sql(f'SELECT DISTINCT symbol AS s FROM "cn"."{table.split(".")[1]}"')["s"]))
                    if names:
                        break
            self._universe = [ts_code(n) for n in names if n[:2] in ("SH", "SZ")]
        return self._universe

    def _sliced_day(self, api, params):
        """A market-wide day in code slices: the mirror caps at 6000 rows and the REST front returns at most 5000
        with no working paging, so once a day outgrows them it is fetched a few hundred codes at a time."""
        codes = self.day_universe()
        if not codes:
            raise RuntimeError(f"{api}: cannot slice a day without a known universe (refresh cn.stock_basic first)")
        size = SLICE[api]
        frames = []
        for start in range(0, len(codes), size):
            frames.append(self._query(api, {**params, "ts_code": ",".join(codes[start:start + size])}))
        parts = [f for f in frames if f is not None and len(f)]
        if not parts:
            raise RuntimeError(f"{api}: slicing returned nothing for {params}")
        return pd.concat(parts, ignore_index=True)

    def fetch(self, table, key):
        api, kind, paged = API[table]
        if kind == "stock_basic":
            return self._stock_basic()
        if kind == "trade_cal":
            return self._trade_cal()
        if kind == "index_months":
            return self._index_members(key)
        if kind == "day":
            try:
                raw = self._query(api, {"trade_date": key}, paged)
            except Truncated:
                if api not in SLICE:
                    raise
                raw = self._sliced_day(api, {"trade_date": key})
            date_col = "trade_date"
        elif kind == "period":
            raw = self._query(api, {"period": key}, paged)
            date_col = "end_date"
        elif kind == "week":
            monday = pd.Timestamp(key)
            raw = self._query(api, {"start_date": compact(monday), "end_date": compact(monday + timedelta(days=6))}, paged)
            date_col = "float_date" if table == "cn.unlock" else "ann_date"
        elif kind == "symbol":
            code = key if "." in key else f"{key[2:]}.{key[:2]}"
            raw = self._query(api, {"ts_code": code}, paged)
            date_col = "trade_date"
        else:  # snapshot
            raw = self._query(api, {}, paged)
            date_col = "list_date"
        if raw is None or raw.empty:
            return EMPTY.copy()
        out = raw.copy()
        out["date"] = pd.to_datetime(out[date_col], format="%Y%m%d", errors="coerce")
        if table.startswith("cn."):
            out["symbol"] = out["ts_code"].map(qlib_code)
        else:
            out["symbol"] = out["ts_code"].astype(str)
        out = out.dropna(subset=["symbol"])
        if table in ("cb.basic",):
            out["date"] = out["date"].fillna(pd.Timestamp("1900-01-01"))
        return out.dropna(subset=["date"]).drop(columns=[c for c in ("ts_code",) if c in out.columns])
