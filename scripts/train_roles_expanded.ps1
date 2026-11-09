param(
    [int]$MaxSteps = 150,
    [int]$MaxLength = 512,
    [double]$LearningRate = 0.00005,
    [string]$DataTag = 'expanded',
    [string]$AdapterSet = 'by-role-expanded',
    [string[]]$Roles = @(
        'functional_reviewer',
        'refactoring_reviewer',
        'documentation_reviewer',
        'requirements_reviewer',
        'test_reviewer'
    )
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$trainer = Join-Path $PSScriptRoot 'train_qlora.py'
$dataRoot = Join-Path $projectRoot "data\processed\$DataTag"
$adapterRoot = Join-Path $projectRoot "outputs\adapters\$AdapterSet"
$logRoot = Join-Path $projectRoot "outputs\logs\role-training-$DataTag"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python environment not found: $python"
}
foreach ($split in @('train', 'validation')) {
    if (-not (Test-Path -LiteralPath (Join-Path $dataRoot "$split.jsonl"))) {
        throw "Expanded $split data is missing under $dataRoot"
    }
}
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null

foreach ($roleId in $roles) {
    $outputDir = Join-Path $adapterRoot $roleId
    if (Test-Path -LiteralPath $outputDir) {
        $existing = Get-ChildItem -LiteralPath $outputDir -Force -ErrorAction SilentlyContinue
        if ($existing) {
            throw "Refusing to overwrite existing candidate adapter: $outputDir"
        }
    }
    $logPath = Join-Path $logRoot "$roleId.log"
    Write-Output "Training expanded $roleId; log: $logPath"
    $trainFile = Join-Path $dataRoot 'train.jsonl'
    $evalFile = Join-Path $dataRoot 'validation.jsonl'
    $arguments = @(
        $trainer,
        '--role-id', $roleId,
        '--train-file', $trainFile,
        '--eval-file', $evalFile,
        '--output-dir', $outputDir,
        '--max-steps', "$MaxSteps",
        '--max-length', "$MaxLength",
        '--learning-rate', "$LearningRate"
    )
    & $python @arguments 2>&1 | Tee-Object -FilePath $logPath
    if ($LASTEXITCODE -ne 0) {
        throw "Training failed for $roleId with exit code $LASTEXITCODE. See $logPath"
    }
}

Write-Output "Expanded role adapters are saved under $adapterRoot"
