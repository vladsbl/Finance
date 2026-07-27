@echo off
REM Module 11 -- entry point for Windows Task Scheduler.
REM
REM Activates the project's virtual environment, then runs the daily
REM pipeline (pipeline/run_daily.py). Intended to be pointed to directly
REM by a Task Scheduler action -- see the setup procedure documented in
REM the chat response that introduced this file (or ask Claude Code to
REM repeat it) for the exact "Create Basic Task" steps.
REM
REM This file makes NO changes to Windows itself -- it is only a batch
REM script Task Scheduler is configured (manually, by the project owner)
REM to execute on a schedule.

setlocal

REM Repository root = the parent directory of this script's own folder
REM (pipeline\..), so the task works regardless of where the repo is cloned.
set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"

call ".venv\Scripts\activate.bat"
python pipeline\run_daily.py
set "EXIT_CODE=%ERRORLEVEL%"

call deactivate
exit /b %EXIT_CODE%
