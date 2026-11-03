param(
    [string]$OutputZip = (Join-Path $PSScriptRoot "weekly_time_report.zip")
)

$ErrorActionPreference = "Stop"
$staging = Join-Path $env:TEMP ("notion-weekly-report-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $staging | Out-Null
try {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "index.py") -Destination $staging
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "README.md") -Destination $staging
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "..\notion_calendar_export.py") -Destination (Join-Path $staging "notion_calendar_export.py")
    if (Test-Path -LiteralPath $OutputZip) {
        Remove-Item -LiteralPath $OutputZip -Force
    }
    Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $OutputZip -CompressionLevel Optimal
    Write-Output "Package created: $OutputZip"
}
finally {
    $tempRoot = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    $resolvedStaging = [System.IO.Path]::GetFullPath($staging)
    if ($resolvedStaging.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
    } else {
        throw "Refusing to remove staging folder outside TEMP: $resolvedStaging"
    }
}
