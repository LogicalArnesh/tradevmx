' TradeVMx silent launcher — starts the desktop app with no console window.
' Used by the "Run at Windows startup" toggle and by the Startup shortcut.
Option Explicit

Dim shell, fso, base, py, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

base = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = base

py = "pythonw.exe"
If fso.FileExists(base & "\.venv\Scripts\pythonw.exe") Then
  py = """" & base & "\.venv\Scripts\pythonw.exe"""
End If

cmd = py & " """ & base & "\desktop.py"""
shell.Run cmd, 0, False
