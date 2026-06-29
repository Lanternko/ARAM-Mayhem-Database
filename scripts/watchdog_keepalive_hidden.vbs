' watchdog_keepalive_hidden.vbs
' Per-minute keepalive for the Mayhem LCU watchdog.
' Runs under wscript.exe (a windowless host). The common case -- watchdog already
' alive -- is resolved entirely here via WMI, so NO console process is spawned and
' no black window flashes. PowerShell (watchdog_keepalive.ps1) is invoked only on
' the rare restart path, where it sets up stdout/stderr redirection for diagnostics.
Option Explicit

Dim fso, shell, scriptDir, root, logDir, keepaliveLog, ps1
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
root = fso.GetParentFolderName(scriptDir)
logDir = root & "\logs"
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
keepaliveLog = logDir & "\mayhem-lcu-watchdog-keepalive.log"

Dim wmi, procs, p, found
found = 0
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT ProcessId,Name,CommandLine FROM Win32_Process WHERE Name='python.exe' OR Name='pythonw.exe'")
For Each p In procs
    If Not IsNull(p.CommandLine) Then
        If InStr(p.CommandLine, "mayhem_lcu_watchdog.py") > 0 Then
            found = p.ProcessId
            Exit For
        End If
    End If
Next

If found <> 0 Then
    WriteLog "ok watchdog_pid=" & found
    WScript.Quit 0
End If

' Watchdog not running -> delegate the full (logged) start to the PS1, hidden.
ps1 = scriptDir & "\watchdog_keepalive.ps1"
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & ps1 & Chr(34), 0, True
WScript.Quit 0

Sub WriteLog(msg)
    Dim f
    Set f = fso.OpenTextFile(keepaliveLog, 8, True)  ' 8 = ForAppending, create if missing
    f.WriteLine Timestamp() & " " & msg
    f.Close
End Sub

Function Timestamp()
    Dim d
    d = Now
    Timestamp = Year(d) & "-" & Pad2(Month(d)) & "-" & Pad2(Day(d)) & "T" & _
                Pad2(Hour(d)) & ":" & Pad2(Minute(d)) & ":" & Pad2(Second(d))
End Function

Function Pad2(n)
    If n < 10 Then
        Pad2 = "0" & n
    Else
        Pad2 = "" & n
    End If
End Function
