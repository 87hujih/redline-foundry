[CmdletBinding()]
param(
    [switch]$StatusOnly,
    [switch]$SkipApi
)

$ErrorActionPreference = "Stop"
$repository = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtime = Join-Path $repository ".runtime"
$postgresRoot = Join-Path $runtime "postgresql-17"
$postgresData = Join-Path $runtime "postgres-data-17-v2"
$postgresControl = Join-Path $postgresRoot "bin\pg_ctl.exe"
$postgresLog = Join-Path $runtime "postgres-17.log"
$tikaContainer = "docreview-tika"

function Test-LocalPort {
    param([Parameter(Mandatory = $true)][int]$Port)

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.ConnectAsync("127.0.0.1", $Port)
        if (-not $connect.Wait(1000)) {
            return $false
        }
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Wait-LocalPort {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$Service
    )

    foreach ($attempt in 1..40) {
        if (Test-LocalPort -Port $Port) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "$Service did not become ready on 127.0.0.1:$Port"
}

function Start-LocalPostgres {
    if (Test-LocalPort -Port 55432) {
        Write-Host "PostgreSQL is ready on 127.0.0.1:55432"
        return
    }
    if (-not (Test-Path -LiteralPath $postgresControl -PathType Leaf)) {
        throw "PostgreSQL runtime is missing: $postgresControl"
    }
    if (-not (Test-Path -LiteralPath $postgresData -PathType Container)) {
        throw "PostgreSQL data directory is missing: $postgresData"
    }

    & $postgresControl start -D $postgresData -l $postgresLog -o "-p 55432 -h 127.0.0.1"
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL failed to start"
    }
    Wait-LocalPort -Port 55432 -Service "PostgreSQL"
    Write-Host "PostgreSQL started on 127.0.0.1:55432"
}

function Start-LocalTika {
    if (Test-LocalPort -Port 9998) {
        Write-Host "Tika is ready on 127.0.0.1:9998"
        return
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker is required to start Tika"
    }

    & docker inspect $tikaContainer *> $null
    if ($LASTEXITCODE -eq 0) {
        & docker start $tikaContainer | Out-Null
    }
    else {
        & docker run --detach --name $tikaContainer --publish 127.0.0.1:9998:9998 apache/tika:3.3.0.0 | Out-Null
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Tika failed to start"
    }
    Wait-LocalPort -Port 9998 -Service "Tika"
    Write-Host "Tika started on 127.0.0.1:9998"
}

if (-not (Test-Path -LiteralPath (Join-Path $repository ".env") -PathType Leaf)) {
    throw "Create the untracked .env file before local startup"
}

Start-LocalPostgres
Start-LocalTika

if ($StatusOnly) {
    [pscustomobject]@{
        PostgreSQL = Test-LocalPort -Port 55432
        Tika = Test-LocalPort -Port 9998
        API = Test-LocalPort -Port 8080
    }
    return
}

Push-Location $repository
try {
    & uv run docreview-init-local
    if ($LASTEXITCODE -ne 0) {
        throw "Local identity bootstrap failed"
    }
    if ($SkipApi) {
        return
    }
    if (Test-LocalPort -Port 8080) {
        Write-Host "DocReview API is already ready at http://127.0.0.1:8080"
        return
    }
    & uv run docreview-api
    if ($LASTEXITCODE -ne 0) {
        throw "DocReview API exited with an error"
    }
}
finally {
    Pop-Location
}
