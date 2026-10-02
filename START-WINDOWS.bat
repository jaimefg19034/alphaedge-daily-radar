@echo off
cd /d "%~dp0"
echo AlphaEdge local preview: http://localhost:8000
python -m http.server 8000
pause
