@echo off
chcp 65001 >nul
echo ================================================
echo   ARAM Mayhem Collector (Python helper)
echo ================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [error] Python not found.
    echo Install Python 3.11+ and make sure it is on PATH.
    pause
    exit /b 1
)

echo [1/3] Install dependencies...
python -m pip install -e . --quiet
if errorlevel 1 (
    echo [error] Dependency install failed.
    pause
    exit /b 1
)

echo [2/3] Run tuned Mayhem crawl...
echo.
python collect.py --platform TW2
if errorlevel 1 (
    echo.
    echo [error] Crawl failed. Make sure the League client is open and logged in.
    pause
    exit /b 1
)

echo.
echo [3/3] Done
echo.
echo Output: my_games.parquet
echo Repo:   https://github.com/Lanternko/ARAM-mayhem-collector
echo.
pause
