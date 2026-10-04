[CmdletBinding()]
param([switch]$ValidateOnly, [string]$Restore, [switch]$Rebuild,
      [string]$DeploymentPath = (Join-Path $env:USERPROFILE '.config\home-gateway\deployment.json'))
. (Join-Path $PSScriptRoot 'Get-GatewayConfig.ps1') -DeploymentPath $DeploymentPath
$arguments = @()
if ($ValidateOnly) { $arguments += '--validate-only' }
if ($Rebuild) { $arguments += '--rebuild' }
if ($Restore) {
    $restorePath = & wsl.exe @wslSelection --exec wslpath -a ($Restore.Replace('\','/'))
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the restore file path.' }
    $arguments += @('--restore', $restorePath.Trim())
}
& (Join-Path $PSScriptRoot 'Invoke-GatewayTool.ps1') -Tool deploy.py -ToolArguments $arguments -DeploymentPath $DeploymentPath
