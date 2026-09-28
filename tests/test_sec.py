import pandas as pd

from quantdb.sources.sec import parse_spinoff_hits


def hit(name, cik, file_date):
    return {"_source": {"display_names": [name], "ciks": [cik], "file_date": file_date, "form": "10-12B"}}


def test_spinoff_hits_keep_the_first_filing_per_company_and_read_the_ticker():
    hits = [hit("Lands End Inc  (LE)  (CIK 0000799288)", "0000799288", "2014-01-10"),
            hit("Lands End Inc  (LE)  (CIK 0000799288)", "0000799288", "2013-12-06"),  # amendment order is not chronological
            hit("Liberty Spinco, Inc.  (FWONA, FWONB, FWONK)  (CIK 0001560385)", "0001560385", "2012-10-19"),
            hit("Aabaco Holdings, Inc.  (CIK 0001646775)", "0001646775", "2015-11-16"),  # not listed today: no ticker
            {"_source": {"display_names": ["broken"], "ciks": [], "file_date": "2015-01-01"}}]
    out = parse_spinoff_hits(hits)
    assert len(out) == 3 and list(out.columns) == ["date", "symbol", "cik", "name", "file_date", "ticker"]
    le = out[out.cik == "0000799288"].iloc[0]
    assert le.file_date == "2013-12-06" and le.ticker == "LE" and le.symbol == "LE" and le["name"] == "Lands End Inc" and le.date == pd.Timestamp("2013-12-06")
    assert out[out.cik == "0001560385"].iloc[0].ticker == "FWONA"
    unlisted = out[out.cik == "0001646775"].iloc[0]
    assert unlisted.ticker == "" and unlisted.symbol == "CIK0001646775"
    assert parse_spinoff_hits([]).empty
