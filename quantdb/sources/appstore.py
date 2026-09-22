"""Apple App Store top charts (public RSS, no key): one snapshot per day per country and chart (the feed serves at most 100). Day-keyed; the
feed only serves today, so a day fetched later than the same day comes back empty (history builds forward)."""
from datetime import date

import pandas as pd
import requests

from .base import EMPTY, Source

COUNTRIES = ("us", "cn")
CHARTS = ("top-free", "top-paid")


class AppStore(Source):
    name = "appstore"

    def tables(self):
        return ["alt.appstore_top"]

    def fetch(self, table, key):
        if key != date.today().strftime("%Y%m%d"):
            return EMPTY.copy()
        rows = []
        for country in COUNTRIES:
            for chart in CHARTS:
                reply = requests.get(f"https://rss.marketingtools.apple.com/api/v2/{country}/apps/{chart}/100/apps.json", timeout=60)
                if reply.status_code != 200:
                    continue
                for rank, r in enumerate(reply.json()["feed"]["results"], 1):
                    rows.append({"date": pd.Timestamp(key), "symbol": f"{country}:{chart}:{r['id']}", "country": country, "chart": chart, "rank": rank,
                                 "app_id": r["id"], "app": r["name"], "developer": r.get("artistName"), "genre": (r.get("genres") or [{}])[0].get("name")})
        return pd.DataFrame(rows) if rows else EMPTY.copy()
