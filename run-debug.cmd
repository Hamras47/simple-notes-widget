@echo off
rem Start the widget with a console and devtools, so problems are visible.
rem Close the console window to stop the widget.
setlocal
set "HERE=%~dp0"
"%HERE%.venv\Scripts\python.exe" "%HERE%app.py" --debug
