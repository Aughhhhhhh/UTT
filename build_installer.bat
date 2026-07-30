@echo off
setlocal
cd /d "%~dp0"

call build.bat
if errorlevel 1 exit /b %errorlevel%

set "ISCC=%INNO_ISCC%"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 7\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"

if not defined ISCC (
  echo Inno Setup compiler not found.
  echo Install it from https://jrsoftware.org/isdl.php or set INNO_ISCC to ISCC.exe.
  exit /b 1
)

"%ISCC%" "%CD%\installer.iss"
if errorlevel 1 exit /b %errorlevel%

set "SETUP_EXE=%CD%\build\installer\UTT-Setup-1.1.1.exe"
if defined UTT_SIGN_CERT_SHA1 (
  where signtool >nul 2>&1
  if errorlevel 1 (
    echo UTT_SIGN_CERT_SHA1 is set, but signtool was not found.
    exit /b 1
  )
  if not defined UTT_TIMESTAMP_URL set "UTT_TIMESTAMP_URL=http://timestamp.digicert.com"
  signtool sign /sha1 "%UTT_SIGN_CERT_SHA1%" /fd SHA256 /td SHA256 /tr "%UTT_TIMESTAMP_URL%" "%SETUP_EXE%"
  if errorlevel 1 exit /b %errorlevel%
)

echo.
echo Installer complete: "%SETUP_EXE%"
if not defined UTT_SIGN_CERT_SHA1 echo Installer is unsigned. Set UTT_SIGN_CERT_SHA1 to sign it.
