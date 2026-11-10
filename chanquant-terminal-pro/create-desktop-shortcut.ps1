$ErrorActionPreference = "Stop"
$CqAlertRoot = $PSScriptRoot
$CqLauncher = Join-Path $CqAlertRoot "start-alerts.cmd"
if (!(Test-Path -LiteralPath $CqLauncher)) { throw "Launcher not found. Keep this script in the extracted application folder." }
$CqDesktop = [Environment]::GetFolderPath("Desktop")
$CqLink = Join-Path $CqDesktop "ChanQuant-Alerts.lnk"
if (Test-Path -LiteralPath $CqLink) {
    $CqExisting = (New-Object -ComObject WScript.Shell).CreateShortcut($CqLink)
    if ($CqExisting.TargetPath -ne $CqLauncher) {
        throw "A different ChanQuant-Alerts shortcut already exists. Rename it before creating this shortcut."
    }
    Write-Host "The existing shortcut was left unchanged."
    return
}
$CqShell = New-Object -ComObject WScript.Shell
$CqShortcut = $CqShell.CreateShortcut($CqLink)
$CqShortcut.TargetPath = $CqLauncher
$CqShortcut.WorkingDirectory = $CqAlertRoot
$CqShortcut.Description = "ChanQuant local stock alerts - read only"
$CqShortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,23"
$CqShortcut.Save()
Write-Host "Created desktop shortcut: $CqLink"
