@echo off
setlocal
cd /d "%~dp0"

if exist "D:\Anaconda\envs\web_login\python.exe" (
    "D:\Anaconda\envs\web_login\python.exe" "%~dp0login.py" %*
    exit /b %errorlevel%
)

if exist "D:\Anaconda\python.exe" (
    "D:\Anaconda\python.exe" "%~dp0login.py" %*
    exit /b %errorlevel%
)

python "%~dp0login.py" %*
exit /b %errorlevel%
