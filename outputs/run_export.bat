@echo off
setlocal
cd /d "%~dp0"
"%~dp0notion_calendar_app_v2.exe" --output-dir "%~dp0exports\latest" %*
if errorlevel 1 pause
endlocal
