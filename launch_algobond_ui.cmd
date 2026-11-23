@echo off
setlocal
set "ALGOBOND_SOURCE=C:\Users\Ilya\PycharmProjects\kimkarus_ea_monitor_py"
set "ALGOBOND_PROJECT=%ALGOBOND_SOURCE%"
if exist "%~dp0config_demo.ini" set "ALGOBOND_PROJECT=%~dp0."

set "ALGOBOND_PYTHON=%ALGOBOND_PROJECT%\.venv\Scripts\python.exe"
if not exist "%ALGOBOND_PYTHON%" set "ALGOBOND_PYTHON=%ALGOBOND_SOURCE%\.venv\Scripts\python.exe"
if not exist "%~dp0libs\algobond_logging.py" if exist "%ALGOBOND_SOURCE%\libs\algobond_logging.py" (
  if defined PYTHONPATH (
    set "PYTHONPATH=%ALGOBOND_SOURCE%;%PYTHONPATH%"
  ) else (
    set "PYTHONPATH=%ALGOBOND_SOURCE%"
  )
)

if exist "%ALGOBOND_PYTHON%" (
  "%ALGOBOND_PYTHON%" -B -X faulthandler "%~dp0start_dashboard.py" --project "%ALGOBOND_PROJECT%" %*
) else (
  py -3 -B -X faulthandler "%~dp0start_dashboard.py" --project "%ALGOBOND_PROJECT%" %*
)
set "ALGOBOND_EXIT=%ERRORLEVEL%"
if not "%ALGOBOND_EXIT%"=="0" pause
exit /b %ALGOBOND_EXIT%
