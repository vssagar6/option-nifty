# NIFTY Options Backtester

A small Streamlit app for backtesting the supplied NIFTY Bollinger Band exhaustion setup with an ALMA direction filter. It buys options only: a NIFTY BUY signal buys a CALL and a NIFTY SELL signal buys a PUT.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
streamlit run app.py
```

## Input files

Choose the weekly expiry in the sidebar, then upload both CSV files.

### NIFTY candles

Required columns: `timestamp,open,high,low,close`.

### Option prices

Required columns: `timestamp,expiry,option_type,strike,close`. `option_type` accepts `CALL`/`PUT` or `CE`/`PE`. Dates should be compatible with the selected weekly expiry; timestamps should be consistent with NIFTY candles (naive timestamps are treated as Asia/Kolkata).

The app takes the latest quote at or before each signal/exit candle. Upload option quotes across all potential strikes and bars, including the selected expiry, so entry and exit prices can be found. Missing prices cause that entry or exit to be skipped/not completed.

## Implemented defaults and assumptions

- Bollinger Bands use a 20-bar SMA and 2 standard deviations; the SMA basis is hidden. ALMA (length 65, offset 0.85, sigma 6) is used as the directional midline filter.
- Calls use ATM minus 100 points; puts use ATM plus 100 points. ATM is rounded to the configured strike gap (100 by default).
- Trading starts at 10:00 IST. Only one entry before 13:00 and one at/after 13:00 are allowed each day. The signal-candle size filter defaults to a 25-point candle body.
- A call's NIFTY-based trail activates when NIFTY reaches the upper Bollinger Band and exits on a subsequent/overlapping bar that touches the upper band. Puts use the symmetric lower-band condition. Exit decisions use NIFTY prices, not option premium stop prices; option quotes supply the realized option exit premium.
- The 3.8% and 5% settings are recorded as profit milestones for review, not premium-based stop prices or automatic take-profit exits. Brokerage/costs are subtracted from P/L.
- Open positions are closed at the configured daily cutoff or at the end of the data. Results depend on input data and assumptions and are not investment advice.

Review and adjust the documented assumptions to match your exact trading rules before using results.

