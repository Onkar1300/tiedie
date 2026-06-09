Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Update with your Raspberry Pi IP addresses
$Pi_IPs = @(
    "10.222.66.109"
)

$User = "test"
$TargetDir = "/home/test/agent"

# Resolve source paths relative to this script, so it works from any CWD.
$SourceDir = $PSScriptRoot

$GatewayCaPem = Join-Path $SourceDir "..\gateway\ca_certificates\ca.pem"
$GatewayServerCrt = Join-Path $SourceDir "..\gateway\certs\server.crt"
$GatewayServerKey = Join-Path $SourceDir "..\gateway\certs\server.key"

$LocalCaPem = Join-Path $SourceDir "ca.pem"
$LocalServerCrt = Join-Path $SourceDir "server.crt"
$LocalServerKey = Join-Path $SourceDir "server.key"

$CaPem = if (Test-Path $GatewayCaPem) { $GatewayCaPem } elseif (Test-Path $LocalCaPem) { $LocalCaPem } else { throw "Missing CA cert. Expected '$GatewayCaPem' or '$LocalCaPem'." }
$ServerCrt = if (Test-Path $GatewayServerCrt) { $GatewayServerCrt } elseif (Test-Path $LocalServerCrt) { $LocalServerCrt } else { throw "Missing server cert. Expected '$GatewayServerCrt' or '$LocalServerCrt'." }
$ServerKey = if (Test-Path $GatewayServerKey) { $GatewayServerKey } elseif (Test-Path $LocalServerKey) { $LocalServerKey } else { throw "Missing server key. Expected '$GatewayServerKey' or '$LocalServerKey'." }

$PiApFiles = @(
    (Join-Path $SourceDir "main.py"),
    (Join-Path $SourceDir "grpc_server.py"),
    (Join-Path $SourceDir "zephyr_serial_backend.py"),
    (Join-Path $SourceDir "requirements.txt"),
    (Join-Path $SourceDir "README.md"),
    $CaPem,
    $ServerCrt,
    $ServerKey
)

foreach ($Path in $PiApFiles) {
    if (-not (Test-Path $Path)) {
        throw "Missing file: $Path"
    }
}

$GrpcProtoDir = Join-Path $SourceDir "grpc_proto"
if (-not (Test-Path $GrpcProtoDir)) {
    throw "Missing directory: $GrpcProtoDir"
}

foreach ($IP in $Pi_IPs) {
    Write-Host "----------------------------------------" -ForegroundColor Cyan
    Write-Host "Connecting to Raspberry Pi at $IP..." -ForegroundColor Green

    Write-Host "Creating target directory..."
    ssh -o StrictHostKeyChecking=no "$User@$IP" "mkdir -p $TargetDir"

    Write-Host "Pushing files..."
    scp -o StrictHostKeyChecking=no @PiApFiles "$User@${IP}:$TargetDir/"
    scp -r -o StrictHostKeyChecking=no $GrpcProtoDir "$User@${IP}:$TargetDir/"

    Write-Host "✅ Finished $IP" -ForegroundColor Green
}