# ODBC Driver 17 Installation Guide - PowerShell

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " ODBC Driver Installation - Microsoft SQL Server" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Current Status:" -ForegroundColor Yellow
Write-Host "- ODBC Driver 17 for SQL Server: NOT INSTALLED" -ForegroundColor Red
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Installation Options" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "OPTION 1: Download Installer (Recommended)" -ForegroundColor Green
Write-Host "-------------------------------------------"
Write-Host "1. Visit: https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server"
Write-Host "2. Click 'Download ODBC Driver 17 for SQL Server'"
Write-Host "3. Choose your version (x64 or x86)"
Write-Host "4. Run the installer"
Write-Host "5. Restart this app when complete"
Write-Host ""

Write-Host "OPTION 2: Using Windows Package Manager (winget)" -ForegroundColor Green
Write-Host "-------------------------------------------"
Write-Host "Run this command (requires admin):"
Write-Host ""
Write-Host '  winget install Microsoft.ODBCDriver17ForSQLServer' -ForegroundColor Yellow
Write-Host ""

Write-Host "OPTION 3: Using Chocolatey (if installed)" -ForegroundColor Green
Write-Host "-------------------------------------------"
Write-Host "Run this command (requires admin):"
Write-Host ""
Write-Host '  choco install odbc-driver-17-for-sql-server' -ForegroundColor Yellow
Write-Host ""

Write-Host "OPTION 4: Automatic Installation (Admin Required)" -ForegroundColor Green
Write-Host "-------------------------------------------"
Write-Host "Do you want to try automatic installation? (Y/N)"
$response = Read-Host

if ($response -eq "Y" -or $response -eq "y") {
    Write-Host ""
    Write-Host "Checking for admin privileges..." -ForegroundColor Cyan
    
    # Check if running as admin
    $isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")
    
    if (-not $isAdmin) {
        Write-Host "ERROR: This script requires administrator privileges!" -ForegroundColor Red
        Write-Host "Please run PowerShell as Administrator and try again."
        Read-Host "Press Enter to exit"
        exit
    }
    
    # Try winget first
    Write-Host "Attempting to install via Windows Package Manager (winget)..." -ForegroundColor Cyan
    try {
        winget install Microsoft.ODBCDriver17ForSQLServer --accept-source-agreements --accept-package-agreements -e
        if ($LASTEXITCODE -eq 0) {
            Write-Host "✅ Installation successful!" -ForegroundColor Green
            Write-Host "Restart the DCC Test Suite app to use the new driver."
            Read-Host "Press Enter to exit"
            exit
        }
    } catch {
        Write-Host "winget installation failed, trying Chocolatey..." -ForegroundColor Yellow
    }
    
    # Try chocolatey
    Write-Host "Attempting to install via Chocolatey..." -ForegroundColor Cyan
    try {
        choco install odbc-driver-17-for-sql-server -y
        if ($LASTEXITCODE -eq 0) {
            Write-Host "✅ Installation successful!" -ForegroundColor Green
            Write-Host "Restart the DCC Test Suite app to use the new driver."
            Read-Host "Press Enter to exit"
            exit
        }
    } catch {
        Write-Host "❌ Automatic installation failed." -ForegroundColor Red
    }
    
    Write-Host ""
    Write-Host "❌ Automatic installation could not complete." -ForegroundColor Red
    Write-Host "Please use OPTION 1 (manual download) instead." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " After Installation" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "1. Close this window"
Write-Host "2. Restart the DCC Test Suite app"
Write-Host "3. Click 'Connect to Database' button"
Write-Host "4. Connection should now work!"
Write-Host ""
Read-Host "Press Enter to exit"
