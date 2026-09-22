"""FTShare (非凸科技) MCP gateway, setting FTSHARE_API_KEY. Streamable HTTP with SSE replies. Serves
cn.insider (董监高持股变动, week-keyed by change date)."""
import json
import time

import pandas as pd
import requests

from .base import EMPTY, Source

URL = "https://market.ft.tech/gateway/mcp"


class FTShare(Source):
    name = "ftshare"

    def __init__(self, config):
        super().__init__(config)
        (self.key,) = config.require("FTSHARE_API_KEY")
        self.headers = {"FTSHARE_API_KEY": self.key, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        self.sid, self.n = None, 1

    def tables(self):
        return ["cn.insider"]

    def _post(self, payload, want_id):
        headers = dict(self.headers)
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        for attempt in range(5):
            try:
                with requests.post(URL, headers=headers, json=payload, timeout=(20, 90), stream=True) as r:
                    if want_id is None:
                        return r, None
                    if "text/event-stream" not in r.headers.get("Content-Type", ""):
                        return r, r.json()
                    buf = []
                    for raw in r.iter_lines():
                        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
                        if line == "":
                            body = "\n".join(buf).strip(); buf = []
                            if body.startswith("{"):
                                try:
                                    msg = json.loads(body)
                                except ValueError:
                                    continue
                                if msg.get("id") == want_id:
                                    return r, msg
                        elif line.startswith("data:"):
                            buf.append(line[5:].lstrip())
                    return r, None
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
                time.sleep(2 + 3 * attempt)
        raise RuntimeError("FTShare gateway unreachable")

    def _session(self):
        if self.sid:
            return
        r, _ = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "quantdb", "version": "0.1"}}}, 1)
        self.sid = r.headers.get("Mcp-Session-Id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, None)

    def call(self, tool, **args):
        self._session(); self.n += 1
        _, msg = self._post({"jsonrpc": "2.0", "id": self.n, "method": "tools/call", "params": {"name": tool, "arguments": args}}, self.n)
        if msg is None:
            raise RuntimeError(f"no reply for {tool}")
        if "error" in msg:
            raise RuntimeError(json.dumps(msg["error"], ensure_ascii=False)[:300])
        res = msg["result"]
        if res.get("isError"):
            raise RuntimeError((res.get("content") or [{}])[0].get("text", "error")[:300])
        return res.get("structuredContent") or {}

    def fetch(self, table, key):
        monday = pd.Timestamp(key); sunday = monday + pd.Timedelta(days=6)
        rows, page = [], 1
        while True:
            out = self.call("ft_v1_holder_stock_ggmx", start_date=str(monday.date()), end_date=str(sunday.date()), page=page, page_size=500)
            data = out.get("data") or []
            rows.extend(data)
            pagination = (out.get("metadata") or {}).get("pagination") or {}
            if not data or not pagination.get("has_more"):
                break
            page += 1
        if not rows:
            return EMPTY.copy()
        raw = pd.DataFrame(rows)
        code_col = _first(raw, "stock_code", "code", "SECURITY_CODE")
        date_col = _first(raw, "change_date", "CHANGE_DATE", "date")
        if not code_col or not date_col:
            raise RuntimeError(f"unexpected ggmx columns: {list(raw.columns)[:20]}")
        code = raw[code_col].astype(str).str.extract(r"(\d{6})")[0].str.zfill(6)
        out = raw.drop(columns=[c for c in ("crawl_batch_ts", "crawl_date", "data_time", "source") if c in raw.columns])
        out["date"] = pd.to_datetime(raw[date_col], errors="coerce")
        out["symbol"] = code.map(lambda c: (("SH" if c.startswith(("6", "9")) else "SZ") + c) if isinstance(c, str) and not c.startswith(("4", "8")) else None)
        for c in out.columns:
            if c not in ("date", "symbol") and out[c].dtype == object:
                numeric = pd.to_numeric(out[c], errors="coerce")
                if numeric.notna().sum() >= out[c].notna().sum() * 0.9 and out[c].notna().any() and not c.endswith(("name", "code", "date")):
                    out[c] = numeric
        return out.dropna(subset=["date", "symbol"])


def _first(frame, *names):
    for n in names:
        if n in frame.columns:
            return n
    return None
