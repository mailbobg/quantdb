"""Point-in-time helpers: turn announcement-dated event rows into (date × symbol) frames a backtest may see on
each trading day without look-ahead.

    as_of(events, calendar, value, lag=1)      last announced value per symbol, known by each trading day
    event_flags(events, calendar, window)      1 when an event happened within the past ``window`` trading days
    next_trading_day(dates, calendar)          the first trading day on or after each date (news after close
                                               becomes tradable the next session)
"""
import numpy as np
import pandas as pd


def next_trading_day(dates, calendar: pd.DatetimeIndex, shift=1) -> pd.Series:
    """For every date the first calendar day strictly after it (shift=1) or on/after it (shift=0)."""
    dates = pd.to_datetime(pd.Series(dates))
    idx = np.searchsorted(calendar.values, dates.values, side="right" if shift else "left")
    idx = np.clip(idx, 0, len(calendar) - 1)
    out = pd.Series(calendar.values[idx], index=dates.index)
    out[dates > calendar[-1]] = pd.NaT
    return out


def as_of(events: pd.DataFrame, calendar: pd.DatetimeIndex, value: str, announce="date", symbol="symbol", shift=1) -> pd.DataFrame:
    """A (date × symbol) frame holding, for each trading day, the latest ``value`` announced before it."""
    e = events[[announce, symbol, value]].dropna()
    e = e.assign(effective=next_trading_day(e[announce], calendar, shift)).dropna(subset=["effective"])
    e = e.sort_values([announce]).drop_duplicates(["effective", symbol], keep="last")
    wide = e.pivot(index="effective", columns=symbol, values=value).reindex(calendar)
    return wide.ffill()


def event_flags(events: pd.DataFrame, calendar: pd.DatetimeIndex, window: int, announce="date", symbol="symbol", shift=1) -> pd.DataFrame:
    """1.0 on the ``window`` trading days after each event (inclusive of the first tradable day), else 0.0."""
    e = events[[announce, symbol]].dropna()
    e = e.assign(effective=next_trading_day(e[announce], calendar, shift)).dropna(subset=["effective"])
    hits = e.groupby(["effective", symbol]).size().unstack(fill_value=0).reindex(calendar, fill_value=0)
    return (hits.rolling(window, min_periods=1).sum() > 0).astype(float)


def count_in_window(events: pd.DataFrame, calendar: pd.DatetimeIndex, window: int, announce="date", symbol="symbol", shift=1) -> pd.DataFrame:
    """Number of events per symbol in the trailing ``window`` trading days."""
    e = events[[announce, symbol]].dropna()
    e = e.assign(effective=next_trading_day(e[announce], calendar, shift)).dropna(subset=["effective"])
    hits = e.groupby(["effective", symbol]).size().unstack(fill_value=0).reindex(calendar, fill_value=0)
    return hits.rolling(window, min_periods=1).sum()
