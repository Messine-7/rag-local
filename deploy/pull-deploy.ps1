<#
.SYNOPSIS
    Déploiement « pull » du PC fixe.
.DESCRIPTION
    Rien n'est envoyé depuis GitHub vers cette machine : c'est elle qui vient chercher
    le dernier commit de main et l'image Docker que la CI a construite et testée pour ce commit.
    Tant que l'image n'existe pas (CI en cours ou en échec), rien ne change.
    Lancé toutes les 2 minutes par le Planificateur de tâches (voir install-task.ps1).
#>

# Pas de "Stop" : sous Windows PowerShell 5.1, la sortie d'erreur des commandes natives
# deviendrait une exception. On contrôle $LASTEXITCODE à la main.
$ErrorActionPreference = "Continue"

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$composeArgs = @("-f", "docker-compose.yml", "-f", "deploy/compose.prod.yml")
$stateFile   = Join-Path $PSScriptRoot ".last-deployed"
$failedFile  = Join-Path $PSScriptRoot ".last-failed"
$logFile     = Join-Path $PSScriptRoot "deploy.log"

function Write-Log([string]$Message) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $Message" | Add-Content -Path $logFile -Encoding UTF8
}

function Read-State([string]$Path) {
    if (Test-Path $Path) { return (Get-Content $Path -Raw).Trim() }
    return ""
}

# Propriétaire GitHub, déduit du remote (ghcr.io impose des minuscules)
$remoteUrl = git remote get-url origin
if ($remoteUrl -notmatch 'github\.com[:/]([^/]+)/') {
    Write-Log "remote origin inattendu : $remoteUrl"
    exit 1
}
$owner = $Matches[1].ToLower()

git fetch --quiet origin main
if ($LASTEXITCODE -ne 0) { Write-Log "git fetch a échoué"; exit 1 }
$target = (git rev-parse origin/main).Trim()

# Déjà déployé, ou déjà tenté sans succès : on attend le commit suivant
if ($target -eq (Read-State $stateFile) -or $target -eq (Read-State $failedFile)) { exit 0 }

# L'image n'est publiée que si la CI est passée pour ce commit
$image = "ghcr.io/$owner/rag-local-api:$target"
docker pull --quiet $image *> $null
if ($LASTEXITCODE -ne 0) { exit 0 }   # pas encore publiée : on réessaiera au prochain passage

git merge --ff-only --quiet origin/main
if ($LASTEXITCODE -ne 0) { Write-Log "fast-forward impossible (modifications locales ?)"; exit 1 }

$env:GHCR_OWNER  = $owner
$env:APP_VERSION = $target
docker compose @composeArgs up -d --no-build --wait --wait-timeout 120
if ($LASTEXITCODE -ne 0) {
    Set-Content -Path $failedFile -Value $target
    Write-Log "ÉCHEC du déploiement de $target"
    docker compose @composeArgs logs --tail 50 api 2>&1 | Add-Content -Path $logFile -Encoding UTF8
    exit 1
}

Set-Content -Path $stateFile -Value $target
Write-Log "déployé $target"
