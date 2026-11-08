from __future__ import annotations

from datetime import date, time
from math import exp
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(page_title="NIFTY Options Backtester", layout="wide")
st.title("NIFTY Options Backtester")
st.caption("Historical-data research tool only; not financial advice or a live trading system.")


def clean_columns(frame: pd.DataFrame, needed: set[str], label: str) -> pd.DataFrame:
    frame = frame.copy()
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    missing = needed - set(frame.columns)
    if missing:
        raise ValueError(f"{label} needs these columns: {', '.join(sorted(missing))}")
    return frame


def timestamps(values: pd.Series) -> pd.Series:
    result = pd.to_datetime(values, errors="coerce")
    if result.isna().any():
        raise ValueError("A timestamp could not be read. Use YYYY-MM-DD HH:MM:SS format.")
    if result.dt.tz is None:
        return result.dt.tz_localize(IST)
    return result.dt.tz_convert(IST)


def alma(series: pd.Series, length: int, offset: float, sigma: float) -> pd.Series:
    center = offset * (length - 1)
    scale = length / sigma
    weights = [exp(-((i - center) ** 2) / (2 * scale**2)) for i in range(length)]
    weight_sum = sum(weights)
    weights = [w / weight_sum for w in weights]
    return series.rolling(length, min_periods=length).apply(
        lambda values: sum(value * weight for value, weight in zip(values, weights)), raw=True
    )


def read_nifty(file: Any, bb_length: int, multiplier: float, alma_length: int,
               alma_offset: float, alma_sigma: float) -> pd.DataFrame:
    data = clean_columns(pd.read_csv(file), {"timestamp", "open", "high", "low", "close"}, "NIFTY CSV")
    data["timestamp"] = timestamps(data["timestamp"])
    for col in ("open", "high", "low", "close"):
        data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.dropna(subset=["timestamp", "open", "high", "low", "close"])
    data = data.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    if data.empty:
        raise ValueError("No valid NIFTY candle rows found.")
    basis = data["close"].rolling(bb_length, min_periods=bb_length).mean()
    deviation = data["close"].rolling(bb_length, min_periods=bb_length).std(ddof=0) * multiplier
    data["upper"] = basis + deviation
    data["lower"] = basis - deviation
    data["alma"] = alma(data["close"], alma_length, alma_offset, alma_sigma)
    return data


def read_options(file: Any) -> pd.DataFrame:
    quotes = clean_columns(pd.read_csv(file), {"timestamp", "expiry", "option_type", "strike", "close"}, "Option CSV")
    quotes["timestamp"] = timestamps(quotes["timestamp"])
    quotes["expiry"] = pd.to_datetime(quotes["expiry"], errors="coerce").dt.date
    quotes["option_type"] = quotes["option_type"].astype(str).str.strip().str.upper().replace(
        {"C": "CALL", "CE": "CALL", "P": "PUT", "PE": "PUT"}
    )
    quotes["strike"] = pd.to_numeric(quotes["strike"], errors="coerce")
    quotes["close"] = pd.to_numeric(quotes["close"], errors="coerce")
    quotes = quotes.dropna(subset=["timestamp", "expiry", "strike", "close"])
    quotes = quotes[quotes["option_type"].isin(["CALL", "PUT"])].sort_values("timestamp")
    if quotes.empty:
        raise ValueError("No valid CALL/PUT option quotes found.")
    return quotes.reset_index(drop=True)


def get_price(quotes: pd.DataFrame, expiry: date, option_type: str, strike: int,
              at_time: pd.Timestamp) -> float | None:
    matches = quotes[(quotes.expiry == expiry) & (quotes.option_type == option_type) &
                     (quotes.strike == strike) & (quotes.timestamp <= at_time)]
    return None if matches.empty else float(matches.iloc[-1].close)


def signals(data: pd.DataFrame, start: time, max_size: float, size_type: str) -> pd.DataFrame:
    data = data.copy()
    data["green"] = data.close > data.open
    data["red"] = data.close < data.open
    data["trade_time"] = data.timestamp.dt.hour * 60 + data.timestamp.dt.minute >= start.hour * 60 + start.minute
    data["candle_size"] = data.high - data.low if size_type == "Range (high-low)" else (data.close - data.open).abs()
    data["call_signal"] = False
    data["put_signal"] = False
    green_count = red_count = 0
    current_day = None
    for idx, row in data.iterrows():
        day = row.timestamp.date()
        if day != current_day:
            green_count = red_count = 0
            current_day = day
        if not row.trade_time or pd.isna(row.upper) or pd.isna(row.lower) or pd.isna(row.alma):
            continue
        if row.green and row.close > row.upper:
            green_count += 1
        if row.red and row.close < row.lower:
            red_count += 1
        # NIFTY BUY -> buy CALL. NIFTY SELL -> buy PUT. No options are sold.
        call = row.red and row.close < row.upper and green_count > 0 and row.close > row.alma
        put = row.green and row.close > row.lower and red_count > 0 and row.close < row.alma
        if row.candle_size <= max_size and call:
            data.at[idx, "call_signal"] = True
            green_count = 0
        if row.candle_size <= max_size and put:
            data.at[idx, "put_signal"] = True
            red_count = 0
    return data


def backtest(data: pd.DataFrame, quotes: pd.DataFrame, expiry: date, gap: int, itm: int,
             start: time, cutoff: time, quantity: int, costs: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    completed: list[dict[str, Any]] = []
    slots: dict[date, set[str]] = {}
    active: dict[str, Any] | None = None

    def close(bar: pd.Series, reason: str) -> None:
        nonlocal active
        if active is None:
            return
        price = get_price(quotes, expiry, active["option_type"], active["strike"], bar.timestamp)
        if price is not None:
            points = price - active["entry_option_price"]
            completed.append({**active, "exit_time": bar.timestamp, "exit_nifty": float(bar.close),
                              "exit_option_price": price, "exit_reason": reason,
                              "pnl_points": points, "pnl": points * quantity - costs})
        active = None

    for _, bar in data.iterrows():
        now = bar.timestamp
        today = now.date()
        if active and active["entry_time"].date() != today:
            close(bar, "End of day")
        if active:
            # NIFTY price/levels trigger exits; option premiums are used only for P/L.
            if active["option_type"] == "CALL":
                if bar.high >= bar.upper:
                    active["trail_active"] = True
                if active["trail_active"] and bar.low <= bar.upper:
                    close(bar, "NIFTY upper Bollinger trail")
            else:
                if bar.low <= bar.lower:
                    active["trail_active"] = True
                if active["trail_active"] and bar.high >= bar.lower:
                    close(bar, "NIFTY lower Bollinger trail")
        if active and now.time() >= cutoff:
            close(bar, "Daily cutoff")
        if active or not bar.trade_time:
            continue
        side = "CALL" if bar.call_signal else ("PUT" if bar.put_signal else None)
        if side is None:
            continue
        slot = "before 13:00" if now.time() < time(13, 0) else "13:00 or later"
        used = slots.setdefault(today, set())
        if slot in used:
            continue
        atm = int(round(float(bar.close) / gap) * gap)
        strike = atm - itm if side == "CALL" else atm + itm
        entry = get_price(quotes, expiry, side, strike, now)
        if entry is None:
            continue
        used.add(slot)
        active = {"entry_time": now, "signal": f"NIFTY {'BUY' if side == 'CALL' else 'SELL'} → {side} BUY",
                  "option_type": side, "expiry": expiry, "strike": strike,
                  "entry_nifty": float(bar.close), "entry_option_price": entry, "trail_active": False}
    if active and not data.empty:
        close(data.iloc[-1], "End of data")
    trades = pd.DataFrame(completed)
    if trades.empty:
        return trades, pd.DataFrame(columns=["date", "trades", "daily_pnl", "daily_points"])
    trades["date"] = pd.to_datetime(trades.entry_time).dt.date
    daily = trades.groupby("date", as_index=False).agg(
        trades=("pnl", "count"), daily_pnl=("pnl", "sum"), daily_points=("pnl_points", "sum")
    )
    return trades, daily


with st.sidebar:
    st.header("Settings")
    expiry = st.date_input("Weekly option expiry", value=date.today())
    bb_length = st.number_input("Bollinger length", 2, 200, 20)
    bb_mult = st.number_input("Bollinger multiplier", 0.1, 10.0, 2.0, 0.1)
    alma_length = st.number_input("ALMA length", 1, 200, 65)
    alma_offset = st.number_input("ALMA offset", 0.0, 1.0, 0.85, 0.01)
    alma_sigma = st.number_input("ALMA sigma", 0.1, 20.0, 6.0, 0.1)
    gap = st.number_input("NIFTY strike gap", 1, 1000, 100, 50)
    itm = st.number_input("ITM distance", 1, 1000, 100, 50)
    start = st.time_input("Start time (IST)", time(10, 0))
    cutoff = st.time_input("Close by (IST)", time(15, 20))
    max_size = st.number_input("Maximum signal candle size", 0.0, 10000.0, 25.0, 1.0)
    size_type = st.selectbox("Candle size measure", ["Body (open-close)", "Range (high-low)"])
    quantity = st.number_input("Quantity", 1, 100000, 1)
    costs = st.number_input("Total costs per trade", 0.0, 100000.0, 0.0, 1.0)

st.write("Choose the expiry and upload NIFTY candles plus historical option prices below.")
col1, col2 = st.columns(2)
with col1:
    nifty_file = st.file_uploader("NIFTY candles CSV", type="csv")
with col2:
    option_file = st.file_uploader("Option prices CSV", type="csv")

with st.expander("CSV file format"):
    st.markdown("""
NIFTY CSV columns: `timestamp,open,high,low,close`.

Option CSV columns: `timestamp,expiry,option_type,strike,close`. Option type accepts CALL/PUT or CE/PE. Dates should look like `2026-11-12`; timestamps should match NIFTY data. Naive times are treated as India time.

Calls use ATM minus the ITM distance; puts use ATM plus it. Default is 100 points. Maximum one trade before 1 PM and one from 1 PM onward each day. The NIFTY Bollinger level, not option price, determines the band exit. Upload quote data through the exit time. Historical results are not a guarantee of future performance.
""")

if nifty_file and option_file:
    try:
        nifty = read_nifty(nifty_file, int(bb_length), float(bb_mult), int(alma_length), float(alma_offset), float(alma_sigma))
        option_quotes = read_options(option_file)
        nifty = signals(nifty, start, float(max_size), size_type)
        trades, daily = backtest(nifty, option_quotes, expiry, int(gap), int(itm), start, cutoff, int(quantity), float(costs))
        st.subheader("Backtest results")
        if trades.empty:
            st.warning("No completed trades found. Check the selected expiry, quotes, and date/time coverage.")
        else:
            a, b, c = st.columns(3)
            a.metric("Net P/L", f"{trades.pnl.sum():,.2f}")
            b.metric("Completed trades", str(len(trades)))
            c.metric("Winning trades", f"{int((trades.pnl > 0).sum())} / {len(trades)}")
            st.subheader("Daily P/L")
            st.dataframe(daily, use_container_width=True, hide_index=True)
            st.bar_chart(daily.set_index("date")["daily_pnl"])
            st.subheader("Trades")
            columns = ["entry_time", "signal", "expiry", "strike", "entry_nifty", "entry_option_price",
                       "exit_time", "exit_nifty", "exit_option_price", "exit_reason", "pnl_points", "pnl"]
            st.dataframe(trades[columns], use_container_width=True, hide_index=True)
            st.download_button("Download trades CSV", trades.to_csv(index=False), "trades.csv", "text/csv")
            st.download_button("Download daily P/L CSV", daily.to_csv(index=False), "daily_pnl.csv", "text/csv")
    except Exception as error:
        st.error(str(error))
