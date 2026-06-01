# Removes the lock-screen keyboard. Called (elevated) by uninstall.bat.
$ErrorActionPreference = "SilentlyContinue"
$dir = "C:\LockScreenKeyboard"

Write-Host "`n  Removing lock-screen keyboard...`n" -ForegroundColor Cyan

# Undo the button link (Utilman + osk, in case either was used).
foreach ($t in @("Utilman.exe", "osk.exe")) {
    $k = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\$t"
    if (Test-Path $k) {
        Remove-ItemProperty -Path $k -Name "Debugger" -ErrorAction SilentlyContinue
        if (-not (Get-Item $k).Property) { Remove-Item -Path $k -Force -ErrorAction SilentlyContinue }
    }
}

# Remove the Defender allowances and the copied program.
Remove-MpPreference -ExclusionPath $dir -ErrorAction SilentlyContinue
Remove-MpPreference -ExclusionProcess (Join-Path $dir "LockScreenKeyboard.exe") -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "  DONE. Lock screen is back to normal." -ForegroundColor Green
Read-Host "`n  Press Enter to close"
