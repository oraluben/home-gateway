[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('gateway.py','deploy.py','prepare-host.py','tailscale.py')][string]$Tool,
    [string[]]$ToolArguments = @(),
    [string]$DeploymentPath = (Join-Path $env:USERPROFILE '.config\home-gateway\deployment.json')
)
. (Join-Path $PSScriptRoot 'Get-GatewayConfig.ps1') -DeploymentPath $DeploymentPath
$linuxRepo = & wsl.exe @wslSelection --exec wslpath -u ($repoRoot.Replace('\','/'))
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the operator repository path.' }
$linuxConfig = & wsl.exe @wslSelection --exec wslpath -a ($DeploymentPath.Replace('\','/'))
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the private configuration path.' }
$arguments = @($wslSelection) + @('--exec','python3',($linuxRepo.Trim()+'/tools/'+$Tool),'--config',$linuxConfig.Trim()) + $ToolArguments
& wsl.exe @arguments
if ($LASTEXITCODE -ne 0) { throw "Gateway tool failed: $Tool (exit $LASTEXITCODE)" }
if ($Tool -eq 'deploy.py' -and $ToolArguments -notcontains '--validate-only') {
    & wsl.exe @wslSelection --exec python3 ($linuxRepo.Trim()+'/tools/config.py') --config $linuxConfig.Trim() --write-operator $linuxConfig.Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Deployment completed, but local management metadata could not be refreshed.' }
}
