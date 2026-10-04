#Requires -RunAsAdministrator
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
Import-Module Hyper-V
. (Join-Path $PSScriptRoot 'Get-HyperVConfig.ps1')
$null = New-Item -ItemType Directory -Path $stateDirectory -Force
$statusPath = Join-Path $stateDirectory 'initialize-vm.json'
$diskPath = Join-Path $stateDirectory 'vm\gateway.vhdx'
$seedPath = Join-Path $stateDirectory 'vm\seed.iso'
$createdSwitch = $false
function Write-SetupStatus($state, $detail) {
    [ordered]@{ State = $state; UpdatedAt = (Get-Date).ToString('o'); Detail = $detail } |
        ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $statusPath -Encoding UTF8
}
try {
    Write-SetupStatus 'running' 'Checking VM prerequisites'
    if (-not (Test-Path -LiteralPath $diskPath) -or -not (Test-Path -LiteralPath $seedPath)) {
        throw 'The prepared disk or seed ISO is missing.'
    }
    if (Get-VM -Name $settings.VMName -ErrorAction SilentlyContinue) {
        throw 'HomeGateway already exists. Refusing to replace an existing VM.'
    }
    $adapter = Get-NetAdapter -Physical | Where-Object { [guid]$_.InterfaceGuid -eq [guid]$settings.PhysicalAdapterGuid }
    if (-not $adapter -or $adapter.Status -ne 'Up') { throw 'The configured physical adapter is not connected.' }
    $ipConfig = Get-NetIPConfiguration -InterfaceIndex $adapter.ifIndex
    $ipInterface = Get-NetIPInterface -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4
    $backup = [ordered]@{
        CapturedAt = (Get-Date).ToString('o')
        AdapterName = $adapter.Name
        AdapterGuid = $adapter.InterfaceGuid.ToString()
        DHCP = [string]$ipInterface.Dhcp
        IPv4 = @($ipConfig.IPv4Address | Select-Object IPAddress, PrefixLength, PrefixOrigin)
        Gateway = @($ipConfig.IPv4DefaultGateway.NextHop)
        DNS = @($ipConfig.DNSServer.ServerAddresses)
    }
    $backup | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $stateDirectory 'host-network-before.json') -Encoding UTF8
    $switch = Get-VMSwitch -Name $settings.SwitchName -ErrorAction SilentlyContinue
    if ($switch) {
        if ([string]$switch.SwitchType -ne 'External' -or $switch.NetAdapterInterfaceDescription -ne $adapter.InterfaceDescription) {
            throw 'The named switch exists but is not attached to the expected physical adapter.'
        }
    } else {
        $existingExternal = Get-VMSwitch -SwitchType External | Where-Object { $_.NetAdapterInterfaceDescription -eq $adapter.InterfaceDescription }
        if ($existingExternal) { throw 'This adapter already has another external switch. Review settings before continuing.' }
        $recoveryScript = Join-Path $PSScriptRoot 'Restore-HostNetwork.ps1'
        $null = Start-Process -FilePath powershell.exe -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $recoveryScript + '"'), '-Watchdog', '-WorkerProcessId', $PID) -WindowStyle Hidden -PassThru
        Write-SetupStatus 'running' 'Creating external switch; waiting for Windows network recovery'
        $switch = New-VMSwitch -Name $settings.SwitchName -NetAdapterName $adapter.Name -AllowManagementOS $true
        $createdSwitch = $true
        $deadline = (Get-Date).AddSeconds(45)
        do {
            Start-Sleep -Seconds 2
            $hostNetwork = Get-NetIPConfiguration | Where-Object {
                $_.InterfaceAlias -eq ('vEthernet (' + $settings.SwitchName + ')') -and
                $_.IPv4Address.IPAddress -contains $backup.IPv4[0].IPAddress -and
                $_.IPv4DefaultGateway.NextHop -contains $backup.Gateway[0]
            }
        } until ($hostNetwork -or (Get-Date) -ge $deadline)
        if (-not $hostNetwork) { throw 'Windows did not recover its original IPv4 and gateway within 45 seconds.' }
    }
    Write-SetupStatus 'running' 'Creating and starting HomeGateway'
    $disk = Get-VHD -Path $diskPath
    if ($disk.Size -lt ([int64]$settings.DiskSizeGB * 1GB)) {
        Resize-VHD -Path $diskPath -SizeBytes ([int64]$settings.DiskSizeGB * 1GB)
    }
    $vm = New-VM -Name $settings.VMName -Generation 2 -MemoryStartupBytes ([int64]$settings.MemoryMB * 1MB) -VHDPath $diskPath -Path (Join-Path $stateDirectory 'vm') -SwitchName $settings.SwitchName
    Set-VMProcessor -VM $vm -Count $settings.ProcessorCount
    Set-VMMemory -VM $vm -DynamicMemoryEnabled $false
    Set-VMNetworkAdapter -VM $vm -StaticMacAddress $settings.MacAddress
    Set-VMFirmware -VM $vm -EnableSecureBoot On -SecureBootTemplate MicrosoftUEFICertificateAuthority
    Set-VM -VM $vm -AutomaticStartAction Start -AutomaticStartDelay 15 -AutomaticStopAction ShutDown -AutomaticCheckpointsEnabled $false
    $null = Add-VMDvdDrive -VM $vm -Path $seedPath
    $bootDisk = Get-VMHardDiskDrive -VM $vm | Select-Object -First 1
    Set-VMFirmware -VM $vm -FirstBootDevice $bootDisk
    Start-VM -VM $vm
    Write-SetupStatus 'complete' ([ordered]@{
        VMName = $vm.Name
        SwitchName = $switch.Name
        MacAddress = $settings.MacAddress
        IPv4 = @((Get-VMNetworkAdapter -VM $vm).IPAddresses)
    })
} catch {
    $failure = $_.Exception.Message
    $rollback = 'No external switch rollback needed'
    if ($createdSwitch) {
        try {
            & (Join-Path $PSScriptRoot 'Restore-HostNetwork.ps1')
            $rollback = 'Restored the original physical network'
        } catch { $rollback = 'Switch rollback failed: ' + $_.Exception.Message }
    }
    Write-SetupStatus 'failed' ([ordered]@{ Error = $failure; Rollback = $rollback })
    throw
}
