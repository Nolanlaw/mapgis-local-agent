param([string]$PythonPath, [string]$MapGISInstallation)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PortablePython = Join-Path $ProjectRoot 'runtime\python\python.exe'
if ((Test-Path -LiteralPath $PortablePython) -and -not $PythonPath) {
    $PortableArguments = @('-s', '-B', '-X', 'utf8', (Join-Path $PSScriptRoot 'portable.py'), 'setup')
    if ($MapGISInstallation) { $PortableArguments += @('--mapgis', $MapGISInstallation) }
    & $PortablePython @PortableArguments
    if ($LASTEXITCODE -ne 0) { throw 'Portable package setup failed; inspect the reported error.' }
    return
}
$NativeConfigPath = Join-Path $ProjectRoot 'config.local.json'
if ($MapGISInstallation) {
    $NativeInstallation = (Resolve-Path -LiteralPath $MapGISInstallation).Path
    if (-not (Test-Path -LiteralPath (Join-Path $NativeInstallation 'program\mdiedit6x.exe') -PathType Leaf)) {
        throw 'MapGISInstallation must contain program\mdiedit6x.exe.'
    }
    if (Test-Path -LiteralPath $NativeConfigPath) {
        $ExistingNativeConfig = Get-Content -LiteralPath $NativeConfigPath -Raw | ConvertFrom-Json
        if (-not $ExistingNativeConfig.mapgis_installation -or
            (Resolve-Path -LiteralPath $ExistingNativeConfig.mapgis_installation).Path -ne $NativeInstallation) {
            throw 'Existing config.local.json points elsewhere; inspect it before changing the installation binding.'
        }
    }
}
if (-not $PythonPath) {
    $InstalledPython = Get-ItemProperty -LiteralPath 'HKCU:\Software\Python\PythonCore\3.12\InstallPath' -ErrorAction SilentlyContinue
    if ($InstalledPython -and $InstalledPython.ExecutablePath) { $PythonPath = $InstalledPython.ExecutablePath }
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'Pass -PythonPath with an existing Python 3.12+ interpreter. No global Python is modified.'
}
$EnvironmentPath = Join-Path $ProjectRoot '.venv'
$EnvironmentPython = Join-Path $EnvironmentPath 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $EnvironmentPython)) {
    & $PythonPath -m venv $EnvironmentPath
    if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
}
& $EnvironmentPython -m ensurepip --upgrade
if ($LASTEXITCODE -ne 0) { throw 'pip setup failed' }
& $EnvironmentPython -m pip install -r (Join-Path $ProjectRoot 'requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'dependency installation failed' }
# Normal wheels avoid Python 3.12/CP936 editable-path decoding failures.
& $EnvironmentPython -m pip install --no-deps (Join-Path $ProjectRoot 'vendor\mapgis2shp')
if ($LASTEXITCODE -ne 0) { throw 'native reader installation failed' }
& $EnvironmentPython -m pip install --no-deps $ProjectRoot
if ($LASTEXITCODE -ne 0) { throw 'bridge installation failed' }
$McpEntry = [ordered]@{ mcpServers = [ordered]@{ mapgis_local = [ordered]@{
    command = $EnvironmentPython
    args = @('-X', 'utf8', (Join-Path $ProjectRoot 'scripts\launch_mcp.py'), '--root', $ProjectRoot)
}}}
$McpPath = Join-Path $ProjectRoot 'mcp-config.local.json'
[System.IO.File]::WriteAllText($McpPath, ($McpEntry | ConvertTo-Json -Depth 8), [System.Text.UTF8Encoding]::new($false))
if ($MapGISInstallation -and -not (Test-Path -LiteralPath $NativeConfigPath)) {
    $NativeConfig = @{ mapgis_installation = $NativeInstallation } | ConvertTo-Json
    [System.IO.File]::WriteAllText($NativeConfigPath, $NativeConfig, [System.Text.UTF8Encoding]::new($false))
}
& $EnvironmentPython -X utf8 (Join-Path $ProjectRoot 'scripts\mapgis_cli.py') capabilities
if ($LASTEXITCODE -ne 0) { throw 'local capabilities check failed' }
Write-Output "Ready. Client configuration: $McpPath"
Write-Output 'No global model, provider, MCP, licence or security setting was changed.'
