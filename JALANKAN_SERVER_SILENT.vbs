Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
strCurDir = fso.GetParentFolderName(WScript.ScriptFullName)

' 1. Start Apache Reverse Proxy (Laragon) silently if present
apacheExe = "C:\laragon\bin\apache\httpd-2.4.68-260617-Win64-VS18\bin\httpd.exe"
apacheDir = "C:\laragon\bin\apache\httpd-2.4.68-260617-Win64-VS18"
If fso.FileExists(apacheExe) Then
    WshShell.Run """" & apacheExe & """ -d """ & apacheDir & """", 0, False
End If

' 2. Locate pythonw.exe dynamically
pyPath = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe")
If Not fso.FileExists(pyPath) Then
    pyPath = "C:\Users\SD Kristen Petra 1\AppData\Local\Programs\Python\Python313\pythonw.exe"
End If
If Not fso.FileExists(pyPath) Then
    pyPath = "pythonw.exe"
End If

' 3. Start FastAPI Server silently
cmd = """" & pyPath & """ """ & strCurDir & "\server_web.py"""
WshShell.CurrentDirectory = strCurDir
WshShell.Run cmd, 0, False
