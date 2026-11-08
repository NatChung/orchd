# First Windows install, or one-time migration from an in-place uv installation.
# Run in a PowerShell session; subsequent updates use "orchd upgrade".
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Source,
    [string]$Python = '3.12'
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Use this installer on Windows.' }
$uv = (Get-Command uv -CommandType Application -ErrorAction Stop).Source
$binDir = & $uv tool dir --bin
if ($LASTEXITCODE -ne 0) { throw 'Cannot locate the uv tool launcher directory.' }

# uv may remove files from an existing environment before it discovers a locked
# executable. Even on a retry, never pass a previously used tool root to install.
$generation = Join-Path "$env:LOCALAPPDATA\orchd\versions" ([guid]::NewGuid().ToString('N'))
$oldRoot = $env:UV_TOOL_DIR
$oldBin = $env:UV_TOOL_BIN_DIR
try {
    $env:UV_TOOL_DIR = $generation
    $env:UV_TOOL_BIN_DIR = $binDir
    & $uv tool install --force --refresh --python $Python --from $Source orchd
    if ($LASTEXITCODE -ne 0) { throw "Install failed. Existing environments were retained; new directory: $generation" }
} finally {
    $env:UV_TOOL_DIR = $oldRoot
    $env:UV_TOOL_BIN_DIR = $oldBin
}
Write-Output "Installed launcher: $(Join-Path $binDir 'orchd.exe'). Use orchd upgrade for future updates."

$launcher = Join-Path $binDir 'orchd.exe'
& $launcher install
if ($LASTEXITCODE -ne 0) { throw 'Package installed, but provisioning failed. Retry orchd install.' }
