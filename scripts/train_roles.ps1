param(
    [int]$MaxSteps = 100,
    [int]$MaxLength = 512,
    [double]$LearningRate = 0.00005
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$trainer = Join-Path $PSScriptRoot 'train_qlora.py'
$logRoot = Join-Path $projectRoot 'outputs\logs\role-training'
$roles = @(
    'functional_reviewer',
    'refactoring_reviewer',
    'documentation_reviewer',
    'requirements_reviewer',
    'test_reviewer'
)

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python environment not found: $python"
}
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null

foreach ($roleId in $roles) {
    $outputDir = Join-Path $projectRoot "outputs\adapters\by-role\$roleId"
    if (Test-Path -LiteralPath $outputDir) {
        $existing = Get-ChildItem -LiteralPath $outputDir -Force -ErrorAction SilentlyContinue
        if ($existing) {
            throw "Refusing to overwrite existing output: $outputDir"
        }
    }
    $logPath = Join-Path $logRoot "$roleId.log"
    Write-Output "Training $roleId; log: $logPath"
    & $python $trainer `
        --role-id $roleId `
        --output-dir $outputDir `
        --max-steps $MaxSteps `
        --max-length $MaxLength `
        --learning-rate $LearningRate 2>&1 | Tee-Object -FilePath $logPath
    if ($LASTEXITCODE -ne 0) {
        throw "Training failed for $roleId with exit code $LASTEXITCODE. See $logPath"
    }
}

Write-Output "All five role adapters are saved under outputs\adapters\by-role."
