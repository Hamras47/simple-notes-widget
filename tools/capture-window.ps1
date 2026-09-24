# Capture the widget's on-screen rectangle, including whatever shows through the
# glass, so the native acrylic effect is verified rather than assumed.
#
#   powershell -ExecutionPolicy Bypass -File tools\capture-window.ps1 `
#     -Out ..\_ref\widget\desktop.png
#
# A window capture (rather than a page screenshot) is the only way to see the
# blur: it is done by DWM behind the web content and no DOM dump can show it.
param(
    [string]$Title = "DeepSeek Balance",
    [string]$Out = "capture.png",
    [int]$Margin = 28
)

Add-Type -AssemblyName System.Drawing

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinProbe {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
    [DllImport("user32.dll")]
    public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")]
    public static extern uint GetDpiForWindow(IntPtr hWnd);
}
"@

# Without this the process is DPI-virtualised and the grab would be a scaled
# down copy of the screen instead of real pixels.
[void][WinProbe]::SetProcessDPIAware()

$proc = Get-Process |
    Where-Object { $_.MainWindowTitle -eq $Title -and $_.MainWindowHandle -ne 0 } |
    Select-Object -First 1
if (-not $proc) {
    Write-Error "no window titled '$Title' - is the widget running?"
    exit 2
}
$handle = $proc.MainWindowHandle

$rect = New-Object WinProbe+RECT
if (-not [WinProbe]::GetWindowRect($handle, [ref]$rect)) {
    Write-Error "GetWindowRect failed"
    exit 3
}

$width = $rect.Right - $rect.Left
$height = $rect.Bottom - $rect.Top
$scale = [WinProbe]::GetDpiForWindow($handle) / 96.0

$left = $rect.Left - $Margin
$top = $rect.Top - $Margin
$grabW = $width + (2 * $Margin)
$grabH = $height + (2 * $Margin)

$bitmap = New-Object System.Drawing.Bitmap -ArgumentList $grabW, $grabH
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($left, $top, 0, 0, (New-Object System.Drawing.Size -ArgumentList $grabW, $grabH))
$bitmap.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose()
$bitmap.Dispose()

"window     : $($rect.Left),$($rect.Top)  ${width}x${height} physical px (window dpi scale $scale)"
"capture    : $Out  ${grabW}x${grabH} (+$Margin px of desktop around it)"
"css size   : $([math]::Round($width / $scale))x$([math]::Round($height / $scale)) at dpr $scale"
