param(
    [ValidateSet('start', 'stop')]
    [string]$Action = 'start',
    [switch]$NoAdminPrompt
)

$launcherOptions = @($Action)
if ($NoAdminPrompt) {
    $launcherOptions += '--no-admin-prompt'
}
& (Join-Path $PSScriptRoot 'local.cmd') @launcherOptions
exit $LASTEXITCODE
