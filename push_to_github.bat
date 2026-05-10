@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "REPO_DIR=%~dp0"
pushd "%REPO_DIR%"

set "GIT_EXE="
for /f "delims=" %%I in ('where git.exe 2^>nul') do (
    if not defined GIT_EXE set "GIT_EXE=%%I"
)

if not defined GIT_EXE (
    if exist "%ProgramFiles%\Git\bin\git.exe" set "GIT_EXE=%ProgramFiles%\Git\bin\git.exe"
)

if not defined GIT_EXE (
    if exist "%ProgramFiles(x86)%\Git\bin\git.exe" set "GIT_EXE=%ProgramFiles(x86)%\Git\bin\git.exe"
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
    set "COMMIT_MSG=%*"
)

"%GIT_EXE%" add -A
"%GIT_EXE%" diff --cached --quiet
if not errorlevel 1 (
    echo No staged changes to commit.
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