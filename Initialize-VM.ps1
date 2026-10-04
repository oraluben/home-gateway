#Requires -RunAsAdministrator
[CmdletBinding()]
param()
& (Join-Path $PSScriptRoot 'hosts\hyperv\Initialize-VM.ps1') @PSBoundParameters
