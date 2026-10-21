<# Official Windows onedir installer; compatible with Windows PowerShell 5.1. #>
$ErrorActionPreference = 'Stop'
$AssumeYes = $false; $DeleteMode = $false
foreach ($argument in $args) {
    switch ($argument) {
        { $_ -in '-y', '--yes' } { $AssumeYes = $true }
        { $_ -in '-d', '--delete' } { $DeleteMode = $true }
        default { throw "Unknown installer option: $argument" }
    }
}
function Confirm-Action([string]$Message) {
    if ($AssumeYes) { return $true }
    return (Read-Host "$Message [y/N]") -match '^(y|yes)$'
}
function Assert-NoLinks([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -Force -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point in installation path: $cursor" }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
}
function Read-Metadata([string]$Path) {
    $result = @{}
    if (Test-Path -LiteralPath $Path) {
        Assert-NoLinks $Path
        foreach ($line in [IO.File]::ReadAllLines($Path)) {
            if ($line -match '^([^=]+)=(.*)$') {
                if ($result.ContainsKey($Matches[1])) { throw 'Duplicate installation metadata key' }
                $result[$Matches[1]] = $Matches[2]
            }
        }
    }
    return $result
}
function Write-Atomic([string]$Path, [string]$Text) {
    $temporary = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Text)
        $stream = [IO.File]::Open($temporary, 'CreateNew', 'Write', 'None')
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $Path) }
    } finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}
function Get-Sha([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Receive-VerifiedHost([string]$Url, [string]$Destination, [long]$Limit) {
    # Explicit redirects, bounded streaming, and no ambient credentials.
    for ($redirect = 0; $redirect -le 5; $redirect++) {
        if ($Url -notmatch '^https://(?:api\.github\.com/repos/farin-works/luminesk-cli/releases/latest|github\.com/farin-works/luminesk-cli/releases/download/[^?#]+|release-assets\.githubusercontent\.com/[^\r\n]*)$') { throw 'Release asset has an invalid download URL' }
        $request = [Net.HttpWebRequest]::Create($Url)
        $request.AllowAutoRedirect = $false
        $request.UserAgent = 'Luminesk-Installer'
        $request.Timeout = 300000
        $request.ReadWriteTimeout = 300000
        $response = $request.GetResponse()
        try {
            if ([int]$response.StatusCode -in 301,302,303,307,308) { $Url = $response.Headers['Location']; continue }
            if ([int]$response.StatusCode -ne 200 -or $response.ContentLength -gt $Limit) { throw 'Invalid or oversized download' }
            $inputStream = $response.GetResponseStream()
            $outputStream = [IO.File]::Create($Destination)
            try {
                $buffer = New-Object byte[] 65536
                [long]$total = 0
                while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $total += $count
                    if ($total -gt $Limit) { throw 'Download size limit exceeded' }
                    $outputStream.Write($buffer, 0, $count)
                }
                $outputStream.Flush($true)
            } finally { $outputStream.Dispose(); $inputStream.Dispose() }
            return
        } finally { $response.Dispose() }
    }
    throw 'Too many download redirects'
}
function Expand-Bundle([string]$ArchivePath, [string]$Destination) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        if ($zip.Entries.Count -gt 20001) { throw 'Too many ZIP members' }
        $entries = @{}; [long]$total = 0
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName
            if ($name -notmatch '^nesk/(?:[A-Za-z0-9_,+.-]+/)*[A-Za-z0-9_,+.-]+/?$' -or $name -match '(^|/)\.{1,2}(/|$)' -or $name -match '(^|/)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])([./]|$)' -or $name -match '[. ](/|$)') { throw "Unsafe ZIP path: $name" }
            $key = $name.TrimEnd('/')
            if ($entries.ContainsKey($key)) { throw 'Duplicate or case-colliding ZIP member' }
            $type = ($entry.ExternalAttributes -shr 16) -band 0xF000
            if ($type -notin 0x8000,0x4000 -or (($type -eq 0x4000) -ne $name.EndsWith('/'))) { throw 'Links and special ZIP members are forbidden on Windows' }
            $total += $entry.Length
            if ($entry.Length -gt 536870912 -or $total -gt 2147483648) { throw 'Expanded bundle size limit exceeded' }
            $entries[$key] = $entry
        }
        $inventory = $entries['nesk/.bundle-manifest']
        if (-not $inventory -or $inventory.Length -gt 4194304) { throw 'Missing or oversized bundle inventory' }
        $reader = [IO.StreamReader]::new($inventory.Open(), [Text.Encoding]::ASCII)
        try { $manifest = $reader.ReadToEnd() } finally { $reader.Dispose() }
        $records = @{}; $expectedCount = 1
        foreach ($line in ($manifest -split "`n")) {
            if (-not $line) { continue }
            if ($line -notmatch '^(F|D) (644|755) ([0-9]{1,10}) ([0-9a-f]{64}|-) ([A-Za-z0-9_,+./-]+)$') { throw 'Invalid bundle inventory' }
            $kind = $Matches[1]; $mode = $Matches[2]; $size = [long]$Matches[3]; $digest = $Matches[4]; $path = $Matches[5]
            $name = "nesk/$path"; $entry = $entries[$name]
            if (-not $entry -or $records.ContainsKey($path) -or $entry.Length -ne $size -or ($kind -eq 'D') -ne $entry.FullName.EndsWith('/')) { throw 'ZIP structure does not match inventory' }
            if (($kind -eq 'F' -and $digest -eq '-') -or ($kind -eq 'D' -and ($size -ne 0 -or $digest -ne '-' -or $mode -ne '755'))) { throw 'Invalid inventory record' }
            $records[$path] = @{ Kind=$kind; Digest=$digest; Entry=$entry }
            $expectedCount++
        }
        if ($expectedCount -ne $entries.Count -or $records['nesk.exe'].Kind -ne 'F' -or $records['_internal'].Kind -ne 'D') { throw 'Incomplete onedir bundle' }
        foreach ($path in $records.Keys) {
            $parent = [IO.Path]::GetDirectoryName($path.Replace('/', '\'))
            while ($parent) {
                if ($records[$parent.Replace('\','/')].Kind -ne 'D') { throw 'Undeclared or non-directory ZIP parent' }
                $parent = [IO.Path]::GetDirectoryName($parent)
            }
        }
        [IO.Directory]::CreateDirectory((Join-Path $Destination "nesk")) | Out-Null
        [IO.File]::WriteAllText((Join-Path $Destination "nesk/.bundle-manifest"), $manifest, [Text.Encoding]::ASCII)
        foreach ($path in $records.Keys) {
            $record = $records[$path]
            $output = Join-Path $Destination "nesk/$path"
            if ($record.Kind -eq 'D') { [IO.Directory]::CreateDirectory($output) | Out-Null; continue }
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($output)) | Out-Null
            $sourceStream = $record.Entry.Open(); $targetStream = [IO.File]::Create($output)
            try {
                $buffer = New-Object byte[] 65536; [long]$written = 0
                while (($count = $sourceStream.Read($buffer,0,$buffer.Length)) -gt 0) {
                    $written += $count
                    if ($written -gt $record.Entry.Length) { throw 'ZIP member exceeds declared size' }
                    $targetStream.Write($buffer,0,$count)
                }
                $targetStream.Flush($true)
            } finally { $targetStream.Dispose(); $sourceStream.Dispose() }
            if ((Get-Sha $output) -ne $record.Digest) { throw "Damaged bundle file: $path" }
        }
    } finally { $zip.Dispose() }
}
function Test-Bundle([string]$Executable, [string]$Version) {
    & $Executable --_bundle-check
    if ($LASTEXITCODE -ne 0) { throw 'Bundle import smoke test failed' }
    $reported = & $Executable --version
    if ($LASTEXITCODE -ne 0 -or "$reported" -notmatch ([regex]::Escape($Version) + '(?:\s|$)')) { throw 'Bundle version smoke test failed' }
    & $Executable --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Bundle help smoke test failed' }
}
$rawArch = $env:PROCESSOR_ARCHITEW6432
if (-not $rawArch) { $rawArch = $env:PROCESSOR_ARCHITECTURE }
switch ($rawArch) { { $_ -in 'AMD64','x86_64','x64' } { $arch='amd64' }; default { throw "Unsupported architecture: $rawArch. Windows requires AMD64 (x64)." } }
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$bin = Join-Path $HOME '.local\bin'; $state = Join-Path $HOME '.local\share\luminesk'
$relativeRoot = '..\share\luminesk\app'
if ($isAdmin) { $bin = Join-Path $env:ProgramFiles 'luminesk'; $state = Join-Path $env:ProgramData 'luminesk'; $relativeRoot = 'app' }
$root = [IO.Path]::GetFullPath((Join-Path $bin $relativeRoot))
$metadataPath = Join-Path $state '.metadata_one_line'
$launcher = Join-Path $bin 'nesk.cmd'; $legacy = Join-Path $bin 'nesk.exe'
Assert-NoLinks $bin; Assert-NoLinks $state; Assert-NoLinks $root
[IO.Directory]::CreateDirectory($bin) | Out-Null
[IO.Directory]::CreateDirectory($state) | Out-Null
[IO.Directory]::CreateDirectory($root) | Out-Null
$lock = $null; $work = $null
try {
    # An OS-held lock is released even after termination or power loss. Keep the
    # file: deleting an open lock creates competing lock domains on POSIX hosts.
    Assert-NoLinks (Join-Path $root 'install.lock')
    try { $lock = [IO.File]::Open((Join-Path $root 'install.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
    catch { throw 'Another installation is active. Retry after it finishes.' }
    $metadata = Read-Metadata $metadataPath
    $owned = $metadata.installed_via -eq 'one-line-script' -and $metadata.binary -in $legacy,$launcher
    if ($metadata.Count -and -not $owned) { throw 'Foreign installation metadata' }
    $existing = Get-Command nesk -ErrorAction SilentlyContinue
    if ($existing -and (-not $owned -or $existing.Source -notin $launcher,$legacy)) { throw "Foreign installation: $($existing.Source). Check uv tool list / pipx list." }
    foreach ($entry in @($launcher,$legacy)) {
        Assert-NoLinks $entry
        if ([IO.File]::Exists($entry) -and -not $owned) { throw "Refusing to overwrite unowned $entry" }
    }
    if ([IO.File]::Exists($legacy) -and $metadata.layout -eq 'onedir') {
        if (-not $metadata.legacy_sha -or (Get-Sha $legacy) -ne $metadata.legacy_sha) { throw 'Foreign executable at the legacy entry point' }
    }
    if ([IO.File]::Exists($launcher)) {
        if ((Get-Sha $launcher) -notin $metadata.launcher_sha,$metadata.previous_launcher_sha) { throw 'Managed launcher was replaced by a foreign file' }
    }
    if ($DeleteMode) {
        if (-not $owned) { Write-Host 'Luminesk is not installed.'; return }
        if (-not (Confirm-Action 'Remove the installer-owned application (keep instances and cache)?')) { return }
        $versions = Join-Path $root 'versions'; Assert-NoLinks $versions
        $directories = @(Get-ChildItem -LiteralPath $versions -Directory -ErrorAction SilentlyContinue)
        foreach ($directory in $directories) {
            Assert-NoLinks $directory.FullName
            if (-not [IO.File]::Exists((Join-Path $directory.FullName '.owned'))) { throw "Unowned version directory: $directory" }
            $inventoryPath = Join-Path $directory.FullName '.installed-files'
            Assert-NoLinks $inventoryPath
            if (-not [IO.File]::Exists($inventoryPath)) { throw "Missing ownership inventory: $directory" }
            $ownedPaths = @{}
            foreach ($relative in [IO.File]::ReadAllLines($inventoryPath)) { $ownedPaths[$relative] = $true }
            foreach ($file in (Get-ChildItem -LiteralPath $directory.FullName -Recurse -Force)) {
                $relative = $file.FullName.Substring($directory.FullName.Length + 1).Replace('\','/')
                if (-not $ownedPaths.ContainsKey($relative)) { throw "Unexpected user files in $directory; move them outside the application before uninstalling." }
                Assert-NoLinks $file.FullName
                if (-not $file.PSIsContainer) {
                    try { $probe = [IO.File]::Open($file.FullName,'Open','ReadWrite','None'); $probe.Dispose() }
                    catch { throw 'Application files are in use. Close running nesk processes and retry removal.' }
                }
            }
        }
        foreach ($entry in @($launcher,$legacy)) { if ([IO.File]::Exists($entry)) { [IO.File]::Delete($entry) } }
        foreach ($directory in $directories) {
            $resolved = [IO.Path]::GetFullPath($directory.FullName)
            if ([IO.Path]::GetDirectoryName($resolved) -ne $versions) { throw 'Invalid removal scope' }
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
        [IO.File]::Delete($metadataPath)
        Write-Host 'Luminesk uninstalled; instances and cache were preserved.'
        return
    }
    $work = Join-Path $root ('.stage.' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($work) | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Receive-VerifiedHost 'https://api.github.com/repos/farin-works/luminesk-cli/releases/latest' "$work\release.json" 4194304
    $release = Get-Content -Raw -LiteralPath "$work\release.json" | ConvertFrom-Json
    if ($release.tag_name -notmatch '^v?(\d+\.\d+\.\d+(?:(?:a|b|rc)[1-9][0-9]*)?)$') { throw 'Invalid release version' }
    $version = $Matches[1]; $assetName = "luminesk-windows-$arch.zip"
    $assets = @($release.assets | Where-Object { $_.name -eq $assetName })
    if ($assets.Count -ne 1) { throw 'Release must contain exactly one matching onedir ZIP' }
    $targetAsset = $assets[0]
    $expectedUrl = "https://github.com/farin-works/luminesk-cli/releases/download/$($release.tag_name)/$assetName"
    if ($targetAsset.browser_download_url -cne $expectedUrl) { throw 'Release asset has an invalid download URL' }
    if ($targetAsset.digest -cnotmatch '^sha256:([0-9a-f]{64})$') { throw 'Release asset has no valid SHA-256 digest' }
    $expectedSha = $Matches[1]
    if ($existing) { & $existing.Source --version; if ($LASTEXITCODE -ne 0) { throw 'Existing command failed' } }
    if (-not (Confirm-Action "Install Luminesk $version (windows-$arch)?")) { return }
    Receive-VerifiedHost $expectedUrl "$work\bundle.zip" 536870912
    if ((Get-Sha "$work\bundle.zip") -ne $expectedSha) { throw 'Checksum verification failed' }
    Expand-Bundle "$work\bundle.zip" $work
    Test-Bundle "$work\nesk\nesk.exe" $version
    $versions = Join-Path $root 'versions'; Assert-NoLinks $versions
    [IO.Directory]::CreateDirectory($versions) | Out-Null
    $id = "$version-$([guid]::NewGuid().ToString('N'))"
    $destination = Join-Path $versions $id
    [IO.Directory]::CreateDirectory($destination) | Out-Null
    [IO.File]::WriteAllText("$destination\.owned", 'one-line-script')
    [IO.Directory]::Move("$work\nesk", "$destination\nesk")
    $ownedPaths = @('.owned', '.installed-files', 'nesk', 'nesk/.bundle-manifest')
    foreach ($line in [IO.File]::ReadAllLines("$destination\nesk\.bundle-manifest")) {
        $ownedPaths += 'nesk/' + ($line -split ' ')[4]
    }
    if ([IO.File]::Exists($legacy)) { $ownedPaths += 'legacy-nesk.exe' }
    [IO.File]::WriteAllLines("$destination\.installed-files", [string[]]$ownedPaths, [Text.Encoding]::ASCII)
    Test-Bundle "$destination\nesk\nesk.exe" $version
    # Relative ASCII target keeps Unicode home directories out of CMD source.
    # Disable delayed expansion, forward the original argument tail and exit code.
    $launcherText = "@echo off`r`nsetlocal DisableDelayedExpansion`r`n`"%~dp0$relativeRoot\versions\$id\nesk\nesk.exe`" %*`r`nexit /b %errorlevel%`r`n"
    [IO.File]::WriteAllText("$work\next.cmd", $launcherText, [Text.UTF8Encoding]::new($false))
    $oldLauncher = if ([IO.File]::Exists($launcher)) { [IO.File]::ReadAllText($launcher) } else { $null }
    $oldMetadata = if ([IO.File]::Exists($metadataPath)) { [IO.File]::ReadAllText($metadataPath) } else { $null }
    $previousSha = if ($oldLauncher) { Get-Sha $launcher } else { '' }
    $legacySha = if ([IO.File]::Exists($legacy)) { Get-Sha $legacy } else { '' }
    $newMetadata = "installed_via=one-line-script`nlayout=onedir`nversion=$version`nbinary=$launcher`nlauncher_sha=$(Get-Sha "$work\next.cmd")`nprevious_launcher_sha=$previousSha`nlegacy_sha=$legacySha`n"
    $migrated = $false
    try {
        # Both old/new launcher hashes are recorded before the atomic replacement,
        # so interruption on either side leaves a recognized working entry point.
        Write-Atomic $metadataPath $newMetadata
        Write-Atomic $launcher $launcherText
        if ([IO.File]::Exists($legacy)) {
            [IO.File]::Move($legacy, "$destination\legacy-nesk.exe")
            $migrated = $true
        }
        & $launcher --version
        if ($LASTEXITCODE -ne 0) { throw 'Activated launcher smoke test failed' }
    } catch {
        if ($migrated) { [IO.File]::Move("$destination\legacy-nesk.exe", $legacy) }
        if ($oldLauncher) { Write-Atomic $launcher $oldLauncher } elseif ([IO.File]::Exists($launcher)) { [IO.File]::Delete($launcher) }
        if ($oldMetadata) { Write-Atomic $metadataPath $oldMetadata } elseif ([IO.File]::Exists($metadataPath)) { [IO.File]::Delete($metadataPath) }
        throw
    }
    $scope = if ($isAdmin) { 'Machine' } else { 'User' }
    $savedPath = [Environment]::GetEnvironmentVariable('Path', $scope)
    if ($bin -notin ($savedPath -split ';')) { [Environment]::SetEnvironmentVariable('Path', "$bin;$savedPath", $scope) }
    if ($bin -notin ($env:Path -split ';')) { $env:Path = "$bin;$env:Path" }
    Write-Host "Luminesk $version installed. Previous versions retained for running processes."
} finally {
    if ($work -and [IO.Directory]::Exists($work)) {
        $resolved = [IO.Path]::GetFullPath($work)
        if ([IO.Path]::GetDirectoryName($resolved) -eq $root -and [IO.Path]::GetFileName($resolved).StartsWith('.stage.')) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
    if ($lock) { $lock.Dispose() }
}
