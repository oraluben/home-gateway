param([string]$DeploymentPath = (Join-Path $env:USERPROFILE '.config\home-gateway\deployment.json'))
. (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'Get-GatewayConfig.ps1') -DeploymentPath $DeploymentPath
if ($deployment.host) {
    if ($deployment.host.backend -ne 'hyperv') { throw 'This command requires the hyperv backend.' }
    $settings = $deployment.host.hyperv
} else { $settings = $deployment.hyperv }
if (-not $settings) { throw 'Hyper-V settings are missing from the private deployment file.' }
