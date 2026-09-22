"""quantdb: one local store for the market data every research program here needs, so nobody fetches twice.

    import quantdb
    db = quantdb.open()                       # ~/.quantdb or $QUANTDB_HOME
    db.read("cn.moneyflow", "2025-01-01", "2025-03-31", columns=["net_mf_amount"])
    db.wide("cn.basic", "turnover_rate_f")    # date × symbol
    db.sql("select date, count(*) n from cn.toplist group by 1 order by 1 desc limit 5")
    quantdb.refresh("cn.moneyflow")           # fetch the trading days not yet stored
"""
from . import schema
from .recorders import Recorder, refresh
from .store import Store

__all__ = ["open", "refresh", "Recorder", "Store", "schema", "tables"]


def open(root=None) -> Store:
    return Store(root)


def tables():
    return schema.TABLES
