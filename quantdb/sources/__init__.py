"""Source registry: name -> class. Sources are constructed lazily with the shared Config."""
from .base import Config, Source

REGISTRY = {}


def _load():
    if REGISTRY:
        return
    from .appstore import AppStore
    from .arkfunds import ArkFunds
    from .baostock_src import Baostock
    from .eastmoney import Eastmoney
    from .ftshare import FTShare
    from .qlib_bridge import QlibBridge
    from .sec import Sec
    from .openbb_src import OpenBB
    from .tushare_reseller import TushareReseller

    for cls in (TushareReseller, Baostock, Eastmoney, Sec, AppStore, FTShare, ArkFunds, QlibBridge, OpenBB):
        REGISTRY[cls.name] = cls


def make(name: str, config: Config) -> Source:
    _load()
    if name not in REGISTRY:
        raise KeyError(f"unknown source {name!r}; known: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[name](config)
