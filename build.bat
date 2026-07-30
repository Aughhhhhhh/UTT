@echo off
setlocal
cd /d "%~dp0"

REM Keep the finished EXE and all PyInstaller temporary output under build.
REM The cache folder is intentionally not removed, so archive extraction persists.
set "ASSETS_SRC=%CD%\assets"

if not exist "%ASSETS_SRC%" (
  echo Assets folder not found: "%ASSETS_SRC%"
  exit /b 1
)

python -m PyInstaller --noconfirm --clean --onefile --noupx --windowed --name UTT --icon "%CD%\UTT.ico" ^
  --version-file "%CD%\version_info.txt" ^
  --distpath "%CD%\build" ^
  --workpath "%CD%\build\_pyinstaller" ^
  --specpath "%CD%\build\_pyinstaller" ^
  --add-data "%CD%\psg_list.json;." ^
  --add-data "%CD%\UTT.ico;." ^
  main.py

if errorlevel 1 exit /b %errorlevel%

if not exist "%CD%\build\assets" mkdir "%CD%\build\assets"
xcopy "%ASSETS_SRC%\*" "%CD%\build\assets\" /E /I /Y >nul
if errorlevel 1 exit /b %errorlevel%
copy /Y "%CD%\UTT.ico" "%CD%\build\UTT.ico" >nul
if errorlevel 1 exit /b %errorlevel%

if defined UTT_SIGN_CERT_SHA1 (
  where signtool >nul 2>&1
  if errorlevel 1 (
    echo UTT_SIGN_CERT_SHA1 is set, but signtool was not found.
    exit /b 1
  )
  if not defined UTT_TIMESTAMP_URL set "UTT_TIMESTAMP_URL=http://timestamp.digicert.com"
  signtool sign /sha1 "%UTT_SIGN_CERT_SHA1%" /fd SHA256 /td SHA256 /tr "%UTT_TIMESTAMP_URL%" "%CD%\build\UTT.exe"
  if errorlevel 1 exit /b %errorlevel%
)

echo.
echo Build complete: "%CD%\build\UTT.exe"
echo Runtime assets: "%CD%\build\assets"
if not defined UTT_SIGN_CERT_SHA1 echo Build is unsigned. Set UTT_SIGN_CERT_SHA1 to sign with a trusted certificate.
