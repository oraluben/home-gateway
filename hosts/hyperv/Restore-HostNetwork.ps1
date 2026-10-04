#Requires -RunAsAdministrator
[CmdletBinding()]
param([switch]$Watchdog, [int]$WorkerProcessId = 0)
$ErrorActionPreference = 'Stop'
Import-Module Hyper-V
. (Join-Path $PSScriptRoot 'Get-HyperVConfig.ps1')
$backupPath = Join-Path $stateDirectory 'host-network-before.json'
if (-not (Test-Path -LiteralPath $backupPath)) { throw 'The original host-network snapshot is missing.' }
$backup = Get-Content -LiteralPath $backupPath -Raw | ConvertFrom-Json
$recoveryPath = Join-Path $stateDirectory 'network-recovery.json'
function Test-OriginalNetwork {
    $originalAddress = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $backup.IPv4[0].IPAddress -ErrorAction SilentlyContinue
    if (-not $originalAddress) { return $false }
    $route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -InterfaceIndex $originalAddress.InterfaceIndex -ErrorAction SilentlyContinue |
        Where-Object { $_.NextHop -eq $backup.Gateway[0] }
    if (-not $route) { return $false }
    $ping = New-Object System.Net.NetworkInformation.Ping
    try { return $ping.Send($backup.Gateway[0], 1000).Status -eq 'Success' } catch { return $false } finally { $ping.Dispose() }
}
if ($Watchdog) {
    $deadline = (Get-Date).AddSeconds(120)
    do {
        Start-Sleep -Seconds 3
        $statusPath = Join-Path $stateDirectory 'initialize-vm.json'
        if (Test-Path -LiteralPath $statusPath) {
            try { $setupStatus = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json } catch { $setupStatus = $null }
            if ($setupStatus.State -in @('complete', 'failed') -and (Test-OriginalNetwork)) {
                @{ State = 'not-needed'; UpdatedAt = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $recoveryPath -Encoding UTF8
                exit 0
            }
        }
    } until ((Get-Date) -ge $deadline)
    if (Test-OriginalNetwork) {
        @{ State = 'not-needed'; UpdatedAt = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $recoveryPath -Encoding UTF8
        exit 0
    }
    if ($WorkerProcessId -gt 0) { Stop-Process -Id $WorkerProcessId -Force -ErrorAction SilentlyContinue }
}
try {
    $switch = Get-VMSwitch -Name $settings.SwitchName -ErrorAction SilentlyContinue
    $adapter = Get-NetAdapter -Physical | Where-Object { [guid]$_.InterfaceGuid -eq [guid]$backup.AdapterGuid }
    if (-not $adapter) { throw 'The original physical adapter cannot be found.' }
    if ($switch) {
        if ([string]$switch.SwitchType -ne 'External' -or $switch.NetAdapterInterfaceDescription -ne $adapter.InterfaceDescription) {
            throw 'The named switch does not match this setup; refusing to remove it.'
        }
        $vm = Get-VM -Name $settings.VMName -ErrorAction SilentlyContinue
        if ($vm -and [string]$vm.State -ne 'Off') { Stop-VM -VM $vm -TurnOff }
        Remove-VMSwitch -Name $settings.SwitchName -Force
        Start-Sleep -Seconds 3
    }
    if ($backup.DHCP -eq 'Enabled') {
        Set-NetIPInterface -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -Dhcp Enabled
        $renew = Start-Process -FilePath ipconfig.exe -ArgumentList @('/renew', ('"' + $adapter.Name + '"')) -WindowStyle Hidden -PassThru
        if (-not $renew.WaitForExit(20000)) { Stop-Process -Id $renew.Id -Force -ErrorAction SilentlyContinue }
    } else {
        $existing = Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -IPAddress $backup.IPv4[0].IPAddress -ErrorAction SilentlyContinue
        if (-not $existing) {
            $null = New-NetIPAddress -InterfaceIndex $adapter.ifIndex -IPAddress $backup.IPv4[0].IPAddress -PrefixLength $backup.IPv4[0].PrefixLength -DefaultGateway $backup.Gateway[0]
        }
    }
    if ($backup.DNS.Count -gt 0) { Set-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -ServerAddresses $backup.DNS }
    @{ State = 'restored'; Verified = (Test-OriginalNetwork); UpdatedAt = (Get-Date).ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $recoveryPath -Encoding UTF8
} catch {
    @{ State = 'failed'; Error = $_.Exception.Message; UpdatedAt = (Get-Date).ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $recoveryPath -Encoding UTF8
    throw
}
