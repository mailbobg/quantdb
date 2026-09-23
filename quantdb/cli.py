"""quantdb command line.

    quantdb init [--secrets FILE]         create $QUANTDB_HOME with a .env (copying the known keys from FILE)
    quantdb status                        what is stored
    quantdb tables                        what is registered
    quantdb refresh cn.moneyflow [--start 2020-01-01] [--limit 50] [--source tushare]
    quantdb refresh --namespace cn
    quantdb sql "select count(*) from cn.margin"
    quantdb export-qlib DIR [--start 2015-01-01]  write a Qlib provider directory from cn.daily / cn.adj_factor / cn.index_members
    quantdb import-legacy TABLE PATH ...  load an existing CSV/parquet/pickle cache into a table
    quantdb import-studio ROOT            load RD-Agent Studio's extra/ cache (tushare + baostock)
    quantdb forget cn.fina 20250630       re-fetch a key next refresh
"""
import argparse
import sys

import pandas as pd

from . import schema
from .recorders import Recorder
from .store import Store


def _report(event):
    kind = event.get("event")
    if kind == "plan":
        print(f"{event['table']}: {event['keys']} keys via {'/'.join(event['sources'])}", flush=True)
    elif kind == "key" and (event["i"] % 20 == 0 or event["i"] == event["n"]):
        print(f"  {event['i']}/{event['n']} {event['key']} {event['rows']} rows ({event['source']})", flush=True)
    elif kind == "thin":
        print(f"  thin {event['key']}: {event['rows']} rows vs {event['stored']} already stored; kept the stored day", flush=True)
    elif kind == "partial":
        print(f"  partial {event['key']}: {event['rows']} rows vs a normal {event['typical']:.0f}; kept, fetched again next run", flush=True)
    elif kind == "fail":
        print(f"  FAIL {event['key']}: {event['error']}", file=sys.stderr, flush=True)
    elif kind == "end":
        print(f"{event['table']}: {event['done']} keys, {event['rows']} rows, {len(event['failed'])} failed", flush=True)


SETTINGS = {  # what .env may hold, and why
    "TUSHARE_MIRROR_TOKEN": "Tushare reseller mirror (tushare protocol)", "TUSHARE_MIRROR_URL": "its URL",
    "DATAHUB_API_KEY": "Tushare reseller REST front (pages)", "DATAHUB_BASE": "its base URL",
    "FTSHARE_API_KEY": "FTShare MCP gateway (cn.insider)", "ALPHAVANTAGE_API_KEY": "Alpha Vantage (us.earnings_av)",
    "QUANTDB_CONTACT": "an email for the SEC EDGAR User-Agent", "QLIB_CN": "Qlib provider for A-shares (default ~/.qlib/qlib_data/cn_data)",
    "QLIB_US": "Qlib provider for US (default ~/.qlib/qlib_data/us_ndx)",
}


def init(store, secrets=None) -> str:
    """Create the store folder and its .env: existing values are kept, known keys are copied from ``secrets``,
    the rest are written as commented placeholders."""
    from pathlib import Path

    env_path = store.root / ".env"
    current = {}
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1); current[k.strip()] = v.strip()
    copied = []
    if secrets:
        for line in Path(secrets).expanduser().read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() in SETTINGS and v.strip() and not current.get(k.strip()):
                    current[k.strip()] = v.strip(); copied.append(k.strip())
    lines = ["# quantdb settings and secrets; this file is never committed anywhere."]
    for key, why in SETTINGS.items():
        lines.append(f"# {why}")
        lines.append(f"{key}={current[key]}" if current.get(key) else f"# {key}=")
    env_path.write_text("\n".join(lines) + "\n")
    try:
        env_path.chmod(0o600); store.root.chmod(0o700)
    except OSError:
        pass
    set_keys = [k for k in SETTINGS if current.get(k)]
    return f"{store.root}: .env with {len(set_keys)} settings set ({', '.join(set_keys) or 'none'}); copied {', '.join(copied) or 'nothing'}"


def main(argv=None):
    p = argparse.ArgumentParser(prog="quantdb", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home", help="store root (default $QUANTDB_HOME or ~/.quantdb)")
    sub = p.add_subparsers(dest="cmd", required=True)
    ini = sub.add_parser("init"); ini.add_argument("--secrets", help="an existing .env to copy the known keys from")
    sub.add_parser("status")
    sub.add_parser("tables")
    r = sub.add_parser("refresh"); r.add_argument("table", nargs="?"); r.add_argument("--namespace"); r.add_argument("--start"); r.add_argument("--end")
    r.add_argument("--limit", type=int); r.add_argument("--source", action="append"); r.add_argument("--symbols", help="comma-separated")
    r.add_argument("--workers", type=int, default=1, help="concurrent fetches (slow gateways)")
    q = sub.add_parser("sql"); q.add_argument("query")
    i = sub.add_parser("import-legacy"); i.add_argument("table"); i.add_argument("paths", nargs="+"); i.add_argument("--source", default="legacy")
    i.add_argument("--date-col", default="date"); i.add_argument("--symbol-col", default="symbol"); i.add_argument("--done", help="mark these keys done: 'from-dates' or comma list")
    st = sub.add_parser("import-studio", help="load RD-Agent Studio's extra/ cache (tushare + baostock)"); st.add_argument("root")
    bf = sub.add_parser("backfill-prices", help="first load of cn.daily / cn.adj_factor per instrument through the REST server"); bf.add_argument("--start", default="2015-01-01"); bf.add_argument("--workers", type=int, default=4)
    ex = sub.add_parser("export-qlib"); ex.add_argument("dir"); ex.add_argument("--start", default="2015-01-01")
    f = sub.add_parser("forget"); f.add_argument("table"); f.add_argument("keys", nargs="+")
    a = p.parse_args(argv)
    store = Store(a.home)
    pd.set_option("display.width", 200); pd.set_option("display.max_columns", 30); pd.set_option("display.max_rows", 200)

    if a.cmd == "init":
        print(init(store, a.secrets))
    elif a.cmd == "status":
        print(store.status().to_string(index=False) if store.tables() else f"empty store at {store.root}")
    elif a.cmd == "tables":
        rows = [{"table": t.name, "key": t.key, "sources": "/".join(t.sources), "description": t.description} for t in schema.TABLES.values()]
        print(pd.DataFrame(rows).to_string(index=False))
    elif a.cmd == "refresh":
        rec = Recorder(store, report=_report)
        kw = {"start": a.start, "end": a.end, "limit": a.limit, "sources": a.source, "symbols": a.symbols.split(",") if a.symbols else None, "workers": a.workers}
        if a.table:
            rec.refresh(a.table, **kw)
        else:
            rec.refresh_all(namespace=a.namespace, **kw)
    elif a.cmd == "sql":
        print(store.sql(a.query).to_string(index=False))
    elif a.cmd == "import-legacy":
        from .legacy import import_files

        record = import_files(store, a.table, a.paths, date_col=a.date_col, symbol_col=a.symbol_col, source=a.source, done=a.done)
        print(f"{a.table}: {record['rows']} rows, {record['symbols']} symbols, {record['start']}..{record['end']}")
    elif a.cmd == "import-studio":
        from .legacy import import_studio_baostock, import_studio_tushare
        from pathlib import Path

        root = Path(a.root).expanduser()
        import_studio_tushare(store, root / "tushare")
        if (root / "baostock").is_dir():
            import_studio_baostock(store, root / "baostock")
    elif a.cmd == "backfill-prices":
        from .legacy import backfill_prices_by_symbol

        out = backfill_prices_by_symbol(store, start=a.start, workers=a.workers)
        print(f"{out['names']} names, {len(out['failed'])} failed" + (f": {out['failed'][:5]}" if out["failed"] else ""))
    elif a.cmd == "export-qlib":
        from .export.qlib import export_qlib

        print(export_qlib(store, a.dir, start=a.start, report=lambda m: print(m, flush=True)))
    elif a.cmd == "forget":
        store.forget(a.table, a.keys)
        print("ok")


if __name__ == "__main__":
    main()
