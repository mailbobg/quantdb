"""OpenBB Platform as one source among others (``pip install quantdb[openbb]``, in quantdb's own venv).

OpenBB is a router over providers, so one adapter reaches yfinance, Nasdaq, FINRA, SEC, FRED and the paid ones
(FMP, Intrinio, …) through the same call shape; providers with keys are configured in OpenBB's own settings
(``~/.openbb_platform/user_settings.json``) or through ``OPENBB_<PROVIDER>_API_KEY`` in quantdb's .env.

Serves (all keyless):
    us.daily              equity.price.historical, yfinance, one fetch per symbol resumed from the last date
    us.earnings_calendar  equity.calendar.earnings, Nasdaq, by week; consensus and actual EPS, re-fetched while recent
    us.short_interest     equity.shorts.short_interest, FINRA, per symbol; adapter kept, table not registered:
                          cdn.finra.org answers 403 from this network
"""
import pandas as pd

from .base import EMPTY, Source, retry

PROVIDERS = {"us.daily": "yfinance", "us.earnings_calendar": "nasdaq", "us.short_interest": "finra"}


class OpenBB(Source):
    name = "openbb"

    def __init__(self, config):
        super().__init__(config)
        self._obb = None

    def tables(self):
        return list(PROVIDERS)

    def obb(self):
        if self._obb is None:
            from openbb import obb

            for key, value in self.config.values.items():
                if key.startswith("OPENBB_") and key.endswith("_API_KEY") and value:
                    provider = key[len("OPENBB_"):-len("_API_KEY")].lower()
                    try:
                        setattr(obb.user.credentials, f"{provider}_api_key", value)
                    except Exception:  # noqa: BLE001  unknown provider name
                        pass
            self._obb = obb
        return self._obb

    def fetch(self, table, key):
        provider = self.config.get(f"QUANTDB_OPENBB_{table.split('.')[1].upper()}_PROVIDER", PROVIDERS[table])
        obb = self.obb()
        if table == "us.daily":
            symbol, _, since = key.partition("@")
            out = retry(lambda: obb.equity.price.historical(symbol, start_date=since or "2000-01-01", provider=provider).to_df(), attempts=3, backoff=(3, 10))
            if out is None or out.empty:
                return EMPTY.copy()
            out = out.reset_index().rename(columns={"index": "date"})
            out["symbol"] = symbol.upper()
            return out[["date", "symbol", *[c for c in out.columns if c not in ("date", "symbol")]]]
        if table == "us.earnings_calendar":
            monday = pd.Timestamp(key)
            out = retry(lambda: obb.equity.calendar.earnings(start_date=str(monday.date()), end_date=str((monday + pd.Timedelta(days=6)).date()), provider=provider).to_df(), attempts=3, backoff=(3, 10))
            if out is None or out.empty:
                return EMPTY.copy()
            out = out.reset_index(drop=True)
            out["date"] = pd.to_datetime(out["report_date"])
            out["symbol"] = out["symbol"].astype(str).str.upper()
            return out
        if table == "us.short_interest":
            symbol = key.split("@")[0]
            out = retry(lambda: obb.equity.shorts.short_interest(symbol, provider=provider).to_df(), attempts=3, backoff=(3, 10))
            if out is None or out.empty:
                return EMPTY.copy()
            out = out.reset_index(drop=True)
            out["date"] = pd.to_datetime(out["settlement_date"])
            out["symbol"] = symbol.upper()
            return out
        return EMPTY.copy()
