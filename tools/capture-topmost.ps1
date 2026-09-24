# Grab the widget as it actually looks: briefly raise it above the other windows
# (the user keeps "always on top" off, so it is normally behind them), take the
# screen grab with the existing DPI-aware tool, then put it back.
#
#   powershell -ExecutionPolicy Bypass -File tools\capture-topmost.ps1
param(
    [string]$Title = "DeepSeek Balance",
    [string]$Out = "..\_ref\widget\desktop-transparent.png"
)

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Raise {
    [DllImport("user32.dll", SetLastError = true)] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
}
"@

# The window is a tool window, so FindWindow is awkward here; ask Windows for the
# process that owns the title instead.
$owner = Get-Process | Where-Object { $_.MainWindowTitle -eq $Title } | Select-Object -First 1
if (-not $owner) { throw "widget not found - start it first" }
$handle = $owner.MainWindowHandle
if ($handle -eq [IntPtr]::Zero) { throw "widget has no window - start it first" }

$SWP = 0x0001 -bor 0x0002 -bor 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
[void][Raise]::SetWindowPos($handle, [IntPtr](-1), 0, 0, 0, 0, $SWP)  # HWND_TOPMOST
Start-Sleep -Milliseconds 400
try {
    & "$PSScriptRoot\capture-window.ps1" -Title $Title -Out $Out
} finally {
    [void][Raise]::SetWindowPos($handle, [IntPtr](-2), 0, 0, 0, 0, $SWP)  # HWND_NOTOPMOST
}
