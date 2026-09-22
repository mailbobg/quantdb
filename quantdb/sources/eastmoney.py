"""Eastmoney through akshare: convertible-bond daily conversion value / premium (bond_zh_cov_value_analysis), the
point-in-time premium the Tushare tables lack. Symbol-keyed by bond code (113050.SH or 113050)."""
import pandas as pd

from .base import EMPTY, Source


class Eastmoney(Source):
    name = "eastmoney"

    def tables(self):
        return ["cb.premium"]

    def fetch(self, table, key):
        import akshare as ak

        code = key.split(".")[0].split("@")[0]
        raw = ak.bond_zh_cov_value_analysis(symbol=code)
        if raw is None or raw.empty:
            return EMPTY.copy()
        out = pd.DataFrame({"date": pd.to_datetime(raw["日期"], errors="coerce"), "symbol": key.split("@")[0],
                            "close": pd.to_numeric(raw["收盘价"], errors="coerce"), "bond_value": pd.to_numeric(raw["纯债价值"], errors="coerce"),
                            "conv_value": pd.to_numeric(raw["转股价值"], errors="coerce"), "bond_premium": pd.to_numeric(raw["纯债溢价率"], errors="coerce"),
                            "conv_premium": pd.to_numeric(raw["转股溢价率"], errors="coerce")})
        return out.dropna(subset=["date"])
