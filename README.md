# claude agents mod

Painel no terminal para acompanhar e conversar com as sessões em segundo plano do Claude Code
(`claude --bg`), no lugar do `claude agents`. Mostra cada sessão com o modelo, o contexto, o
cache e o modo, e a conversa dela ao lado, ao vivo. Dá para responder, enfileirar mensagens,
responder perguntas e permissões, abrir arquivos e links e copiar comandos sem entrar na sessão.

## O que precisa

- Windows 10/11 com o **Windows Terminal**.
- **Python 3.11** com o launcher `py` (`py -3.11 --version` tem que funcionar).
- **Claude Code** instalado (`claude --version`) e logado.
- Git, para baixar e atualizar.

## Instalar

```powershell
git clone https://github.com/Cebakz/claude-agents-mod.git
cd claude-agents-mod
powershell -ExecutionPolicy Bypass -File instalar.ps1 -LinhaStatus
```

Abra um PowerShell novo e rode `claude agents mod`. Na primeira vez ele instala sozinho o
`pywinpty` e o `pyte`. No cmd, o comando é `claude-agents-mod`.

Sem o `-LinhaStatus` o painel funciona igual, só não mostra a barra de contexto por categoria
e o uso do plano (5h / 7d). Com ele, o instalador copia o mod `linha-status` para
`~/.claude/mods/linha-status` e o liga no `~/.claude/settings.json` (guardando uma cópia do
arquivo antes). Sessões já abertas pegam o mod com `/reload-plugins`.

## Atualizar

```powershell
cd claude-agents-mod
git pull
powershell -ExecutionPolicy Bypass -File instalar.ps1 -LinhaStatus
```

Com o painel aberto, F5 recarrega o programa.

## Usar

- **Escolher e acompanhar:** setas ou clique escolhem a sessão na lista; a conversa aparece
  ao lado. Enter ou clique duplo abre a sessão.
- **Responder:** digite e dê enter. Shift+enter pula linha; ↑ com a caixa vazia (espaço abre)
  traz seus prompts anteriores. Tab guarda a mensagem na fila; alt+↑ edita a fila.
- **Perguntas e permissões:** aparecem embaixo da conversa. O número escolhe a opção; texto
  seu e enter responde no "Type something"; "1 3" marca várias.
- **Arquivos:** os arquivos citados viram botões (abrir, ⧉ copy file para colar no Teams /
  WhatsApp, ⌕ in folder). Arrastar o mouse na conversa seleciona e copia.
- **⊞ Features** (no topo): liga e desliga cada função do painel. **? Shortcuts** lista os atalhos.

## Pontos de atenção

- Só roda no Windows (lê teclado e mouse pelo console do Windows).
- Lê arquivos internos do Claude Code (`~/.claude/jobs/*/state.json` e as conversas `.jsonl`),
  que não são documentados: uma atualização do Claude Code pode mudar o formato e quebrar algo.
- Para escrever numa sessão ele abre um `claude attach` escondido. Com pergunta ou permissão
  aberta ele nunca aperta Enter sozinho, para não aprovar nada sem você.
- O SUMMARY no alto de cada conversa é escrito pelo Haiku e gasta tokens; desligue em ⊞ Features.
- Seus dados (grupos, visão, fila, chaves do painel) ficam em `~/.claude/agentes-mod/`.
- `AGENTES_KEYLOG=1` liga o registro das teclas em `~/.claude/agentes-mod/teclas.log`, para
  achar bug de teclado. Desligado por padrão: ele grava tudo o que você digita.
- Projeto independente, sem ligação com a Anthropic.

## Licença

MIT, ver [LICENSE](LICENSE).
