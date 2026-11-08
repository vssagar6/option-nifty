# NIFTY Options Backtester (Windows)

## Start the app without replacing another `app.py`

This repository keeps the Streamlit app in its own folder: `nifty_options_app`. To start it on Windows:

1. Download this repository as a ZIP from GitHub and extract it, or clone it if you already use Git.
2. Install Python for Windows from https://www.python.org/downloads/windows/. During setup, tick **Add Python to PATH** (the launcher `py` is used by the start file).
3. Open the extracted `option-nifty` folder.
4. Double-click **`Run_NIFTY_Options_Backtester.bat`**. The first run creates a private Python environment inside `nifty_options_app` and installs the app packages. It will then open the app in your browser.
5. Keep the black command window open while using the app. To stop it, close that window or press Ctrl+C in it.

The starter uses `nifty_options_app\\app.py`, so it will not overwrite or use a different app.py on your computer. You do not need to type Python commands.

## Data files

NIFTY CSV columns: `timestamp,open,high,low,close`.

Option price CSV columns: `timestamp,expiry,option_type,strike,close`. `option_type` can be `CALL`/`PUT` or `CE`/`PE`. Select the same weekly expiry in the app. Example timestamps: `2026-11-03 10:00:00`.

Defaults include ALMA length 65; call strike = ATM minus 100; put strike = ATM plus 100; and start time 10:00 India time. The app allows at most one entry before 1 PM and one from 1 PM onward per day. Exit bands are evaluated using NIFTY prices, with option prices used for P/L. Backtest results depend on supplied historical data and are not financial advice.
