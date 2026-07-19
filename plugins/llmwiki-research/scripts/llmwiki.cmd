@echo off
setlocal
"%~dp0..\runtime\python\python.exe" -I -B "%~dp0llmwiki.py" %*
exit /b %ERRORLEVEL%
