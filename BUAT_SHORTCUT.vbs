Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
strCurDir = fso.GetParentFolderName(WScript.ScriptFullName)

desktop = ws.SpecialFolders("Desktop")
Set s = ws.CreateShortcut(desktop & "\LabGuard PRO - Guru.lnk")
s.TargetPath = "cmd.exe"
s.Arguments = "/c start """" """ & strCurDir & "\JALANKAN_APLIKASI_GURU.bat"""
s.WorkingDirectory = strCurDir
s.IconLocation = strCurDir & "\app_icon.ico,0"
s.Description = "LabGuard PRO - Aplikasi Pemantauan & Kontrol Lab Komputer Guru"
s.WindowStyle = 7
s.Save

WScript.Echo "Shortcut 'LabGuard PRO - Guru' berhasil dibuat di Desktop Anda!"
