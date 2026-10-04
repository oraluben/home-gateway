[CmdletBinding()]
param(
    [ValidateSet('status','logs-vpn','logs-clash','logs-system','logs-subscription','update-subscription','start','stop','stop-vpn','stop-clash','retry-vpn','retry-clash','retry-dns')]
    [string]$Action = 'status'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Get-GatewayConfig.ps1')
$null = [System.Net.IPAddress]::Parse($connection.Address)
$commands = @{
    'status' = 'sudo python3 /opt/home-gateway/show-status.py'
    'logs-vpn' = 'sudo tail -n 60 /opt/home-gateway/data/logs/vpn.log'
    'logs-clash' = 'sudo tail -n 60 /opt/home-gateway/data/logs/clash.log'
    'logs-system' = 'sudo journalctl -u home-gateway -u home-gateway-dns --no-pager -n 60'
    'logs-subscription' = 'sudo journalctl -u home-gateway-subscription --no-pager -n 20'
    'update-subscription' = 'sudo systemctl start home-gateway-subscription; gateway_update_result=$?; sudo cat /opt/home-gateway/data/subscription-status.json; exit $gateway_update_result'
    'start' = 'sudo systemctl reset-failed home-gateway home-gateway-dns; sudo systemctl start home-gateway-dns home-gateway'
    'stop' = 'sudo systemctl stop home-gateway'
    'stop-vpn' = 'sudo systemctl is-active --quiet home-gateway && sudo touch /opt/home-gateway/data/stop-vpn'
    'stop-clash' = 'sudo systemctl is-active --quiet home-gateway && sudo touch /opt/home-gateway/data/stop-clash'
    'retry-vpn' = 'sudo systemctl is-active --quiet home-gateway && sudo touch /opt/home-gateway/data/retry-vpn'
    'retry-clash' = 'sudo systemctl is-active --quiet home-gateway && sudo touch /opt/home-gateway/data/retry-clash'
    'retry-dns' = 'sudo systemctl reset-failed home-gateway-dns; sudo systemctl restart home-gateway-dns'
}
& ssh.exe -i $keyPath -o ('UserKnownHostsFile=' + $knownHosts) -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=5 ($connection.User + '@' + $connection.Address) $commands[$Action]
if ($LASTEXITCODE -ne 0) { throw "Gateway command failed: $Action (exit $LASTEXITCODE)" }
