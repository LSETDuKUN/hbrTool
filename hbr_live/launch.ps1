$ErrorActionPreference = 'Stop'
$pythonWindow = 'F:\python\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindow)) {
    $pythonWindow = (Get-Command pythonw.exe -ErrorAction Stop).Source
}
Start-Process -FilePath $pythonWindow -ArgumentList '-B','-m','hbr_live_app' -WorkingDirectory $PSScriptRoot -Verb RunAs -WindowStyle Hidden
