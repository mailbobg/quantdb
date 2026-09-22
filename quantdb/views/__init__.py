"""Derived, research-ready frames built from the raw tables. Nothing here fetches; everything reads the store."""
from .point_in_time import as_of, event_flags, next_trading_day  # noqa: F401
