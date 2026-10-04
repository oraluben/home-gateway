#Requires -RunAsAdministrator
[CmdletBinding()]
param()
& (Join-Path $PSScriptRoot 'hosts\hyperv\Get-VMStatus.ps1') @PSBoundParameters
