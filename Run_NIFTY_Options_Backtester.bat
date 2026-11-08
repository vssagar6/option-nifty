@echo off
setlocal
cd /d "%~dp0"
set "APP_DIR=%~dp0nifty_options_app"
set "VENV=%APP_DIR%\.venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo First-time setup: installing app requirements. Please wait...
  py -m venv "%VENV%"
  if errorlevel 1 goto python_error
)
"%VENV%\Scripts\python.exe" -m pip install -r "%APP_DIR%\requirements.txt"
if errorlevel 1 goto install_error
"%VENV%\Scripts\python.exe" -m streamlit run "%APP_DIR%\app.py"
if errorlevel 1 goto run_error
goto end
:python_error
echo Could not create the Python environment. Install Python from https://www.python.org/downloads/windows/ and check "Add Python to PATH".
pause
goto end
:install_error
echo Could not install app requirements. Check your internet connection and try again.
pause
goto end
:run_error
echo The app did not start. Check the error shown above.
pause
:end
endlocal
