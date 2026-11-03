@echo off
setlocal
cd /d "%~dp0"
if not exist "exports\latest" mkdir "exports\latest"
"%~dp0notion_calendar_app_v2.exe" --serve-dashboard --output-dir "%~dp0exports\latest"
endlocal
