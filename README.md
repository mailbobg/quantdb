# quantdb

One local store for every dataset the research programs use, so no program fetches its own copy again.

```
~/.quantdb/                      (or $QUANTDB_HOME)
  tables/cn.moneyflow.parquet    one Parquet file per table, sorted by (date, symbol)
  meta/cn.moneyflow.json         source, coverage, fetch keys done, last refresh
  snapshots/cn.moneyflow/…       the previous versions of the table (last 5)
  .env                           secrets for the sources; never in a repository
```

Every table is a long frame: `date`, `symbol` (Qlib codes: `SZ000001`, `AAPL`, `113050.SH`), then its fields.
Reading is DuckDB over the Parquet files; nothing needs a server.

## Layers

| layer | module | job |
| --- | --- | --- |
| schema | `quantdb/schema.py` | the table registry: name, key kind, sources (primary first), field notes |
| store | `quantdb/store.py` | Parquet + DuckDB: `upsert`, `replace`, `read`, `wide`, `sql`, `status`, snapshots |
| sources | `quantdb/sources/` | one module per provider, each `fetch(table, key) -> DataFrame` |
| recorders | `quantdb/recorders.py` | plan the missing keys per key kind, fetch from the first source that answers, upsert |
| views | `quantdb/views/` | research-ready derivations (point-in-time event grids) |
| legacy | `quantdb/legacy.py` | load caches other programs already fetched, so nothing is fetched twice |

Key kinds: `day` (one fetch per trading day, final once fetched), `period` (report periods, re-fetched while their
announcement season is open), `week` (calendar weeks, last N re-fetched), `symbol` (per instrument, resumed
from the last stored date), `snapshot` (the whole table, replaced).

## Use

```python
import quantdb
db = quantdb.open()
db.read("cn.moneyflow", start="2025-01-01", end="2025-03-31", columns=["net_mf_amount"])
db.wide("cn.basic", "turnover_rate_f", start="2025-01-01")            # date × symbol
db.sql("select date, count(*) n from cn.toplist group by 1 order by 1 desc limit 5")
quantdb.refresh("cn.moneyflow")                                       # only the trading days not stored yet

from quantdb.views import as_of, event_flags
grid = as_of(db.read("cn.holders"), calendar, "holder_num")          # visible from the next trading day
```

```
quantdb tables                          what is registered, with sources
quantdb status                          what is stored
quantdb refresh cn.moneyflow            incremental
quantdb refresh --namespace cn
quantdb refresh cn.baostock --symbols SZ000001,SH600000
quantdb sql "select count(*) from cn.margin"
quantdb import-studio <RD-Agent>/rd-agent/git_ignore_folder/traces/studio_data/extra
quantdb forget cn.fina 20250630         re-fetch a key next time
```

## Sources

| source | tables | needs |
| --- | --- | --- |
| tushare (two reseller servers) | cn.moneyflow margin chips basic toplist block fina forecast express holders unlock index_daily, cb.basic cb.daily, fut.cffex | `TUSHARE_MIRROR_TOKEN/URL`, `DATAHUB_API_KEY/BASE` |
| baostock | cn.baostock | nothing |
| eastmoney (akshare) | cb.premium | nothing |
| ftshare (MCP gateway) | cn.insider | `FTSHARE_API_KEY` |
| sec (EDGAR) | us.form4 us.eps_xbrl | `QUANTDB_CONTACT` (an email for the User-Agent) |
| arkfunds | us.ark_trades | nothing |
| appstore | alt.appstore_top | nothing |
| qlib (read-only bridge) | meta.instruments, calendars | local Qlib providers (`QLIB_CN`, `QLIB_US`) |
| openbb | us.daily us.earnings_calendar | `pip install quantdb[openbb]` in quantdb's own venv |

Adding a provider is one file under `quantdb/sources/` and a line in the registry; adding a table is one
`register(Table(...))` in `quantdb/schema.py`. A table may list several sources; the recorder takes the first one
that answers and records which one did.

## Environments

Readers need only `duckdb`, `pandas`, `pyarrow`: `pip install -e /path/to/quantdb` into any project venv.
Refresh jobs run from quantdb's own venv (`uv venv && uv pip install -e ".[cn,openbb,dev]"`), because some
providers pin web-framework versions that other projects cannot accept.

Prices for backtests stay in Qlib's binary provider; quantdb holds everything Qlib's dense per-day-per-symbol
format cannot: events with several rows a day, announcement dates, strings, reference tables, US and cross-asset
data. The Qlib bridge gives quantdb the calendar and instrument lists; Studio's exporter reads quantdb views
into the Qlib feature files.
