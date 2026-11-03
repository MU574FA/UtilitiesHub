@echo off
setlocal
cd /d "%~dp0"

title OmniDocs Studio

:: Check for virtual environment in standard folders (.venv or venv)
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
)

:: Run main.py with Python
where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
    python main.py %*
) else (
    py main.py %*
)

:: If an error occurred, pause so the error details remain visible
if %ERRORLEVEL% neq 0 (
    echo.
    echo Application exited with error code %ERRORLEVEL%.
    pause
)

endlocal

