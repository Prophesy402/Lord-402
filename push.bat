@echo off
cd /d "%~dp0"
set GIT="C:\Program Files\Git\cmd\git.exe"
%GIT% add -A
%GIT% commit -m "Update bot"
%GIT% push origin main
echo.
echo Done. Railway will redeploy in a minute or two. You can close this window.
pause
