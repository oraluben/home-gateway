#Requires -RunAsAdministrator
[CmdletBinding()]
param([switch]$Watchdog, [int]$WorkerProcessId = 0)
& (Join-Path $PSScriptRoot 'hosts\hyperv\Restore-HostNetwork.ps1') @PSBoundParameters
