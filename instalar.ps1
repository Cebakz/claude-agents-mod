# Instala (ou atualiza) o "claude agents mod" nesta máquina (Windows):
#   1. copia o programa para ~/.claude/agentes-mod/programa;
#   2. cria o atalho claude-agents-mod.cmd em ~/.local/bin, apontando para essa cópia;
#   3. no PowerShell, "claude agents mod" passa a abrir o painel (o resto do "claude" segue igual);
#   4. com -LinhaStatus, instala também o mod linha-status (barra de contexto e uso do plano).
# As bibliotecas (pywinpty e pyte) o próprio programa instala na primeira vez que abre.
#
# Uso, de dentro da pasta clonada:  powershell -ExecutionPolicy Bypass -File instalar.ps1 [-LinhaStatus]
# Para atualizar depois:            git pull  e rode o mesmo comando de novo.
param([switch]$LinhaStatus)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot

$real = Get-Command claude.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $real) { throw "não achei o claude.exe no PATH: instale o Claude Code antes" }
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw "não achei o py (Python launcher): instale o Python 3.11" }

# 1. O programa.
$dest = Join-Path $env:USERPROFILE ".claude\agentes-mod\programa"
New-Item -ItemType Directory -Force $dest | Out-Null
Copy-Item (Join-Path $here "agentes.py"), (Join-Path $here "agentes.cmd") $dest -Force

# 2. O atalho.
$bin = Join-Path $env:USERPROFILE ".local\bin"
New-Item -ItemType Directory -Force $bin | Out-Null
@"
@echo off
rem Abre o painel de agentes (claude agents mod). Gerado por instalar.ps1.
call "$dest\agentes.cmd" %*
"@ | Set-Content -Encoding ascii (Join-Path $bin "claude-agents-mod.cmd")

# 3. A função "claude" no perfil do PowerShell (Windows PowerShell e, se houver, o 7).
$funcao = @'

# "claude agents mod" abre o painel de agentes; qualquer outro "claude ..." vai direto ao claude.exe.
function claude {
    $exe = '__EXE__'
    if (-not (Test-Path $exe)) { $exe = (Get-Command claude.exe -CommandType Application | Select-Object -First 1).Source }
    if ($args.Count -ge 2 -and $args[0] -eq "agents" -and $args[1] -eq "mod") {
        & (Join-Path $env:USERPROFILE ".local\bin\claude-agents-mod.cmd") @($args | Select-Object -Skip 2)
        return
    }
    if ($MyInvocation.ExpectingInput) { $input | & $exe @args } else { & $exe @args }
}
'@.Replace("__EXE__", $real.Source.Replace("'", "''"))
$docs = [Environment]::GetFolderPath("MyDocuments")
if (-not $docs) { $docs = Join-Path $env:USERPROFILE "Documents" }
$perfis = @(Join-Path $docs "WindowsPowerShell\Microsoft.PowerShell_profile.ps1")
if (Get-Command pwsh -ErrorAction SilentlyContinue) { $perfis += Join-Path $docs "PowerShell\Microsoft.PowerShell_profile.ps1" }
foreach ($perfil in $perfis) {
    New-Item -ItemType Directory -Force (Split-Path $perfil) | Out-Null
    if (-not (Test-Path $perfil)) { New-Item -ItemType File $perfil | Out-Null }
    if (-not (Select-String -Path $perfil -Pattern 'claude-agents-mod.cmd' -Quiet)) {
        $funcao | Add-Content -Encoding utf8 $perfil
    }
}
# Sem isso o perfil não carrega no Windows padrão (scripts bloqueados). Só para este usuário.
if ((Get-ExecutionPolicy -Scope CurrentUser) -eq "Undefined" -and
    (Get-ExecutionPolicy -Scope LocalMachine) -in @("Undefined", "Restricted", "AllSigned")) {
    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force
}

# 4. Opcional: o mod linha-status, que grava o /context e o uso do plano para o painel mostrar.
if ($LinhaStatus) {
    $mod = Join-Path $env:USERPROFILE ".claude\mods\linha-status"
    New-Item -ItemType Directory -Force $mod | Out-Null
    Copy-Item (Join-Path $here "linha-status\*") $mod -Recurse -Force
    # O Claude Code carrega o mod pela variável CLAUDE_CODE_PLUGIN_DIRS no settings.json do usuário.
    $settings = Join-Path $env:USERPROFILE ".claude\settings.json"
    $json = if (Test-Path $settings) { Get-Content $settings -Raw -Encoding utf8 | ConvertFrom-Json } else { [pscustomobject]@{} }
    if (-not $json.env) { $json | Add-Member -NotePropertyName env -NotePropertyValue ([pscustomobject]@{}) }
    $dirs = [string]$json.env.CLAUDE_CODE_PLUGIN_DIRS
    if (($dirs -split ';') -notcontains $mod) {
        if (Test-Path $settings) { Copy-Item $settings "$settings.bak-agentes-mod" -Force }   # cópia antes de mexer
        $novo = (@($dirs -split ';' | Where-Object { $_ }) + $mod) -join ';'
        if ($json.env.PSObject.Properties.Name -contains "CLAUDE_CODE_PLUGIN_DIRS") { $json.env.CLAUDE_CODE_PLUGIN_DIRS = $novo }
        else { $json.env | Add-Member -NotePropertyName CLAUDE_CODE_PLUGIN_DIRS -NotePropertyValue $novo }
        [IO.File]::WriteAllText($settings, ($json | ConvertTo-Json -Depth 32), (New-Object Text.UTF8Encoding $false))
        Write-Host "linha-status ligado no settings.json (cópia do anterior em settings.json.bak-agentes-mod)."
    }
}

Write-Host "Pronto. Abra um PowerShell novo e rode: claude agents mod"
