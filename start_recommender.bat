@echo off
REM ============================================================
REM  ARAM Recommender — launch from SOURCE (always latest code)
REM
REM  Use this instead of dist\ARAMRecommender.exe.
REM  The .exe is a frozen code snapshot and only updates when you
REM  manually run scripts\build_recommender_exe.py.  Running from
REM  source means every code change is live on the next launch —
REM  no rebuild ever.
REM
REM  pythonw.exe = the GUI Python with no console window, so this
REM  feels exactly like double-clicking the old .exe.
REM  Double-click this file, or pin the desktop shortcut to taskbar.
REM  Pass extra flags through, e.g.  start_recommender.bat --fake
REM ============================================================
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "scripts\recommend_gui.py" %*
