"""baostock: daily turnover, valuation ratios, ST flag per A-share (free, no key). Symbol-keyed: one fetch per
instrument from the day after what the store holds (the recorder passes ``key`` as ``SZ000001`` or
``SZ000001@2025-01-03`` to start from a date)."""
import pandas as pd

from .base import EMPTY, Source

FIELDS = "date,code,close,volume,amount,turn,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST"


class Baostock(Source):
    name = "baostock"

    def __init__(self, config):
        super().__init__(config)
        self._session = None

    def tables(self):
        return ["cn.baostock"]

    def _login(self):
        import baostock as bs

        if self._session is None:
            r = bs.login()
            if r.error_code != "0":
                raise RuntimeError(f"baostock login failed: {r.error_msg}")
            self._session = bs
        return self._session

    def fetch(self, table, key):
        code, _, since = key.partition("@")
        if code[:2] not in ("SH", "SZ"):
            return EMPTY.copy()
        bs = self._login()
        rs = bs.query_history_k_data_plus(f"{code[:2].lower()}.{code[2:]}", FIELDS, start_date=since or "2010-01-01", end_date="2100-01-01", frequency="d", adjustflag="3")
        if rs.error_code != "0":
            raise RuntimeError(rs.error_msg)
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return EMPTY.copy()
        out = pd.DataFrame(rows, columns=FIELDS.split(","))
        for c in ("close", "volume", "amount", "turn", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM", "isST"):
            out[c] = pd.to_numeric(out[c], errors="coerce")
        out["date"] = pd.to_datetime(out["date"]); out["symbol"] = code
        return out.drop(columns=["code"])
