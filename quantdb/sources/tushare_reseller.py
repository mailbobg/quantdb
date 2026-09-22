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

from .base import EMPTY, Source, compact, qlib_code

MIRROR_CAP = 6000
ROW_CAP = 5000
MIRROR_PACE, MAIN_PACE = 1.5, 0.5
MAIN_ALIAS = {"express_vip": "express"}  # the REST front has no *_vip for express; its express takes period and pages

API = {  # table -> (tushare interface, how the key maps to parameters, paged-only)
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

    def __init__(self, config):
        super().__init__(config)
        self.mirror = self.main = None
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
        raise RuntimeError(f"{api} {params}: {last}")

    def fetch(self, table, key):
        api, kind, paged = API[table]
        if kind == "day":
            raw = self._query(api, {"trade_date": key}, paged)
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
