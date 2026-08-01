@echo off
setlocal
cd /d "%~dp0"

REM Builds the app as a folder (onedir) so it starts instantly instead of
REM re-extracting to %TEMP% on every launch like the old onefile build.
REM The EXE is placed directly in build\ with its _internal folder beside it.
REM The cache folder in build is intentionally preserved.
set "ASSETS_SRC=%CD%\assets"

if not exist "%ASSETS_SRC%" (
  echo Assets folder not found: "%ASSETS_SRC%"
  exit /b 1
)

python -c "import pymem, S3RecipeHandler" >nul 2>&1
if errorlevel 1 (
  echo Missing build dependencies. Install them first:
  echo   python -m pip install -r requirements-build.txt
  exit /b 1
)

set "STAGE=%CD%\build\_app_stage"

python -m PyInstaller --noconfirm --clean --onedir --noupx --windowed --name UTT --icon "%CD%\UTT.ico" ^
  --version-file "%CD%\version_info.txt" ^
  --distpath "%STAGE%" ^
  --workpath "%CD%\build\_pyinstaller" ^
  --specpath "%CD%\build\_pyinstaller" ^
  --hidden-import pymem ^
  --hidden-import S3RecipeHandler ^
  --hidden-import rx2_parser ^
  --add-data "%CD%\psg_list.json;." ^
  --add-data "%CD%\UTT.ico;." ^
  --add-data "%CD%\xbx.png;." ^
  --add-data "%CD%\ps3.png;." ^
  main.py

if errorlevel 1 exit /b %errorlevel%

move /Y "%STAGE%\UTT\UTT.exe" "%CD%\build\UTT.exe" >nul
if errorlevel 1 exit /b %errorlevel%
if exist "%CD%\build\_internal" rmdir /S /Q "%CD%\build\_internal"
move "%STAGE%\UTT\_internal" "%CD%\build\_internal" >nul
if errorlevel 1 exit /b %errorlevel%
rmdir /S /Q "%STAGE%"

if not exist "%CD%\build\assets" mkdir "%CD%\build\assets"
xcopy "%ASSETS_SRC%\*" "%CD%\build\assets\" /E /I /Y >nul
if errorlevel 1 exit /b %errorlevel%
copy /Y "%CD%\UTT.ico" "%CD%\build\UTT.ico" >nul
if errorlevel 1 exit /b %errorlevel%
copy /Y "%CD%\xbx.png" "%CD%\build\xbx.png" >nul
if errorlevel 1 exit /b %errorlevel%
copy /Y "%CD%\ps3.png" "%CD%\build\ps3.png" >nul
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
