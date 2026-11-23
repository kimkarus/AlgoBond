@echo off
setlocal
set "ALGOBOND_PYTHON=%~dp0.venv\Scripts\python.exe"
set "ALGOBOND_PYTHON_OPTIONS="
if exist "%ALGOBOND_PYTHON%" goto run

set "ALGOBOND_PYTHON=py"
set "ALGOBOND_PYTHON_OPTIONS=-3"
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto run

set "ALGOBOND_PYTHON=python"
set "ALGOBOND_PYTHON_OPTIONS="
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto run

echo Install Python 3.10 or newer and add it to PATH.
pause
exit /b 1

:run
"%ALGOBOND_PYTHON%" %ALGOBOND_PYTHON_OPTIONS% -B "%~dp0start_config_editor.py" %*
set "ALGOBOND_EXIT=%ERRORLEVEL%"
if not "%ALGOBOND_EXIT%"=="0" pause
exit /b %ALGOBOND_EXIT%
