@echo off
rem Sai com o codigo 3 quando voce clica em Atualizar: abre de novo com o codigo novo.
:loop
py -3.11 "%~dp0agentes.py" %*
if %errorlevel%==3 goto loop
rem Fechou de qualquer jeito (ate morto a forca): devolve a tela normal, o cursor e o mouse
rem ao terminal, senao sobra o desenho do painel por cima do PowerShell.
py -3.11 -c "import sys; sys.stdout.write(''.join('\x1b' + m for m in ('[?1049l', '[?25h', '[0m', '[?1003l', '[?1006l', '[?1000l', '[?1002l', '[?9001l', '[?2004l', '[?1004l')))"
