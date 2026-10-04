[CmdletBinding()]
param()
& (Join-Path $PSScriptRoot 'hosts\hyperv\Prepare-VM.ps1') @PSBoundParameters
