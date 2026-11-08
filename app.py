from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(page_title="NIFTY Options Backtester", layout="wide")
st.title("NIFTY Options Backtester")
st.caption("Bollinger Band exhaustion signals with an ALMA filter and NIFTY-based trailing exits. For research and education only.")


def normalize_columns(frame: pd.DataFrame, required: set[str], label: str) -> pd.DataFrame:
    frame = frame.copy()
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{label} is missing required columns: {', '.join(sorted(missing))}")
    return frame


def parse_timestamps(values: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(values, errors="coerce")
    if parsed.isna().any():
        raise ValueError("Some timestamps could not be parsed. Use a consistent date/time format.")
    if parsed.dt.tz is None:
        return parsed.dt.tz_localize(IST, ambiguous="NaT", nonexistent="shift_forward")
    return parsed.dt.tz_convert(IST)


def alma(series: pd.Series, length: int, offset: float, sigma: float) -> pd.Series:
    m = offset * (length - 1)
    s = length / sigma
    weights = [float(__import__("math").exp(-((i - m) ** 2) / (2 * s * s))) for i in range(length)]
    total = sum(weights)
    weights = [weight / total for weight in weights]
    return series.rolling(length, min_periods=length).apply(
        lambda window: sum(value * weight for value, weight in zip(window, weights)), raw=True
    )


def prepare_nifty(upload: Any, bb_length: int, bb_mult: float, alma_length: int, alma_offset: float, alma_sigma: float) -> pd.DataFrame:
    data = pd.read_csv(upload)
    data = normalize_columns(data, {"timestamp", "open", "high", "low", "close"}, "NIFTY CSV")
    data["timestamp"] = parse_timestamps(data["timestamp"])
    for column in ("open", "high", "low", "close"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["timestamp", "open", "high", "low", "close"])
    data = data.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    if data.empty:
        raise ValueError("The NIFTY CSV contains no valid OHLC rows.")

    data["bb_basis"] = data["close"].rolling(bb_length, min_periods=bb_length).mean()
    deviation = data["close"].rolling(bb_length, min_periods=bb_length).std(ddof=0) * bb_mult
    data["bb_upper"] = data["bb_basis"] + deviation
    data["bb_lower"] = data["bb_basis"] - deviation
    data["alma"] = alma(data["close"], alma_length, alma_offset, alma_sigma)
    return data


def prepare_options(upload: Any) -> pd.DataFrame:
    quotes = pd.read_csv(upload)
    quotes = normalize_columns(quotes, {"timestamp", "expiry", "option_type", "strike", "close"}, "Option prices CSV")
    quotes["timestamp"] = parse_timestamps(quotes["timestamp"])
    quotes["expiry"] = pd.to_datetime(quotes["expiry"], errors="coerce").dt.date
    quotes["option_type"] = quotes["option_type"].astype(str).str.strip().str.upper()
    quotes["option_type"] = quotes["option_type"].replace({"C": "CALL", "CE": "CALL", "P": "PUT", "PE": "PUT"})
    quotes["strike"] = pd.to_numeric(quotes["strike"], errors="coerce")
    quotes["close"] = pd.to_numeric(quotes["close"], errors="coerce")
    quotes = quotes.dropna(subset=["timestamp", "expiry", "strike", "close"])
    quotes = quotes[quotes["option_type"].isin(["CALL", "PUT"])].sort_values("timestamp")
    if quotes.empty:
        raise ValueError("The option prices CSV contains no valid CALL/PUT quotes.")
    return quotes.reset_index(drop=True)


def add_signals(data: pd.DataFrame, start_time: time, max_candle_size: float, size_mode: str) -> pd.DataFrame:
    data = data.copy()
    data["green"] = data["close"] > data["open"]
    data["red"] = data["close"] < data["open"]
    data["minute_of_day"] = data["timestamp"].dt.hour * 60 + data["timestamp"].dt.minute
    data["trade_time"] = data["minute_of_day"] >= start_time.hour * 60 + start_time.minute
    data["candle_size"] = (data["high"] - data["low"]) if size_mode == "Range (high-low)" else (data["close"] - data["open"]).abs()
    data["size_ok"] = data["candle_size"] <= max_candle_size
    data["buy_call"] = False
    data["buy_put"] = False

    green_count = red_count = 0
    current_day = None
    for idx, row in data.iterrows():
        day = row["timestamp"].date()
        if day != current_day:
            green_count = red_count = 0
            current_day = day
        if not row["trade_time"] or pd.isna(row["bb_upper"]) or pd.isna(row["bb_lower"]) or pd.isna(row["alma"]):
            continue

        if row["green"] and row["close"] > row["bb_upper"]:
            green_count += 1
        if row["red"] and row["close"] < row["bb_lower"]:
            red_count += 1

        # The original NIFTY buy setup maps to a CALL buy; its sell setup maps to a PUT buy.
        call_setup = row["red"] and row["close"] < row["bb_upper"] and green_count > 0 and row["close"] > row["alma"]
        put_setup = row["green"] and row["close"] > row["bb_lower"] and red_count > 0 and row["close"] < row["alma"]
        if row["size_ok"] and call_setup:
            data.at[idx, "buy_call"] = True
            green_count = 0
        if row["size_ok"] and put_setup:
            data.at[idx, "buy_put"] = True
            red_count = 0
    return data


def last_quote(quotes: pd.DataFrame, expiry: date, option_type: str, strike: int, timestamp: pd.Timestamp) -> float | None:
    contract = quotes[(quotes["expiry"] == expiry) & (quotes["option_type"] == option_type) & (quotes["strike"] == strike)]
    contract = contract[contract["timestamp"] <= timestamp]
    if contract.empty:
        return None
    return float(contract.iloc[-1]["close"])


def run_backtest(data: pd.DataFrame, quotes: pd.DataFrame, expiry: date, strike_gap: int, itm_distance: int,
                 start_time: time, cutoff_time: time, trail_arm_pct: float, tighten_pct: float,
                 quantity: int, brokerage: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    day_slots: dict[date, set[str]] = {}
    active: dict[str, Any] | None = None

    def close_trade(bar: pd.Series, exit_reason: str) -> None:
        nonlocal active
        if active is None:
            return
        exit_price = last_quote(quotes, expiry, active["option_type"], active["strike"], bar["timestamp"])
        if exit_price is not None:
            gross = (exit_price - active["entry_option_price"]) * quantity
            pnl = gross - brokerage
            rows.append({**active, "exit_time": bar["timestamp"], "exit_nifty": float(bar["close"]),
                         "exit_option_price": exit_price, "exit_reason": exit_reason,
                         "gross_pnl": gross, "pnl": pnl, "pnl_points": exit_price - active["entry_option_price"]})
        active = None

    for _, bar in data.iterrows():
        now = bar["timestamp"]
        today = now.date()
        if active is not None and active["entry_time"].date() != today:
            close_trade(bar, "End of day")

        if active is not None:
            kind = active["option_type"]
            # Stops and trailing levels are always expressed in NIFTY points, not option premium.
            if kind == "CALL":
                if bar["high"] >= bar["bb_upper"]:
                    active["trail_active"] = True
                if active["trail_active"] and bar["low"] <= bar["bb_upper"]:
                    close_trade(bar, "NIFTY upper Bollinger trail")
            else:
                if bar["low"] <= bar["bb_lower"]:
                    active["trail_active"] = True
                if active["trail_active"] and bar["high"] >= bar["bb_lower"]:
                    close_trade(bar, "NIFTY lower Bollinger trail")

        if active is not None and now.time() >= cutoff_time:
            close_trade(bar, "Daily cutoff")

        if active is not None or not bar["trade_time"]:
            continue
        side = "CALL" if bar["buy_call"] else ("PUT" if bar["buy_put"] else None)
        if side is None:
            continue
        slot = "before_13" if now.time() < time(13, 0) else "after_13"
        slots = day_slots.setdefault(today, set())
        if slot in slots:
            continue

        atm = int(round(float(bar["close"]) / strike_gap) * strike_gap)
        strike = atm - itm_distance if side == "CALL" else atm + itm_distance
        entry_price = last_quote(quotes, expiry, side, strike, now)
        if entry_price is None:
            continue
        slots.add(slot)
        active = {"entry_time": now, "signal": f"NIFTY {'BUY' if side == 'CALL' else 'SELL'} → {side} BUY",
                  "option_type": side, "expiry": expiry, "strike": strike, "entry_nifty": float(bar["close"]),
                  "entry_option_price": entry_price, "trail_active": False,
                  "trail_arm_profit_pct": trail_arm_pct, "tighten_profit_pct": tighten_pct}

    if active is not None and not data.empty:
        close_trade(data.iloc[-1], "End of data")

    trades = pd.DataFrame(rows)
    if trades.empty:
        return trades, pd.DataFrame(columns=["date", "trades", "daily_pnl", "daily_points"])
    trades["date"] = pd.to_datetime(trades["entry_time"]).dt.date
    daily = trades.groupby("date", as_index=False).agg(trades=("pnl", "count"), daily_pnl=("pnl", "sum"), daily_points=("pnl_points", "sum"))
    return trades, daily


with st.sidebar:
    st.header("Strategy settings")
    expiry = st.date_input("Weekly option expiry", value=date.today())
    bb_length = st.number_input("BB length", min_value=2, max_value=200, value=20)
    bb_mult = st.number_input("BB multiplier", min_value=0.1, max_value=10.0, value=2.0, step=0.1)
    alma_length = st.number_input("ALMA length", min_value=1, max_value=200, value=9)
    alma_offset = st.number_input("ALMA offset", min_value=0.0, max_value=1.0, value=0.85, step=0.01)
    alma_sigma = st.number_input("ALMA sigma", min_value=0.1, max_value=20.0, value=6.0, step=0.1)
    st.divider()
    strike_gap = st.number_input("NIFTY strike gap", min_value=1, value=100, step=50)
    itm_distance = st.number_input("ITM distance from ATM (points)", min_value=1, value=100, step=50)
    start_time = st.time_input("Start trading after (IST)", value=time(10, 0))
    cutoff_time = st.time_input("Close positions by (IST)", value=time(15, 20))
    max_candle_size = st.number_input("Maximum signal candle size (points)", min_value=0.0, value=25.0, step=1.0)
    size_mode = st.selectbox("Measure candle size by", ["Body (open-close)", "Range (high-low)"])
    trail_arm_pct = st.number_input("Profit milestone 1 (%)", min_value=0.0, value=3.8, step=0.1)
    tighten_pct = st.number_input("Profit milestone 2 (%)", min_value=0.0, value=5.0, step=0.1)
    quantity = st.number_input("Option quantity", min_value=1, value=1, step=1)
    brokerage = st.number_input("Estimated total costs per trade", min_value=0.0, value=0.0, step=1.0)

st.markdown("### Upload historical data")
left, right = st.columns(2)
with left:
    nifty_file = st.file_uploader("NIFTY OHLC CSV", type=["csv"], key="nifty")
with right:
    option_file = st.file_uploader("Option prices CSV", type=["csv"], key="options")

st.info("Enter the weekly expiry above and upload option prices for that expiry. Strike selection uses 100-point ITM by default: CALL = ATM − 100, PUT = ATM + 100. NIFTY OHLC and option quote timestamps should use the same timezone or be in IST.")
with st.expander("CSV format and strategy assumptions"):
    st.markdown("""
**NIFTY CSV required columns:** `timestamp, open, high, low, close`.

**Option prices CSV required columns:** `timestamp, expiry, option_type, strike, close`. `option_type` may be `CALL`/`PUT` (or `CE`/`PE`); expiry should be a date such as `2026-11-12`. The app uses the latest option quote at or before each NIFTY bar. Include quotes through the expected exit bars.

The original NIFTY BUY setup buys a CALL; the original NIFTY SELL setup buys a PUT (it never sells options). ALMA replaces the visible/decision midline as a direction filter. Bollinger Bands remain calculated from the close SMA, but the SMA basis itself is not plotted. The app allows one entry before 13:00 and one at/after 13:00 per day. Calls trail against the NIFTY upper band after that band is reached; puts use the symmetric lower-band trail. The 3.8% and 5% fields are displayed as strategy milestones for review; the NIFTY-band exit is the actual stop and is not calculated from the option premium. These are explicit modeling assumptions; validate them against your intended rules before relying on results.
""")

if nifty_file and option_file:
    try:
        nifty = prepare_nifty(nifty_file, int(bb_length), float(bb_mult), int(alma_length), float(alma_offset), float(alma_sigma))
        options = prepare_options(option_file)
        nifty = add_signals(nifty, start_time, float(max_candle_size), size_mode)
        trades, daily = run_backtest(nifty, options, expiry, int(strike_gap), int(itm_distance), start_time, cutoff_time,
                                     float(trail_arm_pct), float(tighten_pct), int(quantity), float(brokerage))

        st.subheader("Backtest results")
        if trades.empty:
            st.warning("No completed trades with matching option quotes were found. Check the expiry, timestamps, strike coverage, and signal settings.")
        else:
            total_pnl = float(trades["pnl"].sum())
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Net P/L", f"{total_pnl:,.2f}")
            c2.metric("Completed trades", str(len(trades)))
            c3.metric("Winning trades", f"{int((trades['pnl'] > 0).sum())} / {len(trades)}")
            c4.metric("Option points", f"{trades['pnl_points'].sum():,.2f}")

            st.markdown("#### Daily P/L")
            st.dataframe(daily, use_container_width=True, hide_index=True)
            st.bar_chart(daily.set_index("date")["daily_pnl"])
            st.markdown("#### Trades")
            display_cols = ["entry_time", "signal", "expiry", "strike", "entry_nifty", "entry_option_price", "exit_time", "exit_nifty", "exit_option_price", "exit_reason", "pnl_points", "pnl"]
            st.dataframe(trades[display_cols], use_container_width=True, hide_index=True)
            st.download_button("Download trades CSV", trades.to_csv(index=False).encode("utf-8"), "nifty_option_backtest_trades.csv", "text/csv")
            st.download_button("Download daily P/L CSV", daily.to_csv(index=False).encode("utf-8"), "nifty_option_daily_pnl.csv", "text/csv")
    except Exception as exc:
        st.error(str(exc))
else:
    st.caption("Upload both CSV files to run the backtest.")
