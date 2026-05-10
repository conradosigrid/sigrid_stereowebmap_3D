@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "REPO_DIR=%~dp0"
pushd "%REPO_DIR%"

set "GIT_EXE="

if exist "%ProgramFiles%\Git\bin\git.exe" set "GIT_EXE=%ProgramFiles%\Git\bin\git.exe"

if not defined GIT_EXE (
    if exist "%ProgramFiles(x86)%\Git\bin\git.exe" set "GIT_EXE=%ProgramFiles(x86)%\Git\bin\git.exe"
)

if not defined GIT_EXE (
    for /f "delims=" %%I in ('where git.exe 2^>nul') do (
        if not defined GIT_EXE set "GIT_EXE=%%I"
    )
)

if not defined GIT_EXE (
    echo Git executable not found.
    popd
    exit /b 1
)

if "%~1"=="" (
    for /f "tokens=1-4 delims=/:. " %%a in ("%date% %time%") do (
        set "STAMP=%%d-%%b-%%c_%%a%%~e"
    )
    set "COMMIT_MSG=Update plugin files !STAMP!"
) else (
    set "COMMIT_MSG=%~1"
)

"%GIT_EXE%" add --all .
if errorlevel 1 (
    echo Git add failed.
    popd
    exit /b 1
)

set "HAS_STAGED="
for /f "delims=" %%S in ('"%GIT_EXE%" status --porcelain') do (
    set "LINE=%%S"
    if not "!LINE:~0,1!"==" " if not "!LINE:~0,1!"=="?" set "HAS_STAGED=1"
)

if not defined HAS_STAGED (
    echo No staged changes to commit. Make sure files are saved before running this script.
    "%GIT_EXE%" status --short
    popd
    exit /b 0
)

"%GIT_EXE%" commit -m "%COMMIT_MSG%"
if errorlevel 1 (
    echo Commit failed.
    popd
    exit /b 1
)

"%GIT_EXE%" push origin main
if errorlevel 1 (
    echo Push failed.
    popd
    exit /b 1
)

echo Done.
popd
exit /b 0