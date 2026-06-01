@echo off
title Steam Controller - Lock Screen Keyboard - UNINSTALL
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=[Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent(); if (-not $p.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) { Start-Process -FilePath '%~f0' -Verb RunAs } else { & '%~dp0uninstall.ps1' }"
