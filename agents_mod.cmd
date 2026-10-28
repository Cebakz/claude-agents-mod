@echo off
rem Exits with code 3 when you click Reload: opens again with the new code.
:loop
py -3.11 "%~dp0agents_mod.py" %*
if %errorlevel%==3 goto loop
rem Closed in any way (even killed): give the terminal back its normal screen, cursor and mouse,
rem otherwise the panel drawing is left on top of PowerShell.
py -3.11 -c "import sys; sys.stdout.write(''.join('\x1b' + m for m in ('[?1049l', '[?25h', '[0m', '[?1003l', '[?1006l', '[?1000l', '[?1002l', '[?9001l', '[?2004l', '[?1004l')))"
