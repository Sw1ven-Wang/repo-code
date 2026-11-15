param(
    [string]$BaseImage = 'python@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb',
    [switch]$Rebuild
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$configPath = Join-Path $projectRoot 'examples/sandbox/validation-config.json'
$runtimeReport = Join-Path $projectRoot ('reports/capability-validation/setup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $runtimeReport -Force | Out-Null
& docker info --format '{{.ServerVersion}}'
if ($LASTEXITCODE -ne 0) { throw 'Docker Linux engine is unavailable. Start Docker Desktop, then retry.' }
if ($Rebuild -or -not (Test-Path -LiteralPath $configPath)) {
    & docker buildx build --load --tag repo-code-sandbox:1.5.1-validation --build-arg "BASE_IMAGE=$BaseImage" `
        --file (Join-Path $projectRoot 'examples/sandbox/Dockerfile.validation') `
        --metadata-file (Join-Path $runtimeReport 'image-build-metadata.json') (Join-Path $projectRoot 'examples/sandbox')
    if ($LASTEXITCODE -ne 0) { throw 'Sandbox image build failed.' }
    $imageRefs = & docker image inspect repo-code-sandbox:1.5.1-validation --format '{{json .RepoDigests}}' | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect the built sandbox image.' }
    $imageReference = $imageRefs | Where-Object { $_ -match '^repo-code-sandbox@sha256:[0-9a-f]{64}$' } | Select-Object -First 1
    if (-not $imageReference) { throw 'Docker did not expose a repository digest. Use a local registry and a real repository@sha256 reference; do not substitute an image ID.' }
    $sandboxProfile = [ordered]@{image=$imageReference; base_image=$BaseImage; provider='openai'; model='gpt-6.1-sol'; reasoning_effort='medium'; base_url='https://modelflare.dev/v1'; api_key_env='OPENAI_API_KEY'}
    [IO.File]::WriteAllText($configPath, ($sandboxProfile | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
}
$sandboxProfile = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
& docker image inspect $sandboxProfile.image --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) { throw 'The pinned image is not present. Rerun this script with -Rebuild.' }
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Install the project models/dev dependencies in .venv first.' }
$doctorOutput = & $pythonPath -m repo_code --state-root (Join-Path $projectRoot '.repo-code/docker-doctor') --sandbox docker --image $sandboxProfile.image doctor
$doctorExitCode = $LASTEXITCODE
[IO.File]::WriteAllText((Join-Path $runtimeReport 'doctor.json'), ($doctorOutput -join [Environment]::NewLine), [Text.UTF8Encoding]::new($false))
if ($doctorExitCode -ne 0) { throw 'The actual sandbox doctor probe failed.' }
Get-Content -LiteralPath (Join-Path $runtimeReport 'doctor.json')
Write-Output "Pinned sandbox profile: $configPath"

