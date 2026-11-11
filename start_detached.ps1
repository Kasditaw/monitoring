$py = "C:\Users\SD Kristen Petra 1\AppData\Local\Programs\Python\Python313\pythonw.exe"
$script = "C:\xampp\htdocs\Monitoring\server_web.py"
$cmd = "`"$py`" `"$script`""
$wmi = [wmiclass]"win32_process"
$res = $wmi.Create($cmd, "C:\xampp\htdocs\Monitoring", $null)
Write-Output "Result: $($res.ReturnValue), PID: $($res.ProcessId)"

