#requires -Version 7.0
$ErrorActionPreference = 'Stop'

function Get-UpdateHash([string]$Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-UpdateEntryPresent([string]$Path) {
    # Enumerating the checked parent also sees dangling links. Test-Path
    # alone could mistake an unreadable or dangling entry for absence.
    $leaf = Split-Path -Leaf $Path
    @((Get-ChildItem -LiteralPath (Split-Path -Parent $Path) -Force -ErrorAction Stop) |
        Where-Object { $_.Name -ieq $leaf }).Count -gt 0
}

function Get-UpdatePath([string]$Root, [string]$Relative, [switch]$AllowMissingLeaf) {
    $base = [IO.Path]::GetFullPath($Root).TrimEnd('\','/')
    if ([IO.Path]::IsPathRooted($Relative) -or $Relative -match '[:*?<>|"]' -or
        @($Relative -split '[\\/]' | Where-Object { $_ -in @('', '.', '..') }).Count) {
        throw 'Invalid relative update path.'
    }
    $path = [IO.Path]::GetFullPath((Join-Path $base $Relative))
    if (-not $path.StartsWith($base+[IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Update path escapes its root.'
    }
    $probe = $base
    foreach ($part in @('') + @($Relative -split '[\\/]')) {
        if ($part) { $probe = Join-Path $probe $part }
        if ($AllowMissingLeaf -and $probe -eq $path) {
            $entry = @((Get-ChildItem -LiteralPath (Split-Path -Parent $probe) -Force -ErrorAction Stop) |
                Where-Object { $_.Name -ieq (Split-Path -Leaf $probe) })
            if ($entry.Count -eq 0) { continue }
            if ($entry.Count -ne 1) { throw 'Ambiguous update path.' }
            $item = $entry[0]
        } else { $item = Get-Item -LiteralPath $probe -Force -ErrorAction Stop }
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Redirected update path.'
        }
    }
    $path
}

function Get-UpdateFileList([string]$Root, [string]$Prefix = '') {
    foreach ($item in Get-ChildItem -LiteralPath $Root -Force) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Redirected installed entry.' }
        $relative = if ($Prefix) { "$Prefix/$($item.Name)" } else { $item.Name }
        if ($item.PSIsContainer) { Get-UpdateFileList $item.FullName $relative }
        else { $relative }
    }
}

function Test-UpdateAfterInstallation([string]$Path, [string]$Template) {
    try {
        $actual = Get-Content -Raw -Encoding UTF8 -LiteralPath $Path | ConvertFrom-Json -AsHashtable
        $expected = Get-Content -Raw -Encoding UTF8 -LiteralPath $Template | ConvertFrom-Json -AsHashtable
        $stamp = $actual.last_updated_at
        if ($null -eq $stamp -or $actual.local_updates[-1].applied_at -ne $stamp) { return $false }
        if ($stamp -is [DateTime] -or $stamp -is [DateTimeOffset]) { [void]$stamp.ToString('o') }
        elseif ($stamp -is [string]) { [void][DateTimeOffset]::Parse($stamp) }
        else { return $false }
        $actual.last_updated_at = $null
        $actual.local_updates[-1].applied_at = $null
        ($actual | ConvertTo-Json -Depth 60 -Compress) -ceq ($expected | ConvertTo-Json -Depth 60 -Compress)
    } catch { $false }
}

function Get-UpdateVersion([string]$Target, [string]$Relative, $Spec, [string]$Bundle) {
    $path = Get-UpdatePath $Target $Relative -AllowMissingLeaf:($null -eq $Spec.before)
    if (-not (Test-UpdateEntryPresent $path)) {
        if ($null -eq $Spec.before) { return 'before' }
        return 'unknown'
    }
    $hash = Get-UpdateHash $path
    if ($hash -eq $Spec.before) { return 'before' }
    if ($Relative -eq 'INSTALLATION.json') {
        if (Test-UpdateAfterInstallation $path (Get-UpdatePath $Bundle 'after/INSTALLATION.json')) { return 'after' }
    } elseif ($hash -eq $Spec.after) { return 'after' }
    'unknown'
}

function Get-UpdateState([string]$Target, $Config, [string]$Bundle) {
    # Resolve the declared installation through the safe-path check before
    # enumerating anything under the target root.
    $identity = Get-Content -Raw -Encoding UTF8 -LiteralPath (Get-UpdatePath $Target 'INSTALLATION.json') | ConvertFrom-Json
    if ($identity.installation_id -ne $Config.installation_id -or
        [IO.Path]::GetFullPath($identity.installation_path) -ne [IO.Path]::GetFullPath($Target)) {
        throw 'Installed identity or location changed.'
    }
    $actual = @(Get-UpdateFileList $Target | Sort-Object)
    $allowed = @($Config.files.Keys)
    $required = @($Config.files.Keys | Where-Object { $null -ne $Config.files[$_].before })
    if (@($actual | Where-Object { $_ -notin $allowed }).Count -or
        @($required | Where-Object { $_ -notin $actual }).Count) {
        throw 'Installed file membership changed; inspect before updating.'
    }
    $versions = @{}
    foreach ($relative in $Config.files.Keys) {
        $spec = $Config.files[$relative]
        $version = Get-UpdateVersion $Target $relative $spec $Bundle
        if ($version -eq 'unknown') { throw "Installed file is neither approved version: $relative" }
        if ($relative -notin $Config.changes -and $spec.before -ne $spec.after) { throw 'Undeclared update change.' }
        $versions[$relative] = $version
    }
    $states = @($Config.changes | ForEach-Object { $versions[$_] } | Select-Object -Unique)
    if ($states.Count -eq 1) { return $states[0] }
    'mixed'
}

function Write-UpdateFile([string]$Target, [string]$Relative, [string]$Source, [bool]$InstallationTemplate) {
    $dest = Get-UpdatePath $Target $Relative -AllowMissingLeaf
    $temp = Join-Path (Split-Path -Parent $dest) ('.work-map-update-'+[guid]::NewGuid().ToString('N')+'.tmp')
    try {
        if ($InstallationTemplate) {
            $metadata = Get-Content -Raw -Encoding UTF8 -LiteralPath $Source | ConvertFrom-Json -AsHashtable
            $stamp = [DateTimeOffset]::UtcNow.ToString('o')
            $metadata.last_updated_at = $stamp
            $metadata.local_updates[-1].applied_at = $stamp
            [IO.File]::WriteAllText($temp, ($metadata | ConvertTo-Json -Depth 60)+"`n", [Text.UTF8Encoding]::new($false))
        } else { Copy-Item -LiteralPath $Source -Destination $temp }
        [IO.File]::Move($temp, $dest, $true)
    } finally {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force }
    }
}

function Restore-UpdateFile([string]$Target, [string]$Relative, $Spec, [string]$Bundle) {
    if ($null -ne $Spec.before) {
        Write-UpdateFile $Target $Relative (Get-UpdatePath $Bundle "before/$Relative") $false
        return
    }
    # This batch's only declared addition can return to its approved absence.
    # Never remove a different file or content changed after the update.
    $dest = Get-UpdatePath $Target $Relative -AllowMissingLeaf
    if (Test-UpdateEntryPresent $dest) {
        if ((Get-UpdateHash $dest) -ne $Spec.after) { throw 'Added file changed; do not delete it.' }
        [IO.File]::Delete($dest)
        if (Test-UpdateEntryPresent $dest) { throw 'Added file could not return to absence.' }
    }
}

