@echo off
if "%~1"=="" exit /b 1
if "%~2"=="" exit /b 1
set path_1=%~1
set string=%~2
PsgCliTool.exe "%path_1%" "%string%.psg"
