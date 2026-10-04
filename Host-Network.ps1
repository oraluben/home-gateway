[CmdletBinding()]
param(
    [switch]$EnableLogging,
    [switch]$RestoreLogging,
    [string]$OutputDirectory,
    [string]$VMName = 'HomeGateway'
)
$ErrorActionPreference = 'Stop'
if ($EnableLogging -and $RestoreLogging) { throw 'Choose enable or restore logging, not both.' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $env:USERPROFILE '.local/state/home-gateway/network' }
$null = New-Item -ItemType Directory -Path $OutputDirectory -Force
$logName = 'Microsoft-Windows-Dhcp-Client/Operational'
$baseline = Join-Path $OutputDirectory 'logging-before.json'
$utf8 = [Text.UTF8Encoding]::new($false)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($EnableLogging -or $RestoreLogging) {
    if (-not $isAdmin) { throw 'Changing DHCP logging requires an administrator PowerShell window.' }
    if ($EnableLogging) {
        if (-not (Test-Path -LiteralPath $baseline)) {
            $before = Get-WinEvent -ListLog $logName
            [IO.File]::WriteAllText($baseline, ([ordered]@{
                Enabled = $before.IsEnabled; MaximumSize = $before.MaximumSizeInBytes
            } | ConvertTo-Json), $utf8)
        }
        & wevtutil.exe sl $logName /e:true /ms:4194304
    } else {
        if (-not (Test-Path -LiteralPath $baseline)) { throw 'The original logging settings are missing.' }
        $before = Get-Content -LiteralPath $baseline -Raw | ConvertFrom-Json
        & wevtutil.exe sl $logName ('/e:' + ([string][bool]$before.Enabled).ToLowerInvariant()) ('/ms:' + $before.MaximumSize)
    }
    if ($LASTEXITCODE -ne 0) { throw 'Windows could not change the DHCP event log settings.' }
}

$report = [ordered]@{ CapturedAt = (Get-Date).ToString('o'); Elevated = $isAdmin }
function Read-NetworkSection([string]$Name, [scriptblock]$Read) {
    try { $script:report[$Name] = @(& $Read) }
    catch { $script:report[$Name] = @{ Error = $_.Exception.Message } }
}
Read-NetworkSection 'Boot' { Get-CimInstance Win32_OperatingSystem | Select-Object LastBootUpTime }
Read-NetworkSection 'Adapters' {
    Get-NetAdapter | Select-Object Name,InterfaceGuid,ifIndex,Status,LinkSpeed,MacAddress,
        DriverVersion,DriverProvider,DriverFileName,DriverDate
}
Read-NetworkSection 'Addresses' {
    Get-NetIPAddress | Select-Object InterfaceAlias,InterfaceIndex,AddressFamily,IPAddress,
        PrefixLength,PrefixOrigin,SuffixOrigin,AddressState
}
Read-NetworkSection 'Interfaces' {
    Get-NetIPInterface | Select-Object InterfaceAlias,InterfaceIndex,AddressFamily,Dhcp,ConnectionState,InterfaceMetric
}
Read-NetworkSection 'DefaultRoutes' {
    Get-NetRoute | Where-Object { $_.DestinationPrefix -in '0.0.0.0/0','::/0' } |
        Select-Object InterfaceAlias,InterfaceIndex,DestinationPrefix,NextHop,RouteMetric
}
Read-NetworkSection 'DNS' { Get-DnsClientServerAddress | Select-Object InterfaceAlias,AddressFamily,ServerAddresses }
Read-NetworkSection 'DHCPLeases' {
    Get-CimInstance Win32_NetworkAdapterConfiguration | Where-Object { $_.IPEnabled } |
        Select-Object Description,MACAddress,DHCPEnabled,DHCPServer,DHCPLeaseObtained,DHCPLeaseExpires,
            IPAddress,DefaultIPGateway
}
Read-NetworkSection 'Neighbors' {
    Get-NetNeighbor -AddressFamily IPv4 | Select-Object InterfaceAlias,IPAddress,LinkLayerAddress,State
}
Read-NetworkSection 'AdapterCounters' {
    Get-NetAdapterStatistics | Select-Object Name,ReceivedPacketErrors,OutboundPacketErrors,
        ReceivedDiscardedPackets,OutboundDiscardedPackets
}
Read-NetworkSection 'Services' {
    Get-Service Dhcp,nsi,Netprofm,TermService,vmms | Select-Object Name,Status,StartType
}
Read-NetworkSection 'RDPListeners' {
    Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -eq 3389 } |
        Select-Object LocalAddress,LocalPort,OwningProcess
}
Read-NetworkSection 'RDPFirewall' {
    Get-NetFirewallRule | Where-Object { $_.Name -like 'RemoteDesktop*' } |
        Select-Object Name,Enabled,Profile,Direction,Action
}
Read-NetworkSection 'VM' {
    Get-VM -Name $VMName | Select-Object Name,State,Status,Uptime,AutomaticStartAction,AutomaticStartDelay,AutomaticStopAction
}
Read-NetworkSection 'Switches' {
    Get-VMSwitch | Select-Object Name,SwitchType,NetAdapterInterfaceDescription,AllowManagementOS
}
Read-NetworkSection 'HostVirtualAdapters' {
    Get-VMNetworkAdapter -ManagementOS | Select-Object Name,SwitchName,MacAddress,DhcpGuard,RouterGuard,MacAddressSpoofing,Status
}
Read-NetworkSection 'GuestVirtualAdapters' {
    Get-VMNetworkAdapter -VMName $VMName | Select-Object Name,SwitchName,MacAddress,DhcpGuard,RouterGuard,MacAddressSpoofing,Status,IPAddresses
}
Read-NetworkSection 'VLANs' {
    # Hyper-V settings contain cyclic ParentAdapter references; keep only useful fields.
    Get-VMNetworkAdapterVlan -ManagementOS | Select-Object @{N='Scope';E={'Host'}},
        @{N='Adapter';E={$_.ParentAdapter.Name}}, @{N='Switch';E={$_.ParentAdapter.SwitchName}},
        OperationMode,AccessVlanId,NativeVlanId,AllowedVlanIdListString
    Get-VMNetworkAdapterVlan -VMName $VMName | Select-Object @{N='Scope';E={'Guest'}},
        @{N='Adapter';E={$_.ParentAdapter.Name}}, @{N='Switch';E={$_.ParentAdapter.SwitchName}},
        OperationMode,AccessVlanId,NativeVlanId,AllowedVlanIdListString
}
Read-NetworkSection 'DHCPLogSettings' {
    Get-WinEvent -ListLog 'Microsoft-Windows-Dhcp-Client/Admin',$logName |
        Select-Object LogName,IsEnabled,MaximumSizeInBytes,LogMode,RecordCount
}
$since = (Get-Date).AddDays(-2)
foreach ($eventLog in @('Microsoft-Windows-Dhcp-Client/Admin', $logName,
    'Microsoft-Windows-NCSI/Operational', 'Microsoft-Windows-NetworkProfile/Operational')) {
    Read-NetworkSection $eventLog {
        Get-WinEvent -FilterHashtable @{ LogName = $eventLog; StartTime = $since } -MaxEvents 250 -ErrorAction SilentlyContinue |
            Select-Object TimeCreated,Id,ProviderName,LevelDisplayName,Message
    }
}
Read-NetworkSection 'SystemEvents' {
    Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = $since;
        ProviderName = @('Microsoft-Windows-Kernel-Boot','Microsoft-Windows-Kernel-Power',
            'Microsoft-Windows-Dhcp-Client','Microsoft-Windows-TCPIP','Microsoft-Windows-Hyper-V-VmSwitch','User32') } |
        Where-Object { $_.ProviderName -ne 'Microsoft-Windows-Hyper-V-VmSwitch' -or $_.Id -in 15,23,24 } |
        Select-Object -First 250 TimeCreated,Id,ProviderName,LevelDisplayName,Message
}
$outputPath = Join-Path $OutputDirectory ('network-' + (Get-Date).ToString('yyyyMMdd-HHmmss-fff') + '.json')
[IO.File]::WriteAllText($outputPath, ($report | ConvertTo-Json -Depth 7), $utf8)
Write-Output ('Network report: ' + $outputPath)
Write-Output ('DHCP detailed logging: ' + (Get-WinEvent -ListLog $logName).IsEnabled)
