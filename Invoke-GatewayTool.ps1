[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('gateway.py','deploy.py','prepare-host.py')][string]$Tool,
    [string[]]$ToolArguments = @(),
    [string]$DeploymentPath = (Join-Path $env:USERPROFILE '.config\home-gateway\deployment.json')
)
. (Join-Path $PSScriptRoot 'Get-GatewayConfig.ps1') -DeploymentPath $DeploymentPath
if (-not $deployment.operator.WslDistribution) { throw 'Set operator.WslDistribution for the Windows command wrappers.' }
$linuxRepo = & wsl.exe -d $deployment.operator.WslDistribution -- wslpath -u ($repoRoot.Replace('\','/'))
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the operator repository path.' }
$linuxConfig = & wsl.exe -d $deployment.operator.WslDistribution -- wslpath -a ($DeploymentPath.Replace('\','/'))
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the private configuration path.' }
$arguments = @('-d',$deployment.operator.WslDistribution,'--','python3',($linuxRepo.Trim()+'/tools/'+$Tool),'--config',$linuxConfig.Trim()) + $ToolArguments
& wsl.exe @arguments
if ($LASTEXITCODE -ne 0) { throw "Gateway tool failed: $Tool (exit $LASTEXITCODE)" }
