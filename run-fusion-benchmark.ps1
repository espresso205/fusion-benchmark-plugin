param(
    [Parameter(Mandatory = $true)][string]$Config,
    [switch]$PreflightOnly
)
$ErrorActionPreference = 'Stop'
$resolvedConfig = (Resolve-Path -LiteralPath $Config).Path
$trialSettings = Get-Content -LiteralPath $resolvedConfig -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not (Test-Path -LiteralPath $trialSettings.python -PathType Leaf)) {
    throw 'The configured Python executable does not exist.'
}
Push-Location $PSScriptRoot
try {
    if ($PreflightOnly) {
        & $trialSettings.python -m fusion_pipeline run --config $resolvedConfig --preflight-only
    } else {
        & $trialSettings.python -m fusion_pipeline run --config $resolvedConfig
    }
    $runnerExit = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $runnerExit
