# 用 Windows 自带的文字识别（Windows.Media.Ocr，离线、不联网）读出图片里的文字。
# 用法：powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ocr_win.ps1 <图片路径>
# 输出一行 JSON：{"lang":"zh-Hans-CN","lines":["……"]}；没有可用的识别语言时 {"error":"..."}
param([Parameter(Mandatory = $true)][string]$Path)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
    $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
            $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
    function Await($op, [Type]$type) {
        $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
        $null = $task.Wait(-1)
        $task.Result
    }
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if ($null -eq $engine) { '{"error":"no-ocr-language"}'; exit 0 }
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = New-Object System.Collections.ArrayList
    foreach ($l in $result.Lines) { $null = $lines.Add($l.Text) }
    $stream.Dispose()
    (@{ lang = $engine.RecognizerLanguage.LanguageTag; lines = $lines.ToArray() } | ConvertTo-Json -Compress)
}
catch {
    $e = $_.Exception
    while ($null -ne $e.InnerException) { $e = $e.InnerException }      # Wait() 包着的真正原因
    (@{ error = ($e.Message -replace '\s+', ' ') } | ConvertTo-Json -Compress)
}
