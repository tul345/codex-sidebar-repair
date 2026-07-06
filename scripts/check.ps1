param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$oldPythonPath = $env:PYTHONPATH

try {
    Set-Location $root
    $env:PYTHONPATH = Join-Path $root "src"

    & $Python -m compileall src tests
    & $Python -m unittest discover -s tests
    & $Python -m codex_sidebar_repair --help | Out-Null
    & $Python -m codex_sidebar_repair doctor --json | Out-Null
    & $Python -m codex_sidebar_repair repair --help | Out-Null
}
finally {
    $env:PYTHONPATH = $oldPythonPath
}
