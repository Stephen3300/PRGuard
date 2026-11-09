$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$destination = Join-Path $projectRoot 'data\raw\crc-py-dataset'
$repository = 'https://github.com/busraicoz/crc-py-dataset.git'

if (Test-Path -LiteralPath $destination) {
    $existing = Get-ChildItem -Force -Recurse -File -LiteralPath $destination
    if ($existing.Count -gt 0) {
        throw "Dataset target already contains files; refusing to overwrite: $destination"
    }
}

git clone --depth 1 $repository $destination
if ($LASTEXITCODE -ne 0) { throw "git clone failed: $LASTEXITCODE" }

$chunks = Get-ChildItem -File (Join-Path $destination 'data\train') -Filter 'chunk_*.json'
if ($chunks.Count -eq 0) { throw "No training chunk_*.json files found under $destination\data\train" }
Write-Output "Downloaded $($chunks.Count) CRC-PY data chunks to $destination"

