import numpy as np
import pandas as pd

from quantdb.export.qlib import export_qlib, membership_spans
from quantdb.store import Store


def test_export_writes_qlib_bins_and_spans(tmp_path):
    store = Store(tmp_path / "db")
    days = pd.bdate_range("2025-01-01", periods=6)
    rows = []
    for i, d in enumerate(days):
        rows.append({"date": d, "symbol": "SH600000", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0 + i, "vol": 1000.0, "amount": 500.0})
        if i >= 2:
            rows.append({"date": d, "symbol": "SZ000001", "open": 5.0, "high": 5.0, "low": 5.0, "close": 5.0, "vol": 200.0, "amount": 100.0})
    store.upsert("cn.daily", pd.DataFrame(rows))
    adj = [{"date": d, "symbol": "SH600000", "adj_factor": 2.0 if i < 3 else 4.0} for i, d in enumerate(days)] + [{"date": d, "symbol": "SZ000001", "adj_factor": 1.0} for d in days[2:]]
    store.upsert("cn.adj_factor", pd.DataFrame(adj))
    store.upsert("cn.index_members", pd.DataFrame([{"date": days[1], "symbol": "000300.SH", "con_code": "SH600000", "weight": 1.0},
                                                    {"date": days[4], "symbol": "000300.SH", "con_code": "SZ000001", "weight": 1.0}]), keys=("date", "symbol"))
    out = tmp_path / "cn_data"
    result = export_qlib(store, out)
    assert result["days"] == 6 and result["instruments"]["all"] == 2
    cal = (out / "calendars" / "day.txt").read_text().split()
    assert cal[0] == "2025-01-01" and len(cal) == 6
    close = np.fromfile(out / "features" / "sh600000" / "close.day.bin", dtype="<f4")
    factor = np.fromfile(out / "features" / "sh600000" / "factor.day.bin", dtype="<f4")
    assert close[0] == 0 and factor[1] == 1.0 and factor[4] == 2.0  # factor relative to the first day; 2× after the split
    assert close[4] == np.float32(13.0 * 2.0) and np.isclose(close[4] / factor[4], 13.0)  # raw = adjusted / factor
    volume = np.fromfile(out / "features" / "sh600000" / "volume.day.bin", dtype="<f4")
    assert volume[4] == np.float32(1000.0 / 2.0)
    other = np.fromfile(out / "features" / "sz000001" / "close.day.bin", dtype="<f4")
    assert other[0] == 2 and len(other) == 5  # starts on the third calendar day
    spans = (out / "instruments" / "all.txt").read_text().splitlines()
    assert spans == ["SH600000\t2025-01-01\t2025-01-08", "SZ000001\t2025-01-03\t2025-01-08"]
    csi = (out / "instruments" / "csi300.txt").read_text().splitlines()
    assert csi == ["SH600000\t2025-01-02\t2025-01-07", "SZ000001\t2025-01-08\t2025-01-08"]  # list of day 2 holds through day 5's list; then the new list
    # a second export swaps cleanly
    assert export_qlib(store, out)["days"] == 6 and not (tmp_path / "cn_data.new").exists()


def test_export_stops_at_the_last_complete_day(tmp_path):
    store = Store(tmp_path / "db")
    days = pd.bdate_range("2025-01-01", periods=3)
    rows = [{"date": d, "symbol": "SH600000", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0 + i, "vol": 10.0, "amount": 5.0} for i, d in enumerate(days)]
    store.upsert("cn.daily", pd.DataFrame(rows), done=[d.strftime("%Y%m%d") for d in days[:2]])  # the last day is still arriving
    store.upsert("cn.adj_factor", pd.DataFrame([{"date": d, "symbol": "SH600000", "adj_factor": 1.0} for d in days]))
    result = export_qlib(store, tmp_path / "cn_data")
    assert result["days"] == 2 and result["through"] == str(days[1].date())
    assert (tmp_path / "cn_data" / "calendars" / "day.txt").read_text().split()[-1] == str(days[1].date())
    assert export_qlib(store, tmp_path / "cn_data", through=str(days[2].date()))["days"] == 3
