@echo off
setlocal
"%~dp0..\runtime\python\python.exe" -I -B "%~dp0research_cockpit.py" %*
exit /b %ERRORLEVEL%
