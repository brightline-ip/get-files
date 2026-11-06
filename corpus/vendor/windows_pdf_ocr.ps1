param(
    [Parameter(Mandatory=$true)][string]$InputPdf,
    [Parameter(Mandatory=$true)][string]$OutputJson,
    [Parameter(Mandatory=$true)][string]$PagesCsv
)
$ErrorActionPreference = 'Stop'
# The parent first attaches a kill-on-close, memory-limited Windows job.
if ([Console]::ReadLine() -ne 'go') { exit 2 }
try {
    if ($PagesCsv -notmatch '^\d+(,\d+){0,7}$') { throw 'Invalid pages' }
    $requestedPages = @($PagesCsv.Split(',') | ForEach-Object { [int]$_ })
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
    $null = [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType=WindowsRuntime]
    $null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
    $asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $asTaskAction = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and -not $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction'
    } | Select-Object -First 1
    function Await-Result($Operation, [Type]$ResultType) {
        $task = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
        if (-not $task.Wait(10000)) { throw 'Operation timeout' }
        return $task.Result
    }
    function Await-Action($Operation) {
        $task = $asTaskAction.Invoke($null, @($Operation))
        if (-not $task.Wait(10000)) { throw 'Operation timeout' }
    }
    # Any English recognizer: en-GB, en-AU, en-CA and en-IN Windows installs do not have en-US.
    $language = New-Object Windows.Globalization.Language('en-US')
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    if ($null -eq $engine) {
        foreach ($available in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) {
            if ($available.LanguageTag -like 'en-*') { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($available); break }
        }
    }
    if ($null -eq $engine) { throw 'English OCR unavailable' }
    $source = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($InputPdf)) ([Windows.Storage.StorageFile])
    $pdf = Await-Result ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($source)) ([Windows.Data.Pdf.PdfDocument])
    $resultPages = @()
    foreach ($number in $requestedPages) {
        $page = $null; $stream = $null; $bitmap = $null
        try {
            if ($number -lt 1 -or $number -gt $pdf.PageCount) { throw 'Invalid page' }
            $page = $pdf.GetPage([uint32]($number - 1))
            $width = [double]$page.Size.Width; $height = [double]$page.Size.Height
            if ($width -le 0 -or $height -le 0 -or $width -gt 14400 -or $height -gt 14400) { throw 'Page dimensions rejected' }
            $maximum = [Math]::Min(2600, [Windows.Media.Ocr.OcrEngine]::MaxImageDimension)
            $scale = [Math]::Min(200.0 / 96.0, $maximum / [Math]::Max($width, $height))
            $options = New-Object Windows.Data.Pdf.PdfPageRenderOptions
            $options.DestinationWidth = [uint32][Math]::Max(1, [Math]::Floor($width * $scale))
            $options.DestinationHeight = [uint32][Math]::Max(1, [Math]::Floor($height * $scale))
            $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
            Await-Action ($page.RenderToStreamAsync($stream, $options))
            $stream.Seek(0)
            $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync([Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [Windows.Graphics.Imaging.BitmapAlphaMode]::Ignore)) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $recognized = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $lines = @($recognized.Lines | ForEach-Object { $_.Text })
            $text = [string]::Join("`n", $lines)
            if ($text.Length -gt 200000) { throw 'OCR output limit' }
            $resultPages += @{page=$number; text=$text; status='recognized'; width=$options.DestinationWidth; height=$options.DestinationHeight}
        } catch {
            # Never return native exception messages or input text in diagnostics.
            $resultPages += @{page=$number; text=''; status='failed'}
        } finally {
            if ($null -ne $bitmap) { $bitmap.Dispose() }
            if ($null -ne $stream) { $stream.Dispose() }
            if ($null -ne $page) { $page.Dispose() }
        }
    }
    # Pinned render configuration; the adapter rejects a payload that does not echo it.
    $payload = @{engine='Windows.Media.Ocr'; language=$engine.RecognizerLanguage.LanguageTag; render_dpi=200; max_render_pixels=2600;
                 engine_build=[Environment]::OSVersion.Version.ToString(); pages=$resultPages}
    [IO.File]::WriteAllText($OutputJson, ($payload | ConvertTo-Json -Depth 5 -Compress), (New-Object Text.UTF8Encoding($false)))
} catch {
    exit 3
}
