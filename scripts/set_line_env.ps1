param(
    [string]$EnvPath = ".env.local"
)

$ErrorActionPreference = "Stop"

function Read-SecretText {
    param([string]$Prompt)

    $secure = Read-Host $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Set-EnvLine {
    param(
        [string[]]$Lines,
        [string]$Name,
        [string]$Value
    )

    $pattern = "^\s*$([Regex]::Escape($Name))\s*="
    $updated = $false
    $result = @(foreach ($line in $Lines) {
        if ($line -match $pattern) {
            $updated = $true
            "$Name=$Value"
        }
        else {
            $line
        }
    })

    if (-not $updated) {
        $result += "$Name=$Value"
    }

    $result
}

$secret = Read-SecretText "LINE_CHANNEL_SECRET"
$token = Read-SecretText "LINE_CHANNEL_ACCESS_TOKEN"

if ([string]::IsNullOrWhiteSpace($secret) -or [string]::IsNullOrWhiteSpace($token)) {
    throw "Both LINE_CHANNEL_SECRET and LINE_CHANNEL_ACCESS_TOKEN are required."
}

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
if ([System.IO.Path]::IsPathRooted($EnvPath)) {
    $fullPath = $EnvPath
}
else {
    $fullPath = Join-Path $projectRoot $EnvPath
}
$lines = @()
if (Test-Path -LiteralPath $fullPath) {
    $lines = Get-Content -LiteralPath $fullPath
}

$lines = Set-EnvLine -Lines $lines -Name "LINE_CHANNEL_SECRET" -Value $secret
$lines = Set-EnvLine -Lines $lines -Name "LINE_CHANNEL_ACCESS_TOKEN" -Value $token

Set-Content -LiteralPath $fullPath -Value $lines -Encoding UTF8
Write-Host "Updated $EnvPath with LINE settings."
