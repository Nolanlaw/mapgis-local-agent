param(
    [string]$PythonPath,
    [string]$MapGISInstallation,
    [switch]$EnableNativeLegacyHost,
    [switch]$EnablePrivateUi,
    [ValidateSet('legacy', 'compact', 'hybrid')]
    [string]$Surface = 'hybrid'
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PortablePython = Join-Path $ProjectRoot 'runtime\python\python.exe'
if ((Test-Path -LiteralPath $PortablePython) -and -not $PythonPath) {
    $PortableArguments = @('-s', '-B', '-X', 'utf8', ('"' + (Join-Path $PSScriptRoot 'portable.py') + '"'), 'setup', '--surface', $Surface)
    if ($EnableNativeLegacyHost) { $PortableArguments += '--enable-native-legacy-host' }
    if ($EnablePrivateUi) { $PortableArguments += '--enable-private-ui' }
    if ($MapGISInstallation) {
        $NativeArgument = (Resolve-Path -LiteralPath $MapGISInstallation).Path
        if (Test-Path -LiteralPath $NativeArgument -PathType Container) { $NativeArgument = $NativeArgument.TrimEnd('\') + '\.' }
        $PortableArguments += @('--mapgis', ('"' + $NativeArgument + '"'))
    }
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
if (($EnableNativeLegacyHost -or $EnablePrivateUi) -and -not $MapGISInstallation -and -not (Test-Path -LiteralPath $NativeConfigPath)) {
    throw 'Specify -MapGISInstallation before enabling the isolated native legacy host or private UI backend.'
}
if ($MapGISInstallation) {
    $NativeInstallation = (Resolve-Path -LiteralPath $MapGISInstallation).Path
    $LeafName = (Split-Path -Leaf $NativeInstallation).ToLowerInvariant()
    if ($LeafName -eq 'mdiedit6x.exe') {
        $NativeInstallation = Split-Path -Parent (Split-Path -Parent $NativeInstallation)
    } elseif ($LeafName -eq 'program') {
        $NativeInstallation = Split-Path -Parent $NativeInstallation
    }
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
function Resolve-Python312Path {
    foreach ($RegPath in @(
        'HKCU:\Software\Python\PythonCore\3.12\InstallPath',
        'HKLM:\Software\Python\PythonCore\3.12\InstallPath',
        'HKCU:\Software\Python\PythonCore\3.13\InstallPath',
        'HKLM:\Software\Python\PythonCore\3.13\InstallPath'
    )) {
        $Entry = Get-ItemProperty -LiteralPath $RegPath -ErrorAction SilentlyContinue
        if ($Entry -and $Entry.ExecutablePath -and (Test-Path -LiteralPath $Entry.ExecutablePath -PathType Leaf)) {
            return $Entry.ExecutablePath
        }
    }
    $PyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        foreach ($Tag in @('-3.12', '-3')) {
            $Found = & $PyLauncher.Source $Tag -c "import sys; print(sys.executable if sys.version_info >= (3, 12) else '')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $Found -and (Test-Path -LiteralPath $Found.Trim() -PathType Leaf)) {
                return $Found.Trim()
            }
        }
    }
    foreach ($CmdName in @('python.exe', 'python3.exe')) {
        foreach ($Cmd in (Get-Command $CmdName -All -ErrorAction SilentlyContinue)) {
            if ($Cmd.Source -like '*\WindowsApps\*') { continue }
            $Found = & $Cmd.Source -c "import sys; print(sys.executable if sys.version_info >= (3, 12) else '')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $Found -and (Test-Path -LiteralPath $Found.Trim() -PathType Leaf)) {
                return $Found.Trim()
            }
        }
    }
    return $null
}
if (-not $PythonPath) {
    $PythonPath = Resolve-Python312Path
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'Pass -PythonPath with an existing Python 3.12+ interpreter (also checked HKCU/HKLM registry, py launcher, and PATH). No global Python is modified.'
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
    args = @('-s', '-B', '-X', 'utf8', (Join-Path $ProjectRoot 'scripts\launch_mcp.py'), '--root', $ProjectRoot, '--surface', $Surface)
}}}
$McpPath = Join-Path $ProjectRoot 'mcp-config.local.json'
[System.IO.File]::WriteAllText($McpPath, ($McpEntry | ConvertTo-Json -Depth 8), [System.Text.UTF8Encoding]::new($false))
if ($MapGISInstallation -and -not (Test-Path -LiteralPath $NativeConfigPath)) {
    $NativeConfig = @{ mapgis_installation = $NativeInstallation } | ConvertTo-Json
    [System.IO.File]::WriteAllText($NativeConfigPath, $NativeConfig, [System.Text.UTF8Encoding]::new($false))
}
if ($EnableNativeLegacyHost -or $EnablePrivateUi) {
    $NativeState = Get-Content -LiteralPath $NativeConfigPath -Raw | ConvertFrom-Json
    if ($EnableNativeLegacyHost) {
        $NativeState | Add-Member -NotePropertyName native_backend_legacy_dep_compatibility -NotePropertyValue $true -Force
    }
    if ($EnablePrivateUi) {
        $NativeState | Add-Member -NotePropertyName native_general_private_ui -NotePropertyValue $true -Force
        $NativeState | Add-Member -NotePropertyName native_volume_private_ui -NotePropertyValue $true -Force
    }
    [System.IO.File]::WriteAllText($NativeConfigPath, ($NativeState | ConvertTo-Json -Depth 20), [System.Text.UTF8Encoding]::new($false))
}
& $EnvironmentPython -s -B -X utf8 (Join-Path $ProjectRoot 'scripts\mapgis_cli.py') capabilities --root $ProjectRoot
if ($LASTEXITCODE -ne 0) { throw 'local capabilities check failed' }
Write-Output "Ready. Client configuration ($Surface): $McpPath"
Write-Output 'No global model, provider, MCP, licence or security setting was changed.'
