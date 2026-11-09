$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    uv venv (Join-Path $projectRoot '.venv') --python 3.11
    if ($LASTEXITCODE -ne 0) { throw "uv venv failed: $LASTEXITCODE" }
}

uv pip install --python $python torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu128
if ($LASTEXITCODE -ne 0) { throw "CUDA PyTorch installation failed: $LASTEXITCODE" }

uv pip install --python $python -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw "PRGuard dependency installation failed: $LASTEXITCODE" }

Write-Output "Training environment installed in $projectRoot\.venv"

