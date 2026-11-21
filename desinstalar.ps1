#Requires -Version 5.1
<#
    Desinstalador: para los procesos, borra el entorno y el acceso directo.
    No toca tus sesiones de OpenCode (viven en la base de datos de la app).
#>
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition

Write-Host ""
Write-Host "  Desinstalando el bot de Telegram..." -ForegroundColor Cyan

# procesos del bot y del motor
foreach ($p in (Get-CimInstance Win32_Process | Where-Object {
            $_.CommandLine -match 'control\.py|bot\.py|oc_shim\.py' -and $_.Name -match '^python' })) {
    Stop-Process -Id $p.ProcessId -Force
    Write-Host "   cerrado PID $($p.ProcessId)" -ForegroundColor Gray
}

# acceso directo
$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Telegram Bot.lnk"
if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "   acceso directo eliminado" -ForegroundColor Gray }

# entorno virtual
$venv = Join-Path $root ".venv"
if (Test-Path $venv) { Remove-Item $venv -Recurse -Force; Write-Host "   entorno .venv eliminado" -ForegroundColor Gray }

Write-Host ""
Write-Host "  Listo. Puedes borrar esta carpeta." -ForegroundColor Green
Write-Host "  (El fichero .env con el token sigue aqui; borralo tambien si no lo necesitas.)" -ForegroundColor Yellow
Write-Host ""
Read-Host "Pulsa Enter para cerrar" | Out-Null
