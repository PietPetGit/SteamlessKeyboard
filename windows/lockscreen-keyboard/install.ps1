# Installs the lock-screen keyboard. Called (elevated) by install.bat.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$src  = Join-Path $here "LockScreenKeyboard.exe"
$dir  = "C:\LockScreenKeyboard"
$exe  = Join-Path $dir "LockScreenKeyboard.exe"

Write-Host "`n  Installing lock-screen keyboard...`n" -ForegroundColor Cyan

if (-not (Test-Path $src)) {
    Write-Host "  ERROR: LockScreenKeyboard.exe missing. Keep all files together." -ForegroundColor Red
    Read-Host "`n  Press Enter to close"; return
}

Write-Host "  1/3  Copying onto your PC..."
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Copy-Item -LiteralPath $src -Destination $exe -Force

Write-Host "  2/3  Allowing in Windows Defender..."
try { Add-MpPreference -ExclusionPath $dir -ErrorAction Stop } catch {}
try { Add-MpPreference -ExclusionProcess $exe -ErrorAction Stop } catch {}

Write-Host "  3/3  Linking to the lock-screen button..."
$k = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\Utilman.exe"
New-Item -Path $k -Force | Out-Null
New-ItemProperty -Path $k -Name "Debugger" -Value ('"{0}"' -f $exe) -PropertyType String -Force | Out-Null

if ((Get-ItemProperty $k -Name Debugger -ErrorAction SilentlyContinue).Debugger) {
    Write-Host "`n  DONE. Lock with Win+L, click 'Ease of Access' (bottom-right)," -ForegroundColor Green
    Write-Host "  type your password, press R2. Uninstall any time." -ForegroundColor Green
} else {
    Write-Host "`n  Defender may have blocked a step - run install.bat once more." -ForegroundColor Yellow
}
Read-Host "`n  Press Enter to close"
