@echo off
setlocal
set "NOTES_ROOT=%~dp0"

rem Do not fall back to archived releases with the affected SQLite WAL runtime.
if exist "%NOTES_ROOT%dist\0.8.3\Notes\Notes.exe" (
    start "" /D "%NOTES_ROOT%dist\0.8.3\Notes" "%NOTES_ROOT%dist\0.8.3\Notes\Notes.exe"
    if errorlevel 1 goto launch_failed
    exit /b 0
)

if exist "%NOTES_ROOT%dist\0.8.2\Notes\Notes.exe" (
    start "" /D "%NOTES_ROOT%dist\0.8.2\Notes" "%NOTES_ROOT%dist\0.8.2\Notes\Notes.exe"
    if errorlevel 1 goto launch_failed
    exit /b 0
)

if exist "%NOTES_ROOT%dist\0.8.1\Notes\Notes.exe" (
    start "" /D "%NOTES_ROOT%dist\0.8.1\Notes" "%NOTES_ROOT%dist\0.8.1\Notes\Notes.exe"
    if errorlevel 1 goto launch_failed
    exit /b 0
)

if exist "%NOTES_ROOT%dist\0.8.0\Notes\Notes.exe" (
    start "" /D "%NOTES_ROOT%dist\0.8.0\Notes" "%NOTES_ROOT%dist\0.8.0\Notes\Notes.exe"
    if errorlevel 1 goto launch_failed
    exit /b 0
)

if exist "%NOTES_ROOT%dist\0.7.0\Notes\Notes.exe" (
    start "" /D "%NOTES_ROOT%dist\0.7.0\Notes" "%NOTES_ROOT%dist\0.7.0\Notes\Notes.exe"
    if errorlevel 1 goto launch_failed
    exit /b 0
)

if exist "%NOTES_ROOT%dist\0.6.0\Notes\Notes.exe" (
    start "" /D "%NOTES_ROOT%dist\0.6.0\Notes" "%NOTES_ROOT%dist\0.6.0\Notes\Notes.exe"
    if errorlevel 1 goto launch_failed
    exit /b 0
)

if exist "%NOTES_ROOT%dist\0.5.0\Notes\Notes.exe" (
    start "" /D "%NOTES_ROOT%dist\0.5.0\Notes" "%NOTES_ROOT%dist\0.5.0\Notes\Notes.exe"
    if errorlevel 1 goto launch_failed
    exit /b 0
)

rem Development fallback: use only this project's Python environment.
if exist "%NOTES_ROOT%.venv\Scripts\pythonw.exe" (
    if not exist "%NOTES_ROOT%src\notes\__main__.py" goto missing_files
    set "PYTHONPATH=%NOTES_ROOT%src"
    start "" /D "%NOTES_ROOT%" "%NOTES_ROOT%.venv\Scripts\pythonw.exe" -m notes
    if errorlevel 1 goto launch_failed
    exit /b 0
)

:missing_files
echo Notes could not be found.
echo Use the complete 0.8.3-or-newer package, or the verified private runtime.
echo See README.md for setup instructions.
pause
exit /b 1

:launch_failed
echo Notes could not be started. Please check the application files.
pause
exit /b 1
