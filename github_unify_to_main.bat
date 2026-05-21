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

REM Validate repository and detect current branch
"%GIT_EXE%" rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo This folder is not a Git repository.
    popd
    exit /b 1
)

for /f "delims=" %%b in ('"%GIT_EXE%" rev-parse --abbrev-ref HEAD') do set "SOURCE_BRANCH=%%b"
if not defined SOURCE_BRANCH (
    echo Could not detect active branch.
    popd
    exit /b 1
)
if /I "%SOURCE_BRANCH%"=="HEAD" (
    echo Detached HEAD state detected. Checkout a branch before running this script.
    popd
    exit /b 1
)

REM Require clean working tree to avoid mixing unrelated changes
set "STATUS_SIZE=0"
set "TMP_STATUS=%TEMP%\git_status_%RANDOM%_%RANDOM%.tmp"
"%GIT_EXE%" status --porcelain > "%TMP_STATUS%"
for %%A in ("%TMP_STATUS%") do set "STATUS_SIZE=%%~zA"
del "%TMP_STATUS%" >nul 2>&1
if not "%STATUS_SIZE%"=="0" (
    echo Working tree is not clean. Commit or stash your changes first.
    popd
    exit /b 1
)

echo Source branch: %SOURCE_BRANCH%
echo Target branch: main

REM Ensure remote refs are up to date
"%GIT_EXE%" fetch origin
if errorlevel 1 (
    echo Failed to fetch from origin.
    popd
    exit /b 1
)

REM Move to main and update it safely
"%GIT_EXE%" checkout main
if errorlevel 1 (
    echo Could not checkout main.
    popd
    exit /b 1
)

"%GIT_EXE%" pull --ff-only origin main
if errorlevel 1 (
    echo Could not fast-forward local main with origin/main.
    echo Resolve this manually, then run again.
    popd
    exit /b 1
)

REM If already on main, just push and finish
if /I "%SOURCE_BRANCH%"=="main" (
    echo Already on main. Nothing to merge.
    "%GIT_EXE%" push origin main
    popd
    exit /b %errorlevel%
)

REM Merge source branch into main
"%GIT_EXE%" merge "%SOURCE_BRANCH%"
if errorlevel 1 (
    echo Merge conflict or merge error while merging %SOURCE_BRANCH% into main.
    echo Aborting merge to keep repository clean.
    "%GIT_EXE%" merge --abort >nul 2>&1
    popd
    exit /b 1
)

REM Publish unified main branch
"%GIT_EXE%" push origin main
if errorlevel 1 (
    echo Merge completed locally but push to origin/main failed.
    popd
    exit /b 1
)

echo Done. Branches unified in origin/main and current branch is main.

popd
exit /b 0
