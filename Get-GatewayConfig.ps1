param([string]$DeploymentPath = (Join-Path $env:USERPROFILE '.config\home-gateway\deployment.json'))
$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
$wslSelection = if ($env:HOME_GATEWAY_WSL_DISTRIBUTION) { @('-d', $env:HOME_GATEWAY_WSL_DISTRIBUTION) } else { @() }
if (Test-Path -LiteralPath $DeploymentPath) {
    $deployment = Get-Content -LiteralPath $DeploymentPath -Raw | ConvertFrom-Json
} else {
    # A new operator only needs pass; this renders metadata without a yadm checkout.
    $linuxRepo = & wsl.exe @wslSelection --exec wslpath -u ($repoRoot.Replace('\','/'))
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the repository path in the default WSL distribution.' }
    $linuxConfig = & wsl.exe @wslSelection --exec wslpath -a ($DeploymentPath.Replace('\','/'))
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the management metadata path.' }
    $result = & wsl.exe @wslSelection --exec python3 ($linuxRepo.Trim()+'/tools/config.py') --write-operator $linuxConfig.Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Cannot render operator metadata; unlock the deployment entry in pass.' }
    $deployment = Get-Content -LiteralPath $DeploymentPath -Raw | ConvertFrom-Json
}
$connection = $deployment.connection
$keyPath = if ($connection.KeyPath) { $connection.KeyPath } else { Join-Path $env:USERPROFILE '.ssh\home-gateway_ed25519' }
$knownHosts = if ($connection.KnownHostsPath) { $connection.KnownHostsPath } else { Join-Path $env:USERPROFILE '.ssh\home-gateway_known_hosts' }
$distribution = if ($env:HOME_GATEWAY_WSL_DISTRIBUTION) { $env:HOME_GATEWAY_WSL_DISTRIBUTION } else { $deployment.operator.WslDistribution }
$wslSelection = if ($distribution) { @('-d', $distribution) } else { @() }
$stateDirectory = Join-Path $env:USERPROFILE '.local\state\home-gateway'
$null = New-Item -ItemType Directory -Path $stateDirectory -Force
