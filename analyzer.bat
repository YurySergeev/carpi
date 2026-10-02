@echo off
rem Start the CarPi Analyzer (opens in your browser). Extra args pass through, e.g. analyzer.bat --port 8060
cd /d "%~dp0"
python -m carpi_app %*
if errorlevel 1 pause
