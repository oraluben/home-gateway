#Requires -RunAsAdministrator
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
Import-Module Hyper-V
. (Join-Path $PSScriptRoot 'Get-HyperVConfig.ps1')
$null = New-Item -ItemType Directory -Path $stateDirectory -Force
$vm = Get-VM -Name $settings.VMName
[ordered]@{
    UpdatedAt = (Get-Date).ToString('o')
    Name = $vm.Name
    State = [string]$vm.State
    Status = [string]$vm.Status
    Uptime = $vm.Uptime.ToString()
    IPv4 = @((Get-VMNetworkAdapter -VM $vm).IPAddresses | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' })
    MacAddress = (Get-VMNetworkAdapter -VM $vm).MacAddress
    WindowsNetwork = @(Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | Select-Object InterfaceAlias, @{N='IPv4';E={$_.IPv4Address.IPAddress}}, @{N='Gateway';E={$_.IPv4DefaultGateway.NextHop}})
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $stateDirectory 'vm-status.json') -Encoding UTF8
