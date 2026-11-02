param([string]$PythonPath, [string]$MapGISInstallation, [switch]$EnableNativeLegacyHost)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PortablePython = Join-Path $ProjectRoot 'runtime\python\python.exe'
if ((Test-Path -LiteralPath $PortablePython) -and -not $PythonPath) {
    $PortableArguments = @('-s', '-B', '-X', 'utf8', ('"' + (Join-Path $PSScriptRoot 'portable.py') + '"'), 'setup')
    if ($EnableNativeLegacyHost) { $PortableArguments += '--enable-native-legacy-host' }
    if ($MapGISInstallation) {
        $NativeArgument = (Resolve-Path -LiteralPath $MapGISInstallation).Path
        if (Test-Path -LiteralPath $NativeArgument -PathType Container) { $NativeArgument = $NativeArgument.TrimEnd('\') + '\.' }
        $PortableArguments += @('--mapgis', ('"' + $NativeArgument + '"'))
    }
    # Windows PowerShell 5.1 can return without launching a native application
    # in a host without a console. Start-Process provides a real process handle
    # and exit status; keep both streams in this package's own work directory.
    $SetupLogs = Join-Path $ProjectRoot 'work\setup'
    New-Item -ItemType Directory -Path $SetupLogs -Force | Out-Null
    $LaunchId = [Guid]::NewGuid().ToString('N')
    $SetupOutput = Join-Path $SetupLogs ($LaunchId + '.stdout.txt')
    $SetupError = Join-Path $SetupLogs ($LaunchId + '.stderr.txt')
    $SetupProcess = Start-Process -FilePath $PortablePython -ArgumentList $PortableArguments -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $SetupOutput -RedirectStandardError $SetupError
    [Console]::Out.Write([System.IO.File]::ReadAllText($SetupOutput, [System.Text.Encoding]::UTF8))
    [Console]::Error.Write([System.IO.File]::ReadAllText($SetupError, [System.Text.Encoding]::UTF8))
    if ($SetupProcess.ExitCode -ne 0) { throw 'Portable package setup failed; inspect the reported error.' }
    return
}
$NativeConfigPath = Join-Path $ProjectRoot 'config.local.json'
if ($EnableNativeLegacyHost -and -not $MapGISInstallation -and -not (Test-Path -LiteralPath $NativeConfigPath)) {
    throw 'Specify MapGISInstallation before enabling the isolated legacy native host.'
}
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
& $EnvironmentPython -m pip install --no-deps --force-reinstall (Join-Path $ProjectRoot 'vendor\mapgis2shp')
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
if ($EnableNativeLegacyHost) {
    $LegacyConfig = Get-Content -LiteralPath $NativeConfigPath -Raw | ConvertFrom-Json
    $LegacyConfig | Add-Member -NotePropertyName native_backend_legacy_dep_compatibility -NotePropertyValue $true -Force
    [System.IO.File]::WriteAllText($NativeConfigPath, ($LegacyConfig | ConvertTo-Json -Depth 20), [System.Text.UTF8Encoding]::new($false))
}
& $EnvironmentPython -X utf8 (Join-Path $ProjectRoot 'scripts\mapgis_cli.py') capabilities
if ($LASTEXITCODE -ne 0) { throw 'local capabilities check failed' }
Write-Output "Ready. Client configuration: $McpPath"
Write-Output 'No global model, provider, MCP, licence or security setting was changed.'
