"""The table registry: what every dataset is called, how it is keyed, where it comes from.

A table name is ``<namespace>.<name>``: ``cn`` A-shares, ``us`` US equities, ``cb`` convertible bonds, ``fut``
futures, ``alt`` alternative data, ``meta`` reference tables. Every table is a long frame with ``date`` (the
observation or event date), ``symbol`` (Qlib-style codes: SZ000001, AAPL) and its fields.

``key`` says how a refresh is chunked and what an incremental run has to re-fetch:

    day      one fetch per trading day; a day fetched once is final
    period   one fetch per report period (quarter end); periods keep receiving rows while their
             announcement season is open (``open_days`` after the period end), so those are re-fetched
    week     one fetch per calendar week of the range column; the last ``open_weeks`` weeks are re-fetched
    symbol   one fetch per instrument, extended from the last stored date
    snapshot the whole table in one fetch, replaced every refresh (reference tables, rankings)
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Table:
    name: str
    key: str
    sources: tuple            # source names, primary first
    description: str
    fields: dict = field(default_factory=dict)  # field -> meaning (documentation for humans and agents)
    date_field: str = "date"   # the source column the key is taken from, before normalisation
    open_days: int = 0         # period tables: still receiving rows this long after the period end
    open_weeks: int = 1        # week tables: the most recent weeks re-fetched every run
    ahead_days: int = 0        # week tables keyed by a future date (unlocks): also fetch this far ahead
    universe: str = ""         # symbol tables: the instrument list to iterate ("cn.all", "us.all")

    @property
    def namespace(self):
        return self.name.split(".", 1)[0]


TABLES = {}


def register(table: Table) -> Table:
    if table.name in TABLES:
        raise ValueError(f"table {table.name} registered twice")
    if table.key not in ("day", "period", "week", "symbol", "snapshot"):
        raise ValueError(f"{table.name}: unknown key kind {table.key!r}")
    TABLES[table.name] = table
    return table


def get(name: str) -> Table:
    if name not in TABLES:
        raise KeyError(f"unknown table {name!r}; known: {', '.join(sorted(TABLES))}")
    return TABLES[name]


# ---- A-shares: Tushare paid-tier tables (reseller servers) ----------------------------------------------------
register(Table("cn.moneyflow", "day", ("tushare",), "Order-size money flow per name per day (Tushare moneyflow).",
               {"buy_sm_amount": "small-order buy value, 万元", "sell_sm_amount": "small-order sell value", "buy_md_amount": "", "sell_md_amount": "",
                "buy_lg_amount": "large-order buy", "sell_lg_amount": "", "buy_elg_amount": "extra-large buy", "sell_elg_amount": "", "net_mf_amount": "net inflow, 万元"}))
register(Table("cn.margin", "day", ("tushare", "adata"), "Margin trading balances per name per day (Tushare margin_detail).",
               {"rzye": "financing balance, yuan", "rzmre": "financing purchases", "rzche": "financing repayments", "rqye": "securities-lending balance", "rqmcl": "shares sold short"}))
register(Table("cn.chips", "day", ("tushare",), "Chip distribution per name per day (Tushare cyq_perf).",
               {"winner_rate": "% of holders in profit", "cost_5pct": "", "cost_50pct": "", "cost_95pct": "", "weight_avg": "weighted average cost"}))
register(Table("cn.basic", "day", ("tushare",), "Daily valuation and size (Tushare daily_basic).",
               {"turnover_rate_f": "free-float turnover %", "volume_ratio": "", "pe_ttm": "", "pb": "", "dv_ttm": "trailing dividend yield %", "total_mv": "total market cap, 万元", "circ_mv": "float market cap, 万元", "free_share": "free-float shares, 万股"}))
register(Table("cn.toplist", "day", ("tushare", "adata"), "Dragon-tiger list appearances (Tushare top_list).",
               {"net_amount": "net buy of the listed seats, yuan", "reason": "listing reason"}))
register(Table("cn.block", "day", ("tushare", "adata"), "Block trades (Tushare block_trade).", {"price": "", "vol": "万股", "amount": "万元", "buyer": "", "seller": ""}))
register(Table("cn.fina", "period", ("tushare",), "Financial indicators per report period (Tushare fina_indicator_vip).",
               {"ann_date": "announcement date", "end_date": "period end", "roe": "%", "netprofit_yoy": "%", "or_yoy": "%", "grossprofit_margin": "%", "debt_to_assets": "%", "ocfps": "", "bps": ""},
               date_field="end_date", open_days=150))
register(Table("cn.forecast", "period", ("tushare",), "Earnings forecasts (Tushare forecast_vip).", {"ann_date": "", "type": "预增/预减/…", "p_change_min": "%", "p_change_max": "%"}, date_field="end_date", open_days=150))
register(Table("cn.express", "period", ("tushare",), "Earnings express reports (Tushare express_vip).", {"ann_date": "", "n_income": "net income", "yoy_net_profit": "last year's net income (not a rate)"}, date_field="end_date", open_days=150))
register(Table("cn.holders", "week", ("tushare",), "Shareholder counts by announcement week (Tushare stk_holdernumber).", {"ann_date": "", "end_date": "", "holder_num": ""}, date_field="ann_date", open_weeks=2))
register(Table("cn.unlock", "week", ("tushare",), "Share unlock schedule by unlock week (Tushare share_float); rows per holder.", {"ann_date": "", "float_date": "unlock date", "float_share": "shares", "float_ratio": "% of total shares"}, date_field="float_date", open_weeks=2, ahead_days=120))
register(Table("cn.baostock", "symbol", ("baostock",), "Daily turnover, valuation, float cap and ST flag per name (baostock).",
               {"turn": "turnover %", "peTTM": "", "pbMRQ": "", "psTTM": "", "pcfNcfTTM": "", "isST": "", "amount": "yuan"}, universe="cn.all"))
register(Table("cn.index_daily", "symbol", ("tushare",), "Index daily bars (Tushare index_daily).", {"open": "", "high": "", "low": "", "close": "", "vol": "", "amount": ""}, universe="cn.indices"))
register(Table("cn.insider", "week", ("ftshare",), "Officer/director share changes (FTShare 董监高持股变动, Eastmoney).",
               {"change_date": "", "notice_date": "", "change_direction": "增持/减持", "change_shares": "", "change_ratio": "% of total shares", "avg_price": "", "change_amount": "yuan", "shares_after": "", "executive_name": "the officer", "changer": "who traded (self or related party)", "relation": "本人/受控法人/…", "position": "", "change_reason": "竞价交易/大宗交易/询价转让/…"}, date_field="change_date", open_weeks=2))

# ---- Convertible bonds ---------------------------------------------------------------------------------------
register(Table("cb.basic", "snapshot", ("tushare",), "Convertible bond master (Tushare cb_basic).", {"stk_code": "underlying", "first_conv_price": "", "conv_price": "latest", "list_date": "", "delist_date": "", "maturity_date": ""}))
register(Table("cb.daily", "day", ("tushare",), "Convertible bond daily bars (Tushare cb_daily).", {"open": "", "high": "", "low": "", "close": "", "vol": "", "amount": ""}))
register(Table("cb.premium", "symbol", ("eastmoney",), "Daily conversion value and premium per bond (Eastmoney via akshare bond_zh_cov_value_analysis).",
               {"close": "", "bond_value": "纯债价值", "conv_value": "转股价值", "bond_premium": "纯债溢价率 %", "conv_premium": "转股溢价率 %"}, universe="cb.all"))

# ---- Futures -------------------------------------------------------------------------------------------------
register(Table("fut.cffex", "symbol", ("tushare",), "CFFEX futures contracts daily (Tushare fut_daily), all IF/IH/IC/IM/T contracts.",
               {"open": "", "high": "", "low": "", "close": "", "settle": "", "vol": "", "oi": "open interest"}, universe="fut.cffex_contracts"))

# ---- US ------------------------------------------------------------------------------------------------------
register(Table("us.form4", "snapshot", ("sec",), "SEC Form 4 open-market insider transactions (quarterly bulk data sets, 2017+).",
               {"filed": "filing date", "trans_date": "", "trans_code": "P buy / S sell", "shares": "", "price": "", "value": "", "relationship": "officer/director/10% owner"}))
register(Table("us.eps_xbrl", "symbol", ("sec",), "Quarterly diluted EPS from SEC XBRL companyconcept, dated by the first filing that reported the quarter.",
               {"end": "quarter end", "filed": "first filing date", "eps": "diluted EPS", "form": "10-Q/10-K"}, universe="us.all"))
register(Table("us.daily", "symbol", ("openbb",), "US equity daily bars (OpenBB → yfinance), unadjusted OHLCV.", {"open": "", "high": "", "low": "", "close": "", "volume": ""}, universe="us.all"))
register(Table("us.earnings_calendar", "week", ("openbb",), "Earnings calendar with consensus and actual EPS (OpenBB → Nasdaq), by report week; recent weeks re-fetched.",
               {"report_date": "", "eps_consensus": "", "eps_actual": "filled after the report", "eps_previous": "same quarter last year", "num_estimates": "", "reporting_time": "pre-market/after-hours", "market_cap": ""},
               date_field="report_date", open_weeks=4, ahead_days=21))
register(Table("us.ark_trades", "snapshot", ("arkfunds",), "ARK ETF daily trade disclosures (arkfunds.io).", {"fund": "", "direction": "Buy/Sell", "shares": "", "etf_percent": "% of fund"}))

# ---- Alternative ---------------------------------------------------------------------------------------------
register(Table("alt.appstore_top", "day", ("appstore",), "Apple App Store top-100 charts (free/paid, by country), one snapshot per day.",
               {"country": "", "chart": "top-free/top-paid", "rank": "", "app": "", "developer": "", "genre": ""}))

# ---- Reference -----------------------------------------------------------------------------------------------
register(Table("meta.instruments", "snapshot", ("qlib",), "Instrument master: code, name, market, listing spans (from the local Qlib providers).",
               {"market": "cn/us", "name": "", "start": "", "end": ""}))
