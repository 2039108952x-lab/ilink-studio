' iLink Studio launcher. No second gateway, no WMI process scan, no console window.
' Development fallback uses the local Electron binary; release uses the packaged app.
Option Explicit
Dim sh, fso, root, releaseExe, devExe
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
releaseExe = root & "\release\win-unpacked\iLink Studio.exe"
devExe = root & "\node_modules\electron\dist\electron.exe"
If fso.FileExists(releaseExe) Then
    sh.Run """" & releaseExe & """", 1, False
ElseIf fso.FileExists(devExe) Then
    sh.CurrentDirectory = root
    sh.Run """" & devExe & """ .", 1, False
Else
    MsgBox "iLink Studio executable not found. Build the release first.", 16, "iLink Studio"
End If
