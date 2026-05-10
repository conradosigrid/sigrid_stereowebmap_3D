$ErrorActionPreference = 'Stop'

$pluginRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pluginName = Split-Path -Leaf $pluginRoot
$metadataPath = Join-Path $pluginRoot 'metadata.txt'

if (-not (Test-Path $metadataPath)) {
    throw "metadata.txt not found in plugin root: $pluginRoot"
}

$versionLine = Select-String -Path $metadataPath -Pattern '^version=' | Select-Object -First 1
if (-not $versionLine) {
    throw "version=... not found in metadata.txt"
}

$version = ($versionLine.Line -split '=', 2)[1].Trim()
if (-not $version) {
    throw "Empty version in metadata.txt"
}

$zipPath = Join-Path (Split-Path -Parent $pluginRoot) ("{0}_{1}.zip" -f $pluginName, $version)

if (Test-Path $zipPath) {
    Remove-Item $zipPath -Force
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$files = Get-ChildItem -Path $pluginRoot -Recurse -File -Force |
    Where-Object {
        $_.Extension -notin '.pyc', '.pyo' -and
        $_.FullName -notmatch '(\\|/)__pycache__(\\|/)' -and
        $_.FullName -notmatch '(\\|/)\.[^\\/]+'
    }

$zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($pluginRoot.Length + 1).Replace('\\', '/')
        $entryName = "{0}/{1}" -f $pluginName, $relative
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $zip,
            $file.FullName,
            $entryName,
            [System.IO.Compression.CompressionLevel]::Optimal
        ) | Out-Null
    }
}
finally {
    $zip.Dispose()
}

Write-Host "ZIP ready:" $zipPath
