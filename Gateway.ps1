[CmdletBinding()]
param(
    [ValidateSet('status','logs-vpn','logs-clash','logs-system','logs-subscription','logs-deployment','update-subscription','start','stop','stop-vpn','stop-clash','retry-vpn','retry-clash','retry-dns')]
    [string]$Action = 'status',
    [string]$DeploymentPath = (Join-Path $env:USERPROFILE '.config\home-gateway\deployment.json')
)
& (Join-Path $PSScriptRoot 'Invoke-GatewayTool.ps1') -Tool gateway.py -ToolArguments @($Action) -DeploymentPath $DeploymentPath
