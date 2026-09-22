"""Alpha Vantage EARNINGS (quarterly reported vs estimated EPS with the report date), key ALPHAVANTAGE_API_KEY.
The free tier allows 25 calls a day, so the universe is whatever has been crawled; symbol-keyed."""
import pandas as pd
import requests

from .base import EMPTY, Source


class AlphaVantage(Source):
    name = "alphavantage"

    def tables(self):
        return ["us.earnings_av"]

    def fetch(self, table, key):
        (api_key,) = self.config.require("ALPHAVANTAGE_API_KEY")
        symbol = key.split("@")[0]
        body = requests.get("https://www.alphavantage.co/query", params={"function": "EARNINGS", "symbol": symbol, "apikey": api_key}, timeout=60).json()
        if "quarterlyEarnings" not in body:
            raise RuntimeError(body.get("Information") or body.get("Note") or str(body)[:200])
        return parse_earnings(body, symbol)


def parse_earnings(body, symbol):
    raw = pd.DataFrame(body.get("quarterlyEarnings") or [])
    if raw.empty:
        return EMPTY.copy()
    out = pd.DataFrame({"date": pd.to_datetime(raw["reportedDate"], errors="coerce"), "symbol": symbol.upper(), "fiscal_end": pd.to_datetime(raw["fiscalDateEnding"], errors="coerce"),
                        "eps": pd.to_numeric(raw["reportedEPS"], errors="coerce"), "eps_estimate": pd.to_numeric(raw.get("estimatedEPS"), errors="coerce"),
                        "surprise_pct": pd.to_numeric(raw.get("surprisePercentage"), errors="coerce"), "report_time": raw.get("reportTime")})
    return out.dropna(subset=["date"])
