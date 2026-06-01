@echo off
title Steam Controller - Lock Screen Keyboard - INSTALL
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=[Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent(); if (-not $p.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) { Start-Process -FilePath '%~f0' -Verb RunAs } else { & '%~dp0install.ps1' }"
