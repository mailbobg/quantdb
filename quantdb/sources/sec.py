"""SEC EDGAR, free and unmetered (a User-Agent naming the user is required):

us.form4     the quarterly insider-transactions data sets (Form 3/4/5), all quarters since 2017; snapshot table
us.eps_xbrl  quarterly diluted EPS from companyconcept, one fetch per symbol, dated by the first filing
"""
import io
import zipfile
from datetime import date

import pandas as pd
import requests

from .base import EMPTY, Source, retry

UA = {"User-Agent": "quantdb research (set QUANTDB_CONTACT) "}


class Sec(Source):
    name = "sec"

    def __init__(self, config):
        super().__init__(config)
        self.headers = {"User-Agent": f"quantdb research {config.get('QUANTDB_CONTACT', 'anonymous@example.com')}"}
        self._ciks = None

    def tables(self):
        return ["us.form4", "us.eps_xbrl"]

    def _quarters(self):
        today = date.today()
        for year in range(2017, today.year + 1):
            for q in range(1, 5):
                if year == today.year and q > (today.month - 1) // 3 + 1:
                    break
                yield f"{year}q{q}"

    def fetch(self, table, key):
        if table == "us.form4":
            return self._form4()
        return self._eps(key)

    def _form4(self):
        frames = []
        for q in self._quarters():
            url = f"https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{q}_form345.zip"
            reply = requests.get(url, headers=self.headers, timeout=180)
            if reply.status_code != 200:
                continue  # not published yet
            z = zipfile.ZipFile(io.BytesIO(reply.content))
            sub = pd.read_csv(z.open("SUBMISSION.tsv"), sep="\t", dtype=str, usecols=["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERTRADINGSYMBOL"], low_memory=False)
            tr = pd.read_csv(z.open("NONDERIV_TRANS.tsv"), sep="\t", dtype=str, usecols=["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES", "TRANS_PRICEPERSHARE"], low_memory=False)
            ow = pd.read_csv(z.open("REPORTINGOWNER.tsv"), sep="\t", dtype=str, usecols=["ACCESSION_NUMBER", "RPTOWNER_RELATIONSHIP"], low_memory=False)
            sub = sub[sub.DOCUMENT_TYPE.isin(["4", "4/A"])]
            rel = ow.groupby("ACCESSION_NUMBER")["RPTOWNER_RELATIONSHIP"].agg(lambda s: "|".join(s.dropna().str.lower()))
            tr = tr[tr.TRANS_CODE.isin(["P", "S"])].merge(sub, on="ACCESSION_NUMBER").merge(rel.rename("relationship"), left_on="ACCESSION_NUMBER", right_index=True, how="left")
            tr["shares"] = pd.to_numeric(tr.TRANS_SHARES, errors="coerce"); tr["price"] = pd.to_numeric(tr.TRANS_PRICEPERSHARE, errors="coerce")
            frames.append(pd.DataFrame({"date": pd.to_datetime(tr.FILING_DATE, format="%d-%b-%Y", errors="coerce"), "symbol": tr.ISSUERTRADINGSYMBOL.str.upper().str.strip(),
                                        "filed": pd.to_datetime(tr.FILING_DATE, format="%d-%b-%Y", errors="coerce"), "trans_date": pd.to_datetime(tr.TRANS_DATE, format="%d-%b-%Y", errors="coerce"),
                                        "trans_code": tr.TRANS_CODE, "shares": tr.shares, "price": tr.price, "value": tr.shares * tr.price,
                                        "relationship": tr.relationship, "accession": tr.ACCESSION_NUMBER, "quarter": q}))
        if not frames:
            return EMPTY.copy()
        return pd.concat(frames, ignore_index=True).dropna(subset=["date", "symbol"])

    def _cik(self, symbol):
        if self._ciks is None:
            data = retry(lambda: requests.get("https://www.sec.gov/files/company_tickers.json", headers=self.headers, timeout=60).json())
            self._ciks = {v["ticker"].upper(): f"{int(v['cik_str']):010d}" for v in data.values()}
        return self._ciks.get(symbol.upper())

    def _eps(self, symbol):
        cik = self._cik(symbol)
        if not cik:
            return EMPTY.copy()
        reply = requests.get(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/EarningsPerShareDiluted.json", headers=self.headers, timeout=60)
        if reply.status_code != 200:
            return EMPTY.copy()
        rows = []
        for unit, facts in (reply.json().get("units") or {}).items():
            for f in facts:
                if f.get("form") not in ("10-Q", "10-K") or not f.get("start"):
                    continue
                s, e = pd.Timestamp(f["start"]), pd.Timestamp(f["end"]); days = (e - s).days
                kind = "Q" if 80 <= days <= 100 else "Y" if 350 <= days <= 380 else None
                if kind:
                    rows.append({"kind": kind, "start": s, "end": e, "filed": pd.Timestamp(f["filed"]), "eps": f["val"], "form": f["form"]})
        q = pd.DataFrame(rows)
        if q.empty:
            return EMPTY.copy()
        q = q.sort_values("filed").drop_duplicates(["kind", "end"], keep="first")
        quarters, years = q[q.kind == "Q"].copy(), q[q.kind == "Y"]
        extra = []
        for _, y in years.iterrows():
            inside = quarters[(quarters.end > y.start) & (quarters.end <= y.end)]
            if len(inside) == 3 and (y.end - inside.end.max()).days > 60:
                extra.append({"kind": "Q", "start": inside.end.max(), "end": y.end, "filed": y.filed, "eps": y.eps - inside.eps.sum(), "form": y.form})
        if extra:
            quarters = pd.concat([quarters, pd.DataFrame(extra)], ignore_index=True)
        quarters = quarters.sort_values("end").drop_duplicates("end", keep="first")
        quarters = quarters[(quarters.filed - quarters.end).dt.days.between(10, 120)]
        return pd.DataFrame({"date": quarters["end"], "symbol": symbol.upper(), "end": quarters["end"], "filed": quarters["filed"], "eps": quarters["eps"].astype(float), "form": quarters["form"]})
