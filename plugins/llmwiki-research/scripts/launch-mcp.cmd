@echo off
setlocal
"%~dp0..\runtime\python\python.exe" -I -B "%~dp0launch_mcp.py" %*
exit /b %ERRORLEVEL%
