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

REM Branch name argument
set "NEW_BRANCH=%~1"
if "%NEW_BRANCH%"=="" (
    echo Usage: github_new_branch branch_name
    popd
    exit /b 1
)

REM Validate repository
"%GIT_EXE%" rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo This folder is not a Git repository.
    popd
    exit /b 1
)

REM Keep remote refs updated
"%GIT_EXE%" fetch origin
if errorlevel 1 (
    echo Failed to fetch from origin.
    popd
    exit /b 1
)

REM Refuse if branch already exists locally
"%GIT_EXE%" show-ref --verify --quiet "refs/heads/%NEW_BRANCH%"
if not errorlevel 1 (
    echo Local branch %NEW_BRANCH% already exists.
    echo Switching to it...
    "%GIT_EXE%" checkout "%NEW_BRANCH%"
    popd
    exit /b %errorlevel%
)

REM Refuse if branch exists remotely
"%GIT_EXE%" show-ref --verify --quiet "refs/remotes/origin/%NEW_BRANCH%"
if not errorlevel 1 (
    echo Remote branch origin/%NEW_BRANCH% already exists.
    echo Creating local tracking branch and switching to it...
    "%GIT_EXE%" checkout -b "%NEW_BRANCH%" --track "origin/%NEW_BRANCH%"
    popd
    exit /b %errorlevel%
)

REM Create branch from current HEAD and publish to GitHub
"%GIT_EXE%" checkout -b "%NEW_BRANCH%"
if errorlevel 1 (
    echo Failed to create local branch %NEW_BRANCH%.
    popd
    exit /b 1
)

"%GIT_EXE%" push -u origin "%NEW_BRANCH%"
if errorlevel 1 (
    echo Branch created locally but push failed.
    popd
    exit /b 1
)

echo Done. Branch %NEW_BRANCH% created and tracking origin/%NEW_BRANCH%.

popd
exit /b 0
