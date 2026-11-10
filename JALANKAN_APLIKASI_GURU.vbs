' VBScript Launcher - Menjalankan LabGuard PRO Desktop tanpa jendela CMD hitam (100% Senyap)
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

strCurDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Cari pythonw.exe
pyPath = "pythonw.exe"
If fso.FileExists("C:\Users\SD Kristen Petra 1\AppData\Local\Programs\Python\Python313\pythonw.exe") Then
    pyPath = "C:\Users\SD Kristen Petra 1\AppData\Local\Programs\Python\Python313\pythonw.exe"
ElseIf fso.FileExists("C:\laragon\bin\python\python-3.13\pythonw.exe") Then
    pyPath = "C:\laragon\bin\python\python-3.13\pythonw.exe"
End If

cmd = """" & pyPath & """ """ & strCurDir & "\app_guru.py"""
WshShell.CurrentDirectory = strCurDir
WshShell.Run cmd, 0, False
