@echo off
rem Which Python: 3.11 if there is one (the tested one), otherwise the newest 3.x through the py
rem launcher, otherwise "python" (installs without the launcher, such as the Microsoft Store one).
rem install.ps1 already checked that one of them is 3.11 or newer.
set "PY=python"
py -3.11 -c "" >nul 2>nul
if not errorlevel 1 (set "PY=py -3.11" & goto loop)
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
rem Exits with code 3 when you click Reload: opens again with the new code.
:loop
%PY% "%~dp0agents_mod.py" %*
if %errorlevel%==3 goto loop
rem Closed in any way (even killed): give the terminal back its normal screen, cursor and mouse,
rem otherwise the panel drawing is left on top of PowerShell.
%PY% -c "import sys; sys.stdout.write(''.join('\x1b' + m for m in ('[?1049l', '[?25h', '[0m', '[?1003l', '[?1006l', '[?1000l', '[?1002l', '[?9001l', '[?2004l', '[?1004l')))"
