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

REM Select branch to delete (default: canvas_map_items)
set "TARGET_BRANCH=%~1"
if "%TARGET_BRANCH%"=="" set "TARGET_BRANCH=canvas_map_items"

if /I "%TARGET_BRANCH%"=="main" (
    echo Refusing to delete protected branch: main
    popd
    exit /b 1
)
if /I "%TARGET_BRANCH%"=="master" (
    echo Refusing to delete protected branch: master
    popd
    exit /b 1
)

REM Validate repository and get current branch
"%GIT_EXE%" rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo This folder is not a Git repository.
    popd
    exit /b 1
)

for /f "delims=" %%b in ('"%GIT_EXE%" rev-parse --abbrev-ref HEAD') do set "CURRENT_BRANCH=%%b"
if not defined CURRENT_BRANCH (
    echo Could not detect active branch.
    popd
    exit /b 1
)
if /I "%CURRENT_BRANCH%"=="HEAD" (
    echo Detached HEAD state detected. Checkout a branch before running this script.
    popd
    exit /b 1
)

if /I "%CURRENT_BRANCH%"=="%TARGET_BRANCH%" (
    echo Target branch is currently checked out. Switching to main first...
    "%GIT_EXE%" checkout main
    if errorlevel 1 (
        echo Could not checkout main.
        popd
        exit /b 1
    )
)

REM Keep refs up to date
"%GIT_EXE%" fetch origin
if errorlevel 1 (
    echo Failed to fetch from origin.
    popd
    exit /b 1
)

REM Confirm branch exists locally and/or remotely
set "HAS_LOCAL=0"
set "HAS_REMOTE=0"
"%GIT_EXE%" show-ref --verify --quiet "refs/heads/%TARGET_BRANCH%"
if not errorlevel 1 set "HAS_LOCAL=1"
"%GIT_EXE%" show-ref --verify --quiet "refs/remotes/origin/%TARGET_BRANCH%"
if not errorlevel 1 set "HAS_REMOTE=1"

if "%HAS_LOCAL%%HAS_REMOTE%"=="00" (
    echo Branch %TARGET_BRANCH% does not exist locally or remotely.
    popd
    exit /b 1
)

REM Safety check: branch tip must already be merged into main
if "%HAS_LOCAL%"=="1" (
    "%GIT_EXE%" merge-base --is-ancestor "%TARGET_BRANCH%" main
    if errorlevel 1 (
        echo Local branch %TARGET_BRANCH% is not fully merged into main.
        echo Aborting deletion.
        popd
        exit /b 1
    )
)
if "%HAS_REMOTE%"=="1" (
    "%GIT_EXE%" merge-base --is-ancestor "origin/%TARGET_BRANCH%" main
    if errorlevel 1 (
        echo Remote branch origin/%TARGET_BRANCH% is not fully merged into main.
        echo Aborting deletion.
        popd
        exit /b 1
    )
)

echo Deleting merged branch: %TARGET_BRANCH%

if "%HAS_LOCAL%"=="1" (
    REM Safe to force delete because ancestor checks against main passed above
    "%GIT_EXE%" branch -D "%TARGET_BRANCH%"
    if errorlevel 1 (
        echo Failed to delete local branch %TARGET_BRANCH%.
        popd
        exit /b 1
    )
)

if "%HAS_REMOTE%"=="1" (
    "%GIT_EXE%" push origin --delete "%TARGET_BRANCH%"
    if errorlevel 1 (
        echo Failed to delete remote branch origin/%TARGET_BRANCH%.
        popd
        exit /b 1
    )
)

echo Done. Branch %TARGET_BRANCH% removed locally/remotely where present.
echo Current branch remains main.

popd
exit /b 0
