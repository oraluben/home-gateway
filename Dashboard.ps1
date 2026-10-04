[CmdletBinding()]
param([ValidateSet('open','status','close','copy-key')][string]$Action='open', [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Get-GatewayConfig.ps1')
$statePath = Join-Path $stateDirectory 'dashboard-tunnel.json'
$port = 19090
$url = 'http://127.0.0.1:' + $port + '/ui/'
$tunnel = $null
function Copy-DashboardKey {
    $dashboardKey = & ssh.exe -i $keyPath -o ('UserKnownHostsFile=' + $knownHosts) -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=5 ($connection.User + '@' + $connection.Address) 'sudo python3 /opt/home-gateway/dashboard-key.py'
    if ($LASTEXITCODE -ne 0) { throw 'Unable to copy the dashboard access key.' }
    if ($dashboardKey) { Set-Clipboard -Value ($dashboardKey -join "`n").TrimEnd([char]13,[char]10) }
    Write-Host 'Dashboard access key copied. On first use, paste it into the Secret field.'
}
if ($Action -eq 'copy-key') { Copy-DashboardKey; exit 0 }
if (Test-Path -LiteralPath $statePath) {
    $record = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $candidate = Get-Process -Id $record.ProcessId -ErrorAction SilentlyContinue
    if ($candidate -and $candidate.ProcessName -eq 'ssh' -and
        [math]::Abs(($candidate.StartTime - [datetime]$record.ProcessStartedAt).TotalSeconds) -lt 2) {
        $tunnel = $candidate
    }
}
if ($Action -eq 'close') {
    if ($tunnel) { Stop-Process -Id $tunnel.Id }
    Write-Host 'Dashboard connection closed. Gateway networking continues normally.'
    exit 0
}
if ($Action -eq 'status') {
    if ($tunnel) { Write-Host ('Dashboard: ' + $url + ' (SSH process ' + $tunnel.Id + ')') }
    else { Write-Host 'Dashboard connection is closed. Run Dashboard.ps1 to open it.' }
    exit 0
}
if (-not $tunnel) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw 'Local port 19090 is occupied by another application; it will not be replaced.'
    }
    $key = $keyPath
    $arguments = @('-N','-L',('127.0.0.1:' + $port + ':' + $connection.Address + ':9090'),'-i',('"' + $key + '"'),
        '-o',('"UserKnownHostsFile=' + $knownHosts + '"'),'-o','StrictHostKeyChecking=yes',
        '-o','BatchMode=yes','-o','ConnectTimeout=5','-o','ExitOnForwardFailure=yes',
        '-o','ServerAliveInterval=15','-o','ServerAliveCountMax=2',($connection.User + '@' + $connection.Address))
    $tunnel = Start-Process -FilePath ssh.exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ($tunnel.HasExited) { throw 'The SSH dashboard connection failed.' }
        try {
            $httpStatus = & curl.exe --noproxy '*' --connect-timeout 1 --max-time 2 --silent --output NUL --write-out '%{http_code}' $url
            if ($LASTEXITCODE -eq 0 -and $httpStatus -eq '200') { $ready = $true; break }
        } catch {}
        Start-Sleep -Milliseconds 300
    }
    if (-not $ready) { Stop-Process -Id $tunnel.Id; throw 'The dashboard did not become available.' }
    [ordered]@{ProcessId=$tunnel.Id;ProcessStartedAt=$tunnel.StartTime.ToString('o');Address=$connection.Address;Url=$url} |
        ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
}
Write-Host ('Dashboard: ' + $url)
Write-Host 'AI group: US node. Main proxy group: Hong Kong node. Changes are saved automatically.'
if (-not $NoBrowser) { Copy-DashboardKey; Start-Process $url }
