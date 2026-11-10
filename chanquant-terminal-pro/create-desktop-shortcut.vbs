Option Explicit
Dim cqShell, cqFS, cqRoot, cqLauncher, cqLink, cqShortcut
Set cqShell = CreateObject("WScript.Shell")
Set cqFS = CreateObject("Scripting.FileSystemObject")
cqRoot = cqFS.GetParentFolderName(WScript.ScriptFullName)
cqLauncher = cqFS.BuildPath(cqRoot, "start-alerts.cmd")
If Not cqFS.FileExists(cqLauncher) Then
  WScript.Echo "Launcher not found. Extract the full application folder first."
  WScript.Quit 1
End If
cqLink = cqFS.BuildPath(cqShell.SpecialFolders("Desktop"), "ChanQuant-Alerts.lnk")
If cqFS.FileExists(cqLink) Then
  Set cqShortcut = cqShell.CreateShortcut(cqLink)
  If LCase(cqShortcut.TargetPath) <> LCase(cqLauncher) Then
    WScript.Echo "A different ChanQuant-Alerts shortcut already exists. Rename it before creating this shortcut."
    WScript.Quit 1
  End If
  WScript.Echo "A shortcut already exists. It was left unchanged."
  WScript.Quit 0
End If
Set cqShortcut = cqShell.CreateShortcut(cqLink)
cqShortcut.TargetPath = cqLauncher
cqShortcut.WorkingDirectory = cqRoot
cqShortcut.Description = "ChanQuant local stock alerts - read only"
cqShortcut.IconLocation = cqShell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\shell32.dll,23"
cqShortcut.Save
WScript.Echo "Created desktop shortcut: " & cqLink
