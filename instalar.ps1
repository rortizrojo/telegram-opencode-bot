#Requires -Version 5.1
<#
    Instalador del bot de Telegram para OpenCode.
    Crea un entorno Python aislado, instala dependencias, localiza el motor v2,
    pide el token del bot y deja un acceso directo en el escritorio.
#>
param([switch]$SinPausa)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$version = "?"
if (Test-Path (Join-Path $root "VERSION")) {
    $version = (Get-Content (Join-Path $root "VERSION") -Raw).Trim()
}

function Titulo($t) { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Ok($t)     { Write-Host "   [ok]   $t" -ForegroundColor Green }
function Aviso($t)  { Write-Host "   [!]    $t" -ForegroundColor Yellow }
function Fallo($t)  { Write-Host "   [x]    $t" -ForegroundColor Red }
function Pausa {
    if (-not $SinPausa) { Write-Host ""; Read-Host "Pulsa Enter para cerrar" | Out-Null }
}

Write-Host ""
Write-Host "  ============================================" -ForegroundColor DarkCyan
Write-Host "   Bot de Telegram para OpenCode  v$version" -ForegroundColor White
Write-Host "  ============================================" -ForegroundColor DarkCyan
Write-Host "  Carpeta: $root" -ForegroundColor DarkGray

try {

    # ---------------------------------------------------------------- python
    Titulo "1/6  Python"

    $MIN = [version]"3.10"

    function Get-PythonCandidatos {
        $lista = New-Object System.Collections.ArrayList
        foreach ($n in @("py", "python", "python3")) {
            $c = Get-Command $n -ErrorAction SilentlyContinue
            if ($c) { [void]$lista.Add($c.Source) }
        }
        $base = Join-Path $env:LOCALAPPDATA "Programs\Python"
        if (Test-Path $base) {
            Get-ChildItem $base -Directory -Filter "Python3*" -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                ForEach-Object {
                    $p = Join-Path $_.FullName "python.exe"
                    if (Test-Path $p) { [void]$lista.Add($p) }
                }
        }
        return $lista
    }

    function Resolver-Python {
        foreach ($c in (Get-PythonCandidatos)) {
            try {
                $ver = & $c -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
                if ($LASTEXITCODE -eq 0 -and $ver -and ([version]$ver -ge $MIN)) {
                    $exe = & $c -c "import sys;print(sys.executable)" 2>$null
                    if ($exe -and (Test-Path $exe)) { return $exe.Trim() }
                }
            } catch { }
        }
        return $null
    }

    $pyExe = Resolver-Python

    if (-not $pyExe) {
        Aviso "No encontre Python 3.10 o superior."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            $r = Read-Host "   ?Instalarlo ahora con winget? Python 3.11 (S/N)"
            if ($r -match '^[sSyY]') {
                Write-Host "   Instalando Python 3.11, puede tardar unos minutos..." -ForegroundColor Gray
                winget install -e --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements | Out-Host
                $pyExe = Resolver-Python
            }
        }
        if (-not $pyExe) {
            Fallo "Instala Python 3.10+ desde https://www.python.org/downloads/ y vuelve a ejecutar este instalador."
            Pausa
            exit 1
        }
    }
    Ok "Python: $pyExe"

    # ---------------------------------------------------------------- venv
    Titulo "2/6  Entorno aislado"

    $venv = Join-Path $root ".venv"
    $vpy  = Join-Path $venv "Scripts\python.exe"
    $vpw  = Join-Path $venv "Scripts\pythonw.exe"

    if (-not (Test-Path $vpy)) {
        Write-Host "   Creando entorno en .venv ..." -ForegroundColor Gray
        & $pyExe -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw "No pude crear el entorno virtual." }
    }
    Ok "Entorno: $venv"

    Titulo "3/6  Dependencias"
    & $vpy -m pip install --upgrade pip --quiet --disable-pip-version-check
    & $vpy -m pip install -r (Join-Path $root "requirements.txt") --quiet --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw "Fallo la instalacion de dependencias (revisa la conexion a internet)." }
    Ok "Dependencias instaladas"

    # ---------------------------------------------------------------- motor v2
    Titulo "4/6  Motor de OpenCode (opencode-cli.exe)"

    function Buscar-Motor {
        if ($env:OC_ENGINE -and (Test-Path $env:OC_ENGINE)) { return $env:OC_ENGINE }

        # 1) versiones preparadas por la app, la mas nueva primero
        $cliRoot = Join-Path $env:APPDATA "ai.opencode.desktop\cli"
        $versionados = @()
        if (Test-Path $cliRoot) {
            Get-ChildItem $cliRoot -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                $exe = Join-Path $_.FullName "opencode-cli.exe"
                if (Test-Path $exe) {
                    $m = [regex]::Match($_.Name, '^\d+(\.\d+)*')
                    $v = if ($m.Success) { [version]$m.Value } else { [version]"0.0" }
                    $versionados += [pscustomobject]@{ Version = $v; Path = $exe }
                }
            }
        }
        if ($versionados.Count -gt 0) {
            return ($versionados | Sort-Object Version -Descending | Select-Object -First 1).Path
        }

        # 2) el que trae la app instalada
        $p2 = Join-Path $env:LOCALAPPDATA "Programs\@opencode-aidesktop\resources\opencode-cli.exe"
        if (Test-Path $p2) { return $p2 }

        # 3) en el PATH
        $cmd = Get-Command "opencode-cli" -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }

        return $null
    }

    $motor = Buscar-Motor
    if ($motor) {
        Ok "Motor encontrado: $motor"
    } else {
        Aviso "No encontre opencode-cli.exe."
        Write-Host "   Instala la app de escritorio de OpenCode (https://opencode.ai) y vuelve a ejecutar" -ForegroundColor Gray
        Write-Host "   el instalador, o indica la ruta a mano en el fichero .env con OC_ENGINE=..." -ForegroundColor Gray
        $r = Read-Host "   ?Abrir la pagina de descarga ahora? (S/N)"
        if ($r -match '^[sSyY]') { Start-Process "https://opencode.ai" }
    }

    # ---------------------------------------------------------------- token
    Titulo "5/6  Token del bot de Telegram"

    $envPath = Join-Path $root ".env"
    $cfg = @{}
    if (Test-Path $envPath) {
        Get-Content $envPath | ForEach-Object {
            $l = $_.Trim()
            if ($l -and -not $l.StartsWith("#") -and $l.Contains("=")) {
                $k, $v = $l.Split("=", 2)
                $cfg[$k.Trim()] = $v.Trim()
            }
        }
    }

    $token = $cfg["TELEGRAM_TOKEN"]
    if ($token) {
        $visible = $token.Substring(0, [Math]::Min(12, $token.Length)) + "..."
        $r = Read-Host "   Ya hay un token guardado ($visible). ?Usarlo? (S/N)"
        if ($r -notmatch '^[sSyY]') { $token = $null }
    }

    if (-not $token) {
        Write-Host ""
        Write-Host "   Necesitas un bot nuevo:" -ForegroundColor Gray
        Write-Host "     1. Abre Telegram y habla con @BotFather" -ForegroundColor Gray
        Write-Host "     2. Envia /newbot y elige un nombre" -ForegroundColor Gray
        Write-Host "     3. Copia el token que te da (123456789:AAH...)" -ForegroundColor Gray
        Write-Host ""
        while (-not $token) {
            $t = Read-Host "   Pega el token"
            if ($t -match '^\d{6,}:[A-Za-z0-9_-]{30,}$') { $token = $t.Trim() }
            else { Fallo "Ese token no tiene buena pinta. Debe ser 123456789:AA... (sin espacios)." }
        }
    }
    Ok "Token configurado"

    $lineas = @(
        "# Configuracion del bot. NO compartas este fichero.",
        "TELEGRAM_TOKEN=$token",
        "OPENCODE_URL=http://127.0.0.1:4095",
        "OPENCODE_USER=opencode",
        "OPENCODE_PASS=opencode123"
    )
    if ($cfg["OC_ENGINE"]) { $lineas += "OC_ENGINE=$($cfg['OC_ENGINE'])" }
    if ($cfg["OC_DIR"])    { $lineas += "OC_DIR=$($cfg['OC_DIR'])" }
    Set-Content -Path $envPath -Value $lineas -Encoding ASCII
    Ok "Guardado .env"

    # ---------------------------------------------------------------- acceso directo
    Titulo "6/6  Acceso directo y prueba"

    try {
        $escritorio = [Environment]::GetFolderPath("Desktop")
        $lnkPath = Join-Path $escritorio "Telegram Bot.lnk"
        $ws = New-Object -ComObject WScript.Shell
        $lnk = $ws.CreateShortcut($lnkPath)
        $lnk.TargetPath = $vpw
        $lnk.Arguments = '"' + (Join-Path $root "control.py") + '"'
        $lnk.WorkingDirectory = $root
        $lnk.Description = "Panel del bot de Telegram para OpenCode"
        $lnk.Save()
        Ok "Acceso directo en el escritorio: Telegram Bot"
    } catch {
        Aviso "No pude crear el acceso directo: $($_.Exception.Message)"
    }

    Write-Host "   Probando que el motor arranca..." -ForegroundColor Gray
    if ($motor) { $env:OC_ENGINE = $motor }
    $shim = Start-Process -FilePath $vpy `
        -ArgumentList ('"' + (Join-Path $root "oc_shim.py") + '"') `
        -WorkingDirectory $root -PassThru -WindowStyle Hidden

    $sano = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:4095/health" -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -eq 200) { $sano = $true; break }
        } catch { }
    }

    if ($sano) {
        Ok "Motor v2 respondiendo"
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:4095/health" -TimeoutSec 5
        Write-Host "   Estado: $($r | ConvertTo-Json -Compress)" -ForegroundColor Gray
    } else {
        Aviso "El motor no respondio en 60 s. Revisa oc_shim.log en la carpeta."
    }

    # lo dejamos parado: el panel lo arranca y lo vigila
    if ($shim -and -not $shim.HasExited) {
        try { & taskkill /PID $shim.Id /T /F 2>$null | Out-Null } catch { }
    }

    Write-Host ""
    Write-Host "  ============================================" -ForegroundColor DarkCyan
    Write-Host "   Instalacion terminada" -ForegroundColor Green
    Write-Host "  ============================================" -ForegroundColor DarkCyan
    Write-Host ""
    Write-Host "   1. Abre el acceso directo 'Telegram Bot' del escritorio" -ForegroundColor White
    Write-Host "   2. Pulsa Arrancar" -ForegroundColor White
    Write-Host "   3. En Telegram, habla con tu bot y envia /start" -ForegroundColor White
    Write-Host ""
    Write-Host "   Los modelos necesitan tu cuenta de OpenCode iniciada" -ForegroundColor Gray
    Write-Host "   (abre la app de escritorio una vez y autentica tus proveedores)." -ForegroundColor Gray
    Write-Host ""

} catch {
    Write-Host ""
    Fallo $_.Exception.Message
    Write-Host ""
}

Pausa
