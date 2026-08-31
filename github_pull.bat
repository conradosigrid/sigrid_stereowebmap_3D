@echo off
setlocal EnableExtensions

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

REM Commit pending local changes before updating
set "HAS_CHANGES="
for /f "delims=" %%a in ('"%GIT_EXE%" status --porcelain') do (
    set "HAS_CHANGES=1"
)
if defined HAS_CHANGES (
    echo Local changes detected. Creating automatic commit...
    "%GIT_EXE%" add --all .
    "%GIT_EXE%" commit -m "Automatic commit before pull"
    if errorlevel 1 (
        echo Automatic commit failed. Resolve the reported issue and try again.
        popd
        exit /b 1
    )
    echo Local changes were committed automatically.
)

REM Detect the active branch
for /f "delims=" %%b in ('"%GIT_EXE%" rev-parse --abbrev-ref HEAD') do set "CURRENT_BRANCH=%%b"
if not defined CURRENT_BRANCH (
    echo Could not detect active branch.
    popd
    exit /b 1
)
if /I "%CURRENT_BRANCH%"=="HEAD" (
    echo Detached HEAD state detected. Checkout a branch before updating.
    popd
    exit /b 1
)

REM Update only when a fast-forward is possible
"%GIT_EXE%" pull --ff-only origin "%CURRENT_BRANCH%"
if errorlevel 1 (
    echo Update failed. Resolve the reported issue and try again.
    popd
    exit /b 1
)

echo Repository updated successfully.
popd
exit /b 0