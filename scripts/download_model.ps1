$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$destination = Join-Path $projectRoot 'models\base\Qwen3-1.7B'
$hfCli = Join-Path $projectRoot '.venv\Scripts\hf.exe'

if (Test-Path -LiteralPath $destination) {
    $existing = Get-ChildItem -Force -Recurse -File -LiteralPath $destination
    if ($existing.Count -gt 0) { throw "Model target already contains files; refusing to overwrite: $destination" }
}
if (-not (Test-Path -LiteralPath $hfCli)) {
    throw "Hugging Face CLI not found at $hfCli. Run scripts/install_gpu_env.ps1 first."
}

& $hfCli download Qwen/Qwen3-1.7B --local-dir $destination
if ($LASTEXITCODE -ne 0) { throw "Model download failed: $LASTEXITCODE" }
Write-Output "Downloaded Qwen3-1.7B to $destination"

