@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "REPO_DIR=%~dp0"
pushd "%REPO_DIR%"

REM Find git executable
set "GIT_EXE="
if exist "%ProgramFiles%\Git\bin\git.exe" set "GIT_EXE=%ProgramFiles%\Git\bin\git.exe"
if not defined GIT_EXE if exist "%ProgramFiles(x86)%\Git\bin\git.exe" set "GIT_EXE=%ProgramFiles(x86)%\Git\bin\git.exe"
if not defined GIT_EXE (
    echo Git executable not found.
    popd
    exit /b 1
)

REM Build commit message from all arguments
if "%~1"=="" (
    for /f "tokens=1-4 delims=/:. " %%a in ("%date% %time%") do (
        set "COMMIT_MSG=Update plugin files %%d-%%b-%%c_%%a%%~e"
    )
) else (
    set "COMMIT_MSG=%~1"
    shift
    :loop
    if not "%~1"=="" (
        set "COMMIT_MSG=!COMMIT_MSG! %~1"
        shift
        goto loop
    )
)

REM Stage, commit and push
"%GIT_EXE%" add --all .
"%GIT_EXE%" diff --cached --quiet
if errorlevel 1 (
    "%GIT_EXE%" commit -m "%COMMIT_MSG%"
    "%GIT_EXE%" push origin main
) else (
    echo No changes to commit.
)

popd
exit /b 0