# Optional Windows recovery access, independent of the gateway VM.
[CmdletBinding()]
param([ValidateSet('Install','Login','Status')][string]$Action = 'Status')
$ErrorActionPreference = 'Stop'
$binary = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
$versions = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'hosts\tailscale-versions.json') -Raw | ConvertFrom-Json

if ($Action -eq 'Install') {
    $admin = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (!$admin) {
        $arguments = @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+$PSCommandPath+'"'),'-Action','Install')
        $process = Start-Process -FilePath (Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe') -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -PassThru -Wait
        if ($process.ExitCode -ne 0) { throw 'Elevated Tailscale installation failed. Inspect the installation log in ProgramData\home-gateway\tailscale.' }
        Write-Output 'Windows Tailscale installed with unattended management access. Run this script with -Action Login.'
        return
    }
    $directory = Join-Path $env:ProgramData 'home-gateway\tailscale'
    $null = New-Item -ItemType Directory -Force -Path $directory
    $receipt = Join-Path $directory 'installed.json'
    if (Test-Path -LiteralPath $binary) {
        if (!(Test-Path -LiteralPath $receipt)) { throw 'Existing Windows Tailscale is not managed by this project; inspect it manually.' }
        $previous = Get-Content -LiteralPath $receipt -Raw | ConvertFrom-Json
        $installed = @(& $binary version)[0]
        if ($previous.role -ne 'management' -or $previous.version -ne $versions.windows -or $installed -ne $versions.windows) { throw 'Existing Windows Tailscale version or receipt differs; upgrade explicitly.' }
        & $binary set --auto-update=false
        if ($LASTEXITCODE -ne 0) { throw 'Cannot retain the pinned Windows Tailscale version.' }
        Write-Output 'Windows Tailscale is already installed; routing and DNS preferences preserved.'
        return
    }
    $filename = 'tailscale-setup-'+$versions.windows+'-amd64.msi'
    $msi = Join-Path $directory $filename
    if (!(Test-Path -LiteralPath $msi)) { Invoke-WebRequest -UseBasicParsing ('https://pkgs.tailscale.com/stable/'+$filename) -OutFile $msi -TimeoutSec 120 }
    if ((Get-FileHash -LiteralPath $msi -Algorithm SHA256).Hash.ToLowerInvariant() -ne $versions.windows_sha256) { throw 'Tailscale MSI checksum mismatch.' }
    $signature = Get-AuthenticodeSignature -LiteralPath $msi
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch '(^|, )CN=Tailscale Inc\.(,|$)') { throw 'Tailscale MSI has an unexpected publisher or invalid signature.' }
    $log = Join-Path $directory 'install.log'
    $arguments = @('/i',('"'+$msi+'"'),'/qn','/norestart','/L*v',('"'+$log+'"'))
    $process = Start-Process -FilePath (Join-Path $env:WINDIR 'System32\msiexec.exe') -ArgumentList $arguments -WindowStyle Hidden -PassThru -Wait
    if ($process.ExitCode -notin @(0,3010)) { throw ('Tailscale MSI failed: '+$process.ExitCode+'; log: '+$log) }
    if (!(Test-Path -LiteralPath $binary)) { throw 'Tailscale binary is missing after installation.' }
    & $binary set --accept-dns=false --accept-routes=false --advertise-exit-node=false --advertise-routes= --exit-node= --unattended=true --auto-update=false
    if ($LASTEXITCODE -ne 0) { throw 'Tailscale management preferences could not be applied.' }
    @{version=$versions.windows;role='management';rebootRequested=($process.ExitCode -eq 3010)} | ConvertTo-Json | Set-Content -LiteralPath $receipt -Encoding UTF8
    Write-Output 'Windows Tailscale installed. Browser login is required.'
    return
}
if (!(Test-Path -LiteralPath $binary)) { throw 'Install Windows Tailscale first.' }
if ($Action -eq 'Login') {
    & $binary up --accept-dns=false --accept-routes=false --advertise-exit-node=false --advertise-routes= --exit-node= --unattended=true --hostname=home-windows --timeout=20s --json
    # A timeout while awaiting browser login is expected; status confirms enrollment.
} else {
    & $binary status
    if ($LASTEXITCODE -ne 0) { throw 'Tailscale is not connected; run -Action Login.' }
}
