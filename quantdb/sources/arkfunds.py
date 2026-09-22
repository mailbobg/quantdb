"""ARK ETF daily trade disclosures from arkfunds.io (free, no key). Snapshot table: the whole history each refresh."""
import pandas as pd
import requests

from .base import EMPTY, Source

FUNDS = ("ARKK", "ARKW", "ARKG", "ARKQ", "ARKF", "ARKX")


class ArkFunds(Source):
    name = "arkfunds"

    def tables(self):
        return ["us.ark_trades"]

    def fetch(self, table, key):
        frames = []
        for fund in FUNDS:
            reply = requests.get("https://arkfunds.io/api/v2/etf/trades", params={"symbol": fund, "date_from": "2020-01-01", "date_to": "2100-01-01"}, timeout=120)
            if reply.status_code == 200 and reply.json().get("trades"):
                frames.append(pd.DataFrame(reply.json()["trades"]))
        if not frames:
            return EMPTY.copy()
        raw = pd.concat(frames, ignore_index=True)
        return pd.DataFrame({"date": pd.to_datetime(raw["date"]), "symbol": raw["ticker"].astype(str).str.upper(), "fund": raw["fund"], "direction": raw["direction"],
                             "shares": pd.to_numeric(raw["shares"], errors="coerce"), "etf_percent": pd.to_numeric(raw["etf_percent"], errors="coerce"), "company": raw["company"]})
