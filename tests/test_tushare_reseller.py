import pandas as pd

from quantdb.sources import tushare_reseller as tr


class FakeMirror:
    def __init__(self):
        self.calls = []

    def query(self, api, params):
        self.calls.append((api, params))
        if api == "share_float":
            raise tr.Capped(api)  # 6000-row answer: truncated
        if api == "moneyflow" and params.get("trade_date") == "20250103":
            raise RuntimeError("token daily limit exceeded")
        return pd.DataFrame({"ts_code": ["600000.SH", "920000.BJ"], "trade_date": [params["trade_date"]] * 2, "x": [1.0, 2.0]})


class FakeMain:
    def __init__(self):
        self.calls = []

    def query(self, api, params):
        self.calls.append((api, params))
        if api == "share_float":
            return pd.DataFrame({"ts_code": ["600000.SH"], "ann_date": ["20250101"], "float_date": ["20250108"], "float_share": [1.0]})
        return pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [params["trade_date"]], "x": [3.0]})


def source():
    src = tr.TushareReseller.__new__(tr.TushareReseller)
    src.mirror, src.main = FakeMirror(), FakeMain()
    return src


def test_day_fetch_prefers_mirror_and_drops_beijing(monkeypatch):
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    src = source()
    out = src.fetch("cn.moneyflow", "20250102")
    assert list(out["symbol"]) == ["SH600000"] and out["date"].iloc[0] == pd.Timestamp("2025-01-02") and "ts_code" not in out.columns
    assert src.main.calls == []


def test_capped_and_failed_answers_go_to_the_paging_server(monkeypatch):
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    src = source()
    unlock = src.fetch("cn.unlock", "20250106")  # paged-only table: straight to main
    assert src.mirror.calls == [] and src.main.calls[0][0] == "share_float" and unlock["date"].iloc[0] == pd.Timestamp("2025-01-08")
    out = src.fetch("cn.moneyflow", "20250103")  # mirror quota exhausted: main answers
    assert list(out["symbol"]) == ["SZ000001"] and src.main.calls[-1] == ("moneyflow", {"trade_date": "20250103"})


def test_main_server_learns_the_page_size_from_a_400(monkeypatch):
    class Reply:
        def __init__(self, status, body):
            self.status_code, self._body = status, body
            self.text = str(body)

        def json(self):
            return self._body

        def raise_for_status(self):
            pass

    seen = []

    class Session:
        headers = {}

        def get(self, url, params, timeout):
            seen.append(params["limit"])
            if params["limit"] > 1000:
                return Reply(400, {"code": 400, "msg": "query limit is too large", "detail": {"max_limit": 1000}})
            return Reply(200, {"code": 0, "data": {"fields": ["ts_code", "x"], "items": [["600000.SH", 1]], "has_more": False}})

    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    main = tr._Main.__new__(tr._Main)
    main.session, main.base, main.last, main.limits = Session(), "http://x", 0.0, {}
    frame = main.query("stk_holdernumber", {"start_date": "20250106"})
    assert seen == [tr.ROW_CAP, 1000] and len(frame) == 1 and main.limits["stk_holdernumber"] == 1000
    main.query("stk_holdernumber", {"start_date": "20250113"})
    assert seen[-1] == 1000
