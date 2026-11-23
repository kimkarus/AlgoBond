@echo off
setlocal
set "ALGOBOND_PROJECT=C:\Users\Ilya\PycharmProjects\kimkarus_ea_monitor_py"
if exist "%~dp0config_demo.ini" set "ALGOBOND_PROJECT=%~dp0."
if exist "%ALGOBOND_PROJECT%\.venv\Scripts\python.exe" (
  "%ALGOBOND_PROJECT%\.venv\Scripts\python.exe" -B -X faulthandler "%~dp0start_dashboard.py" --project "%ALGOBOND_PROJECT%"
) else (
  py -3 -B -X faulthandler "%~dp0start_dashboard.py" --project "%ALGOBOND_PROJECT%"
)
if errorlevel 1 pause
