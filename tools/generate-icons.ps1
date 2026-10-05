# Optional maintainer tool; normal PyInstaller builds use the committed assets.
# Run with PowerShell 7 on Windows. Repackages the supplied RAIDiant Icons set.
# The macOS ICNS and supplied PNG representations remain byte-for-byte identical.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing.Common
$raidAssets = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\raidiant\assets'))
$raidSource = [IO.File]::ReadAllBytes((Join-Path $raidAssets 'app-icon.icns'))
$raidOriginalHash = '4163d7bdb598c5cb98bd4e8a84458f91265454fed06dd4928b2e12aaeb03afe5'
if ([Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($raidSource)).ToLowerInvariant() -ne $raidOriginalHash) {
    throw 'The supplied source icon has changed; inspect it before regenerating assets.'
}

$raidFrames = @{}
foreach ($raidSize in @(16, 32, 48, 64, 72, 96, 128, 192, 256, 512, 1024)) {
    $raidFrames[$raidSize] = [IO.File]::ReadAllBytes((Join-Path $raidAssets "app-icon-$raidSize.png"))
}

$raidInput = [IO.MemoryStream]::new($raidFrames[1024], $false)
$raidImage = [Drawing.Image]::FromStream($raidInput)
try {
    foreach ($raidSize in @(16, 20, 24, 32, 40, 48, 64, 72, 96, 128, 160, 192, 256, 512, 1024)) {
        if (-not $raidFrames.ContainsKey($raidSize)) {
            $raidBitmap = [Drawing.Bitmap]::new($raidSize, $raidSize, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
            $raidGraphics = [Drawing.Graphics]::FromImage($raidBitmap)
            $raidAttributes = [Drawing.Imaging.ImageAttributes]::new()
            $raidOutput = [IO.MemoryStream]::new()
            try {
                $raidGraphics.CompositingMode = [Drawing.Drawing2D.CompositingMode]::SourceCopy
                $raidGraphics.CompositingQuality = [Drawing.Drawing2D.CompositingQuality]::HighQuality
                $raidGraphics.InterpolationMode = [Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
                $raidGraphics.PixelOffsetMode = [Drawing.Drawing2D.PixelOffsetMode]::HighQuality
                $raidAttributes.SetWrapMode([Drawing.Drawing2D.WrapMode]::TileFlipXY)
                $raidRectangle = [Drawing.Rectangle]::new(0, 0, $raidSize, $raidSize)
                $raidGraphics.DrawImage($raidImage, $raidRectangle, 0, 0, 1024, 1024,
                    [Drawing.GraphicsUnit]::Pixel, $raidAttributes)
                $raidBitmap.Save($raidOutput, [Drawing.Imaging.ImageFormat]::Png)
                $raidFrames[$raidSize] = $raidOutput.ToArray()
            } finally {
                $raidOutput.Dispose()
                $raidAttributes.Dispose()
                $raidGraphics.Dispose()
                $raidBitmap.Dispose()
            }
        }
        [IO.File]::WriteAllBytes((Join-Path $raidAssets "app-icon-$raidSize.png"), $raidFrames[$raidSize])
    }
} finally {
    $raidImage.Dispose()
    $raidInput.Dispose()
}

# PNG-backed Windows ICO, including fractional display-scale sizes.
$raidSizes = @(16, 20, 24, 32, 40, 48, 64, 72, 96, 128, 160, 192, 256)
$raidIco = [IO.MemoryStream]::new()
$raidWriter = [IO.BinaryWriter]::new($raidIco)
try {
    $raidWriter.Write([uint16]0)
    $raidWriter.Write([uint16]1)
    $raidWriter.Write([uint16]$raidSizes.Length)
    $raidOffset = 6 + 16 * $raidSizes.Length
    foreach ($raidSize in $raidSizes) {
        $raidDimension = if ($raidSize -eq 256) { 0 } else { $raidSize }
        $raidWriter.Write([byte]$raidDimension)
        $raidWriter.Write([byte]$raidDimension)
        $raidWriter.Write([byte]0)
        $raidWriter.Write([byte]0)
        $raidWriter.Write([uint16]1)
        $raidWriter.Write([uint16]32)
        $raidWriter.Write([uint32]$raidFrames[$raidSize].Length)
        $raidWriter.Write([uint32]$raidOffset)
        $raidOffset += $raidFrames[$raidSize].Length
    }
    foreach ($raidSize in $raidSizes) { $raidWriter.Write([byte[]]$raidFrames[$raidSize]) }
    [IO.File]::WriteAllBytes((Join-Path $raidAssets 'app-icon.ico'), $raidIco.ToArray())
} finally { $raidWriter.Dispose(); $raidIco.Dispose() }
Write-Output 'Generated Windows ICO and scale variants; preserved supplied macOS ICNS and PNGs.'
