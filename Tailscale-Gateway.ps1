[CmdletBinding()]
param(
    [ValidateSet('install','login','status','netcheck','logs')][string]$Action = 'status',
    [string]$DeploymentPath = (Join-Path $env:USERPROFILE '.config\home-gateway\deployment.json')
)
& (Join-Path $PSScriptRoot 'Invoke-GatewayTool.ps1') -Tool tailscale.py -ToolArguments @($Action) -DeploymentPath $DeploymentPath
