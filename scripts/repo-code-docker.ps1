[CmdletBinding(PositionalBinding=$false)]
param(
    [string]$StateRoot,
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$RepoCodeArguments
)
$ErrorActionPreference='Stop'
$projectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$sandboxProfile=Get-Content -LiteralPath (Join-Path $projectRoot 'examples/sandbox/validation-config.json') -Raw | ConvertFrom-Json
if (-not $StateRoot) { $StateRoot=Join-Path $projectRoot '.repo-code/docker-runs' }
$pythonPath=Join-Path $projectRoot '.venv/Scripts/python.exe'
& $pythonPath -m repo_code --state-root $StateRoot --sandbox docker --image $sandboxProfile.image `
    --provider $sandboxProfile.provider --model $sandboxProfile.model --base-url $sandboxProfile.base_url `
    --api-key-env $sandboxProfile.api_key_env --reasoning-effort $sandboxProfile.reasoning_effort @RepoCodeArguments
exit $LASTEXITCODE
