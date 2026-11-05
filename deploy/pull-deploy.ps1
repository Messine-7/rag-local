﻿<#
.SYNOPSIS
    Déploiement « pull » du PC fixe.
.DESCRIPTION
    Rien n'est envoyé depuis GitHub vers cette machine : c'est elle qui vient chercher
    le dernier commit de main et l'image Docker que la CI a construite et testée pour ce commit.
    Tant que l'image n'existe pas (CI en cours ou en échec), rien ne change.
    Si la nouvelle version ne démarre pas, la version précédente est relancée.
    Après un déploiement réussi, seules l'image en service et la précédente sont gardées.
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

# Recopie dans le journal, en retrait, ce qu'on lui envoie (sortie d'une commande)
function Add-ToLog {
    process { "    $_" | Add-Content -Path $logFile -Encoding UTF8 }
}

function Read-State([string]$Path) {
    if (Test-Path $Path) { return (Get-Content $Path -Raw).Trim() }
    return ""
}

# Lance une version et attend que l'API soit saine ; renvoie $true si c'est le cas
function Start-Version([string]$Version) {
    $env:APP_VERSION = $Version
    docker compose @composeArgs up -d --no-build --wait --wait-timeout 120 2>&1 | Add-ToLog
    return ($LASTEXITCODE -eq 0)
}

# Supprime les images de l'API, sauf celles à garder (en service et précédente)
function Remove-OldImages([string]$ImageName, [string[]]$Keep) {
    foreach ($tag in (docker image ls $ImageName --format "{{.Tag}}")) {
        if ($Keep -notcontains $tag) { docker image rm "${ImageName}:${tag}" *> $null }
    }
    docker image prune -f *> $null   # couches orphelines
}

# Propriétaire GitHub, déduit du remote (ghcr.io impose des minuscules)
$remoteUrl = git remote get-url origin
if ($remoteUrl -notmatch 'github\.com[:/]([^/]+)/') {
    Write-Log "remote origin inattendu : $remoteUrl"
    exit 1
}
$owner = $Matches[1].ToLower()
$imageName = "ghcr.io/$owner/rag-local-api"
$env:GHCR_OWNER = $owner

git fetch --quiet origin main
if ($LASTEXITCODE -ne 0) { Write-Log "git fetch a échoué"; exit 1 }
$target  = (git rev-parse origin/main).Trim()
$current = Read-State $stateFile

# Déjà déployé, ou déjà tenté sans succès : on attend le commit suivant
if ($target -eq $current -or $target -eq (Read-State $failedFile)) { exit 0 }

# L'image n'est publiée que si la CI est passée pour ce commit
docker pull --quiet "${imageName}:${target}" *> $null
if ($LASTEXITCODE -ne 0) { exit 0 }   # pas encore publiée : on réessaiera au prochain passage

git merge --ff-only --quiet origin/main
if ($LASTEXITCODE -ne 0) { Write-Log "fast-forward impossible (modifications locales ?)"; exit 1 }

Write-Log "déploiement de $target"
if (Start-Version $target) {
    Set-Content -Path $stateFile -Value $target
    Write-Log "déployé $target"
    Remove-OldImages $imageName @($target, $current)
    exit 0
}

Set-Content -Path $failedFile -Value $target
Write-Log "ÉCHEC du déploiement de $target, dernières lignes de l'API :"
docker compose @composeArgs logs --tail 50 api 2>&1 | Add-ToLog

# Retour arrière : on relance l'image précédente (le clone, lui, reste sur le nouveau commit)
if (-not $current) {
    Write-Log "aucune version précédente : l'API reste hors service"
    exit 1
}
Write-Log "retour à la version précédente $current"
if (Start-Version $current) {
    Write-Log "retour arrière réussi : $current est en service"
} else {
    Write-Log "ÉCHEC du retour arrière : l'API est hors service"
}
exit 1
