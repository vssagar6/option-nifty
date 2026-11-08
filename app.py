from pathlib import Path

# Keep the original root-level entry point working while the app itself lives
# separately, so it cannot conflict with another app.py on the user's PC.
app_file = Path(__file__).parent / "nifty_options_app" / "app.py"
exec(compile(app_file.read_text(encoding="utf-8"), str(app_file), "exec"))
