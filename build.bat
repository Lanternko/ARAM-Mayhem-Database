@echo off
chcp 65001 >nul
echo ================================================
echo   Build ARAM-collector.exe (new collector core)
echo ================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [error] Python not found.
    pause
    exit /b 1
)

echo [1/3] Install build dependencies...
pip install pyinstaller -q
python -m pip install -e . -q
if errorlevel 1 (
    echo [error] Failed to install dependencies.
    pause
    exit /b 1
)

echo [2/3] Build exe...
pyinstaller ARAM-collector.spec --noconfirm
if errorlevel 1 (
    echo [error] Build failed.
    pause
    exit /b 1
)

echo [3/3] Done
echo.
echo   Output: dist\ARAM-collector.exe
echo.
echo   Help:   dist\ARAM-collector.exe --help
echo   Status: dist\ARAM-collector.exe status
echo   Crawl:  dist\ARAM-collector.exe snowball-workers --workers 4 --target-games 50000 --max-players 50000 --games-per-player 4 --manual-seed-pending-cap 40
echo.
pause
