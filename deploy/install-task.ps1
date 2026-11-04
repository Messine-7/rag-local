<#
.SYNOPSIS
    Enregistre la tâche planifiée qui lance pull-deploy.ps1 toutes les 2 minutes.
.DESCRIPTION
    La tâche tourne avec le compte courant, quand la session est ouverte :
    c'est aussi la condition pour que Docker Desktop fonctionne.
    À lancer une seule fois sur le PC fixe, depuis la racine du clone.
#>

$script = Join-Path $PSScriptRoot "pull-deploy.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName "rag-local-deploy" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Déploiement pull du projet RAG local" -Force

Write-Host "Tâche 'rag-local-deploy' enregistrée. Journal : $(Join-Path $PSScriptRoot 'deploy.log')"
