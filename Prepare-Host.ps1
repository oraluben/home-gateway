[CmdletBinding()]
param([switch]$Apply,
      [string]$DeploymentPath = (Join-Path $env:USERPROFILE '.config\home-gateway\deployment.json'))
$arguments = @()
if ($Apply) { $arguments += '--apply' }
& (Join-Path $PSScriptRoot 'Invoke-GatewayTool.ps1') -Tool prepare-host.py -ToolArguments $arguments -DeploymentPath $DeploymentPath
