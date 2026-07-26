@echo off
setlocal

cd /d "%~dp0"
if errorlevel 1 goto :repository_error

if /I not "%VIRTUAL_ENV%"=="%~dp0.venv" (
    if not exist "%~dp0.venv\Scripts\activate.bat" goto :venv_missing
    call "%~dp0.venv\Scripts\activate.bat"
    if errorlevel 1 goto :venv_error
)

call python tools\uat_preflight.py
if errorlevel 1 goto :preflight_error

echo Telegram UAT preflight passed. Starting the bot...
call python -m app.integrations.telegram.runner
if errorlevel 1 goto :runner_error

exit /b 0

:preflight_error
echo.
echo Telegram UAT preflight failed. The bot was not started.
pause
exit /b 1

:venv_missing
echo.
echo AURA virtual environment was not found. The bot was not started.
pause
exit /b 1

:venv_error
echo.
echo AURA virtual environment activation failed. The bot was not started.
pause
exit /b 1

:repository_error
echo.
echo AURA repository root could not be opened. The bot was not started.
pause
exit /b 1

:runner_error
echo.
echo The Telegram bot stopped with an error.
pause
exit /b 1
