[CmdletBinding()]
param([switch]$ValidateOnly, [string]$Restore)
. (Join-Path $PSScriptRoot 'Get-GatewayConfig.ps1')
$linuxRepo = & wsl.exe -d $deployment.operator.WslDistribution -- wslpath -u $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the operator repository path.' }
$arguments = @('-d',$deployment.operator.WslDistribution,'--','python3',($linuxRepo.Trim()+'/tools/deploy.py'))
if ($ValidateOnly) { $arguments += '--validate-only' }
if ($Restore) { $restorePath = (& wsl.exe -d $deployment.operator.WslDistribution -- wslpath -a $Restore).Trim(); $arguments += @('--restore', $restorePath) }
& wsl.exe @arguments
if ($LASTEXITCODE -ne 0) { throw 'Gateway deployment failed; check operator GPG access and gateway status.' }
