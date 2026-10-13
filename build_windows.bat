@echo off
chcp 65001 > nul
echo ========================================================
echo   VoiceHUD - PackPoint Windows 11 Build Script
echo ========================================================
echo.

:: 1. Check Python
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found in PATH!
    echo Please install Python 3.10+ and make sure 'Add python.exe to PATH' is checked.
    pause
    exit /b 1
)

echo [*] Installing required dependencies...
python -m pip install --upgrade pip
python -m pip install pyqt5 pyinstaller requests

echo.
echo [*] Building standalone executable via PyInstaller...
python -m PyInstaller --clean -y VoiceHUD.spec

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ========================================================
    echo   BUILD SUCCESSFUL!
    echo   File generated at: dist\VoiceHUD.exe
    echo ========================================================
) else (
    echo.
    echo [ERROR] Build failed. Please check the logs above.
)

pause
