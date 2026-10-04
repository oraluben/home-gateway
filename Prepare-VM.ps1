[CmdletBinding()]
param()
. (Join-Path $PSScriptRoot 'Get-GatewayConfig.ps1')
$linuxRepo = (& wsl.exe -d $deployment.operator.WslDistribution -- wslpath -u $PSScriptRoot).Trim()
$linuxState = (& wsl.exe -d $deployment.operator.WslDistribution -- wslpath -u $stateDirectory).Trim()
$linuxConfig = (& wsl.exe -d $deployment.operator.WslDistribution -- wslpath -u (Join-Path $env:USERPROFILE '.config\home-gateway\deployment.json')).Trim()
& wsl.exe -d $deployment.operator.WslDistribution -- python3 ($linuxRepo+'/tools/prepare-vm.py') --config $linuxConfig --output ($linuxState+'/vm')
if ($LASTEXITCODE -ne 0) { throw 'VM disk preparation failed.' }
$seedDirectory = Join-Path $stateDirectory 'vm\seed'
$isoPath = Join-Path $stateDirectory 'vm\seed.iso'
$filesystem = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
$filesystem.FileSystemsToCreate = 3
$filesystem.VolumeName = 'cidata'
$filesystem.Root.AddTree($seedDirectory,$false)
$result = $filesystem.CreateResultImage()
if (-not ('GatewayIsoWriter' -as [type])) {
    Add-Type @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
public static class GatewayIsoWriter {
  public static void Write(object source, string destination) {
    IStream stream = (IStream)source;
    byte[] buffer = new byte[1024 * 1024];
    IntPtr count = Marshal.AllocCoTaskMem(4);
    try { using (FileStream output = File.Create(destination)) {
      while (true) { stream.Read(buffer, buffer.Length, count); int read = Marshal.ReadInt32(count);
        if (read == 0) break; output.Write(buffer, 0, read); }
    }} finally { Marshal.FreeCoTaskMem(count); }
  }
}
'@
}
[GatewayIsoWriter]::Write($result.ImageStream,$isoPath)
Write-Host 'VM disk and cloud-init seed prepared. Run Initialize-VM.ps1 as administrator.'
