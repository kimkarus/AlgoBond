@echo off
setlocal
set "ALGOBOND_PROJECT=C:\Users\Ilya\PycharmProjects\kimkarus_ea_monitor_py"
if exist "%ALGOBOND_PROJECT%\.venv\Scripts\python.exe" (
  "%ALGOBOND_PROJECT%\.venv\Scripts\python.exe" -B "%~dp0start_config_editor.py" --project "%ALGOBOND_PROJECT%"
) else (
  py -3.10 -B "%~dp0start_config_editor.py" --project "%ALGOBOND_PROJECT%"
)
if errorlevel 1 pause
