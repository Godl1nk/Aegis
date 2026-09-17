@echo off
rem Double-click deploy packer: zips this repo for the server.
rem The zip opens into a top-level folder (e.g. Aegis\...) so it can be
rem dropped straight onto the server. Skips .git/.venv (350MB dead
rem weight), data/.env (live server secrets -- never overwrite the
rem server's copies), and caches.
setlocal
cd /d "%~dp0"
for %%i in ("%~dp0.") do set ROOT=%%~nxi
set OUTDIR=%~dp0
set OUT=Aegis.zip
for /f %%i in ('git -C "%~dp0." rev-parse HEAD 2^>nul') do set COMMIT=%%i
if not defined COMMIT set COMMIT=
if exist "%OUTDIR%%OUT%" del "%OUTDIR%%OUT%"
rem Stamp the source commit into the tree so the server's update check
rem knows what's actually deployed (zip carries no .git). Written here,
rem packed, then removed so the worktree stays clean; .deploy-commit
rem is gitignored and can never be committed.
if defined COMMIT echo %COMMIT%> "%~dp0.deploy-commit"
set SEVENZIP=
if exist "C:\Program Files\7-Zip\7z.exe" set "SEVENZIP=C:\Program Files\7-Zip\7z.exe"
if exist "C:\Program Files (x86)\7-Zip\7z.exe" set "SEVENZIP=C:\Program Files (x86)\7-Zip\7z.exe"
cd /d "%~dp0.."
if defined SEVENZIP (
  "%SEVENZIP%" a -tzip "%OUTDIR%%OUT%" "%ROOT%" -xr!.git -xr!.venv -xr!data -xr!logs -xr!.env -xr!__pycache__ -xr!.pytest_cache -xr!*.pyc -xr!node_modules
) else (
  powershell -NoProfile -Command "Compress-Archive -Path '%ROOT%' -DestinationPath '%OUTDIR%%OUT%'"
)
del "%~dp0.deploy-commit" 2>nul
echo Done: %OUTDIR%%OUT%
pause
