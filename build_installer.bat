@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ISCC=%INNO_ISCC%"
if defined ISCC if not exist "%ISCC%" (
  echo INNO_ISCC points to a file that does not exist:
  echo "%ISCC%"
  goto :missing_inno
)

if not defined ISCC for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do if not defined ISCC set "ISCC=%%I"
if not defined ISCC if exist "%CD%\build\_tools\innosetup7\ISCC.exe" set "ISCC=%CD%\build\_tools\innosetup7\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 7\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%LocalAppData%\Programs\Inno Setup 7\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 7\ISCC.exe"
if not defined ISCC if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"

if not defined ISCC goto :missing_inno

echo Using Inno Setup compiler:
echo "%ISCC%"
echo.

call build.bat
if errorlevel 1 (
  echo.
  echo Application build failed, so the installer was not created.
  if not defined CI pause
  exit /b 1
)

"%ISCC%" "%CD%\installer.iss"
if errorlevel 1 (
  echo.
  echo Installer compilation failed.
  if not defined CI pause
  exit /b 1
)

set "APP_VERSION="
for /f "tokens=3" %%V in ('findstr /b /c:"#define AppVersion " "%CD%\installer.iss"') do set "APP_VERSION=%%~V"
if not defined APP_VERSION (
  echo Could not read AppVersion from installer.iss.
  if not defined CI pause
  exit /b 1
)

set "SETUP_EXE=%CD%\build\installer\UTT-Setup-%APP_VERSION%.exe"
if not exist "%SETUP_EXE%" (
  echo Expected installer was not created:
  echo "%SETUP_EXE%"
  if not defined CI pause
  exit /b 1
)

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
if not defined CI if not defined UTT_NO_OPEN start "" explorer.exe /select,"%SETUP_EXE%"
exit /b 0

:missing_inno
echo.
echo Inno Setup compiler was not found, so no installer can be created.
echo Install Inno Setup from:
echo https://jrsoftware.org/isdl.php
echo.
echo Then run build_installer.bat again. A custom compiler path can be set with:
echo set "INNO_ISCC=C:\path\to\ISCC.exe"
echo.
if not defined CI pause
exit /b 1
