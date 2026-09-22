import pandas as pd
import pytest

from quantdb import schema
from quantdb.recorders import Recorder
from quantdb.sources.base import Config, Source
from quantdb.store import Store
from quantdb.views import as_of, event_flags, next_trading_day


def frame(days, symbols, value=1.0):
    return pd.DataFrame([{"date": pd.Timestamp(d), "symbol": s, "x": value} for d in days for s in symbols])


def test_upsert_replaces_same_keys_and_tracks_done(tmp_path):
    store = Store(tmp_path)
    store.upsert("cn.t", frame(["2025-01-02"], ["SZ000001", "SH600000"]), done=["20250102"], source="test")
    store.upsert("cn.t", frame(["2025-01-02", "2025-01-03"], ["SZ000001"], 2.0), done=["20250103"])
    got = store.read("cn.t")
    assert len(got) == 2  # the whole 01-02 was replaced by the re-fetch (SH600000 gone), 01-03 added
    assert got.set_index(["date", "symbol"]).loc[(pd.Timestamp("2025-01-02"), "SZ000001"), "x"] == 2.0
    store.upsert("cn.t", frame(["2025-01-02"], ["SH600000"], 3.0), keys=("date", "symbol"))
    assert len(store.read("cn.t")) == 3  # by (date, symbol): the other name of that day stays
    meta = store.meta("cn.t")
    assert meta["done"] == ["20250102", "20250103"] and meta["source"] == "test" and meta["rows"] == 3
    assert (tmp_path / "snapshots" / "cn.t").exists()


def test_read_filters_and_wide_and_sql(tmp_path):
    store = Store(tmp_path)
    store.upsert("cn.t", frame(["2025-01-02", "2025-01-03", "2025-01-06"], ["A", "B"]))
    assert len(store.read("cn.t", start="2025-01-03")) == 4
    assert list(store.read("cn.t", symbols=["A"])["symbol"].unique()) == ["A"]
    assert store.read("cn.t", where="x > 5").empty
    wide = store.wide("cn.t", "x")
    assert wide.shape == (3, 2)
    assert store.sql("select count(*) n from cn.t")["n"][0] == 6


def test_replace_and_forget(tmp_path):
    store = Store(tmp_path)
    store.upsert("cn.t", frame(["2025-01-02"], ["A"]), done=["20250102"])
    store.forget("cn.t", ["20250102"])
    assert store.meta("cn.t")["done"] == []
    store.replace("cn.t", frame(["2025-02-01"], ["Z"]))
    assert list(store.read("cn.t")["symbol"]) == ["Z"]


def test_normalise_rejects_missing_columns(tmp_path):
    with pytest.raises(ValueError):
        Store(tmp_path).upsert("cn.t", pd.DataFrame({"date": []}))


class Fake(Source):
    name = "fake"
    calls = []

    def tables(self):
        return ["cn.fake_day", "cn.fake_period", "cn.fake_week", "cn.fake_sym"]

    def fetch(self, table, key):
        Fake.calls.append((table, key))
        if key.endswith("fail"):
            raise RuntimeError("boom")
        day = pd.Timestamp(key.split("@")[0]) if key[0].isdigit() else pd.Timestamp("2025-01-02")
        return pd.DataFrame({"date": [day], "symbol": [key.split("@")[0] if not key[0].isdigit() else "A"], "v": [1.0]})


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(schema, "TABLES", dict(schema.TABLES))
    schema.register(schema.Table("cn.fake_day", "day", ("fake",), ""))
    schema.register(schema.Table("cn.fake_period", "period", ("fake",), "", open_days=150))
    schema.register(schema.Table("cn.fake_week", "week", ("fake",), "", open_weeks=1))
    schema.register(schema.Table("cn.fake_sym", "symbol", ("fake",), "", universe="test"))
    import quantdb.sources as sources

    monkeypatch.setitem(sources.REGISTRY, "fake", Fake)
    sources._load()
    monkeypatch.setitem(sources.REGISTRY, "fake", Fake)
    Fake.calls.clear()


def make_recorder(tmp_path, monkeypatch):
    store = Store(tmp_path)
    (tmp_path / ".env").write_text("")
    rec = Recorder(store, Config(tmp_path))
    monkeypatch.setattr(rec, "calendar", lambda table: pd.DatetimeIndex(pd.bdate_range("2025-01-01", "2025-01-10")))
    monkeypatch.setattr(rec, "universe", lambda name: ["A", "B"])
    return rec


def test_day_refresh_is_incremental(tmp_path, monkeypatch, registry):
    rec = make_recorder(tmp_path, monkeypatch)
    first = rec.refresh("cn.fake_day", start="2025-01-06", end="2025-01-08")
    assert first["done"] == 3 and len(Fake.calls) == 3
    again = rec.refresh("cn.fake_day", start="2025-01-06", end="2025-01-10")
    assert again["keys"] == 2 and again["done"] == 2
    assert rec.store.meta("cn.fake_day")["done"] == ["20250106", "20250107", "20250108", "20250109", "20250110"]


def test_period_refetches_open_seasons(tmp_path, monkeypatch, registry):
    rec = make_recorder(tmp_path, monkeypatch)
    table = schema.get("cn.fake_period")
    keys = rec.keys(table, start="2024-01-01", end="2025-01-15")
    assert keys[0] == "20231231" and keys[-1] == "20241231"
    rec.store.upsert("cn.fake_period", frame(["2024-12-31"], ["A"]), done=keys)
    assert rec.keys(table, start="2024-01-01", end="2025-01-15") == ["20240930", "20241231"]  # still within 150 days


def test_week_keys_refetch_recent_and_symbol_resumes(tmp_path, monkeypatch, registry):
    rec = make_recorder(tmp_path, monkeypatch)
    keys = rec.keys(schema.get("cn.fake_week"), start="2025-01-01", end="2025-01-20")
    assert keys == ["20241230", "20250106", "20250113", "20250120"]
    rec.store.upsert("cn.fake_week", frame(["2025-01-06"], ["A"]), done=keys)
    assert rec.keys(schema.get("cn.fake_week"), start="2025-01-01", end="2025-01-20") == ["20250120"]
    rec.store.upsert("cn.fake_sym", frame(["2025-01-06"], ["A"]))
    assert rec.keys(schema.get("cn.fake_sym")) == ["A@2025-01-07", "B"]


def test_failed_key_is_not_marked_done(tmp_path, monkeypatch, registry):
    rec = make_recorder(tmp_path, monkeypatch)
    monkeypatch.setattr(rec, "universe", lambda name: ["A", "fail"])
    out = rec.refresh("cn.fake_sym")
    assert out["done"] == 1 and out["failed"][0][0] == "fail"
    assert rec.store.meta("cn.fake_sym")["done"] == ["A"]


def test_point_in_time_views():
    cal = pd.DatetimeIndex(pd.bdate_range("2025-01-01", "2025-01-15"))
    events = pd.DataFrame({"date": [pd.Timestamp("2025-01-03"), pd.Timestamp("2025-01-04"), pd.Timestamp("2025-01-08")], "symbol": ["A", "B", "A"], "v": [1.0, 2.0, 3.0]})
    nxt = next_trading_day(events["date"], cal)
    assert list(nxt.dt.strftime("%m-%d")) == ["01-06", "01-06", "01-09"]  # Friday news → Monday; Saturday → Monday
    pit = as_of(events, cal, "v")
    assert pd.isna(pit.loc["2025-01-03", "A"]) and pit.loc["2025-01-06", "A"] == 1.0 and pit.loc["2025-01-09", "A"] == 3.0 and pit.loc["2025-01-15", "B"] == 2.0
    flags = event_flags(events, cal, window=2)
    assert flags.loc["2025-01-06", "A"] == 1 and flags.loc["2025-01-07", "A"] == 1 and flags.loc["2025-01-08", "A"] == 0


def test_upsert_aligns_dtypes(tmp_path):
    store = Store(tmp_path)
    store.upsert("cn.t", frame(["2025-01-02"], ["A"]).assign(ann=20250102, tag=1))
    store.upsert("cn.t", frame(["2025-01-03"], ["A"]).assign(ann="20250103", tag="x"))
    got = store.read("cn.t")
    assert got["ann"].tolist() == [20250102, 20250103]
    assert got["tag"].astype(str).tolist() == ["1", "x"]
