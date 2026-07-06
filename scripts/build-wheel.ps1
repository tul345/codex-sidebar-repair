param(
    [string]$Python = "python",
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $OutputDir) {
    $OutputDir = Join-Path $root "dist"
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
& $Python -m pip wheel $root --no-deps --wheel-dir $OutputDir
