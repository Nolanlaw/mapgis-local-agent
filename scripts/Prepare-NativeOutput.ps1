param(
    [Parameter(Mandatory=$true)][string]$SourceManifest,
    [Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z0-9_-]{1,40}$')][string]$RequestId,
    [Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z]:\\mg67-[A-Za-z0-9-]{1,12}$')][string]$ShortAlias
)
# A filesystem-only staging operation. Native open, path binding, page setup and
# export still use the existing host's Sky adapter. No MPJ binary offsets here.
$ErrorActionPreference = 'Stop'
function Get-NativeDigest([string]$Path) {
    $Stream = [IO.File]::OpenRead($Path)
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($Hasher.ComputeHash($Stream))).Replace('-', '').ToLowerInvariant() }
    finally { $Stream.Dispose(); $Hasher.Dispose() }
}
$Root = Split-Path -Parent $PSScriptRoot
$InputSpec = Get-Content -LiteralPath $SourceManifest -Raw -Encoding UTF8 | ConvertFrom-Json
$Project = Get-Item -LiteralPath $InputSpec.project
if ($Project.Extension -ine '.MPJ' -or $Project.PSIsContainer) { throw 'Expected an existing MPJ file' }
$SourceDirectory = $Project.Directory.FullName
$Names = @($Project.Name) + @($InputSpec.files)
if (($Names | Sort-Object -Unique).Count -ne $Names.Count) { throw 'Duplicate dependency name' }
$Sources = @()
foreach ($Name in $Names) {
    if ([string]::IsNullOrWhiteSpace($Name) -or [IO.Path]::GetFileName($Name) -cne $Name) {
        throw 'Dependencies must be explicit sibling leaf names'
    }
    if ([Text.Encoding]::GetEncoding(936).GetByteCount($Name) -gt 48) {
        throw 'Use a shorter native filename (this staging policy is not a proven engine limit)'
    }
    $Item = Get-Item -LiteralPath (Join-Path $SourceDirectory $Name)
    if ($Item.PSIsContainer -or ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Only ordinary sibling files can be staged'
    }
    $Sources += [ordered]@{ name=$Name; source=$Item.FullName; bytes=$Item.Length;
        sha256=(Get-NativeDigest $Item.FullName) }
}
$StageParent = Join-Path $Root 'work\native-output'
$Stage = Join-Path $StageParent $RequestId
$ReceiptParent = Join-Path $Root 'evidence\native-output'
$ReceiptPath = Join-Path $ReceiptParent ($RequestId + '.json')
# Existing requests and aliases are never retargeted or overwritten, including
# dangling junctions. Rerun with a new request after reconciling the old receipt.
if (Test-Path -LiteralPath $Stage) { throw 'Request directory already exists; reconcile it first' }
if (Test-Path -LiteralPath $ReceiptPath) { throw 'Request receipt already exists' }
if (Get-Item -LiteralPath $ShortAlias -Force -ErrorAction SilentlyContinue) { throw 'Short alias already exists' }
New-Item -ItemType Directory -Path $Stage -Force | Out-Null
foreach ($Entry in $Sources) {
    $Destination = Join-Path $Stage $Entry.name
    [IO.File]::Copy($Entry.source, $Destination, $false)
    $Current = Get-NativeDigest $Entry.source
    $Copied = Get-NativeDigest $Destination
    if ($Entry.sha256 -ne $Current -or $Copied -ne $Current) { throw 'Source changed or copy hash mismatch' }
}
New-Item -ItemType Junction -Path $ShortAlias -Target $Stage | Out-Null
$Link = Get-Item -LiteralPath $ShortAlias -Force
$LinkTarget = @($Link.Target)[0] # Windows PowerShell returns an array; pwsh returns a string.
if ($Link.LinkType -ne 'Junction' -or [IO.Path]::GetFullPath($LinkTarget) -ine [IO.Path]::GetFullPath($Stage)) {
    throw 'Unexpected short alias target'
}
New-Item -ItemType Directory -Path $ReceiptParent -Force | Out-Null
$Receipt = [ordered]@{ request_id=$RequestId; source_project=$Project.FullName;
    stage=$Stage; short_alias=$ShortAlias; native_project=(Join-Path $ShortAlias $Project.Name);
    files=$Sources; copy_hashes_verified=$true;
    state='STAGED_REQUIRES_NATIVE_BINDING';
    required_next='Open native_project, verify every MPJ layer resolves in short_alias, set and save the native page, then export. Staging does not validate missing dependencies or rewrite absolute MPJ references.' }
[IO.File]::WriteAllText($ReceiptPath, ($Receipt | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
$Receipt | ConvertTo-Json -Depth 8
