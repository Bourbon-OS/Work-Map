#requires -Version 7.0
# Bound program maintenance. Normal updates hold the existing shared gate;
# initial/fixed-entry transitions additionally require an external stop boundary.
param([Parameter(Mandatory)][string]$Bundle,[Parameter(Mandatory)][string]$Target,
      [Parameter(Mandatory)][string]$ExpectedBundleHash,
      [ValidateSet('Verify','Apply','Restore')][string]$Mode='Verify',[switch]$UsageStopped)
$ErrorActionPreference='Stop'
$stream=$null;$mutex=$null;$ownsMutex=$false;$marker=$null;$markerHash=$null
$script:GateStream=$null

# The same relative-path/reparse rule is needed before any bundled helper can
# execute. Authenticate every declared member before dot-sourcing that helper.
function Resolve-BootstrapMember([string]$Root,[string]$Relative,[switch]$AllowMissingSuffix){
    $base=[IO.Path]::GetFullPath($Root).TrimEnd('\','/')
    if([IO.Path]::IsPathRooted($Relative) -or $Relative -match '[:*?<>|"]' -or
       @($Relative -split '[\\/]' | Where-Object {$_ -in @('','.','..')}).Count){throw 'Invalid bundle path.'}
    $p=[IO.Path]::GetFullPath((Join-Path $base $Relative))
    if(-not $p.StartsWith($base+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Bundle path escapes root.'}
    $probe=$base;$missing=$false
    foreach($part in @('')+@($Relative -split '[\\/]')){
        if($part){$probe=Join-Path $probe $part}
        if($missing){continue}
        try{$item=Get-Item -LiteralPath $probe -Force}
        catch [Management.Automation.ItemNotFoundException]{
            if(-not $AllowMissingSuffix -or $probe -eq $base){throw}
            $missing=$true;continue
        }
        if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Redirected bundle path.'}
        if($probe -ne $p -and -not $item.PSIsContainer){throw 'Intermediate path is not a directory.'}
    }
    if(-not $AllowMissingSuffix -and -not [IO.File]::Exists($p)){throw 'Required bundle member is not a file.'}
    $p
}
function Get-BootstrapHash([string]$Path){(Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()}
try{
    $bundlePath=[IO.Path]::GetFullPath($Bundle).TrimEnd('\','/')
    $targetPath=[IO.Path]::GetFullPath($Target).TrimEnd('\','/')
    foreach($rootToCheck in @($bundlePath,$targetPath)){
        $ancestor=$rootToCheck
        while($ancestor){
            $item=Get-Item -LiteralPath $ancestor -Force
            if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Redirected maintenance root ancestor.'}
            $ancestor=Split-Path -Parent $ancestor
        }
    }
    $manifestPath=Resolve-BootstrapMember $bundlePath 'BUNDLE_MANIFEST.json'
    if($ExpectedBundleHash -notmatch '^[0-9a-f]{64}$' -or (Get-BootstrapHash $manifestPath) -ne $ExpectedBundleHash){throw 'Expected bundle hash differs.'}
    $manifest=Get-Content -Raw -Encoding utf8 -LiteralPath $manifestPath | ConvertFrom-Json -AsHashtable
    foreach($name in @('apply.ps1','file-functions.ps1','TRANSITION.json')){
        if(-not $manifest.files.ContainsKey($name)){throw 'Required bundle member is undeclared.'}
    }
    if((Get-BootstrapHash $PSCommandPath) -ne $manifest.files['apply.ps1']){throw 'Executing driver differs from the reviewed bundle.'}
    foreach($name in $manifest.files.Keys){
        if($manifest.files[$name] -notmatch '^[0-9a-f]{64}$' -or
           (Get-BootstrapHash (Resolve-BootstrapMember $bundlePath $name)) -ne $manifest.files[$name]){throw "Prepared bytes changed: $name"}
    }
    . (Resolve-BootstrapMember $bundlePath 'file-functions.ps1')
    $script:OriginalHash=${function:Get-UpdateHash}
    function Get-UpdateHash([string]$Path){
        if($script:GateStream -and [IO.Path]::GetFullPath($Path) -eq $script:GatePath){return $script:GateHash}
        & $script:OriginalHash $Path
    }
    $c=Get-Content -Raw -Encoding utf8 -LiteralPath (Resolve-BootstrapMember $bundlePath 'TRANSITION.json') | ConvertFrom-Json -AsHashtable
    $normal=$c.purpose -eq 'workmap-program-update'
    if($c.protocol -ne 1 -or $c.purpose -notin @('workmap-program-update','workmap-program-coordinated','workmap-program-initial') -or
       [IO.Path]::GetFullPath($c.target).TrimEnd('\','/') -ne $targetPath){throw 'Unexpected program transition or target.'}
    if(@($c.changes | Sort-Object -Unique).Count -ne $c.changes.Count){throw 'Duplicate declared change.'}
    $computed=@($c.files.Keys | Where-Object {$c.files[$_].before -ne $c.files[$_].after} | Sort-Object)
    if(($computed -join "`n") -cne (@($c.changes | Sort-Object) -join "`n")){throw 'Undeclared program change.'}
    if(-not $c.files.ContainsKey('INSTALLATION.json') -or -not $c.files.ContainsKey('runtime-use.lock') -or
       $null -eq $c.files['INSTALLATION.json'].after -or $null -eq $c.files['runtime-use.lock'].after){throw 'Missing installation controls.'}
    if($c.purpose -eq 'workmap-program-initial' -and
       ((@($c.changes | Sort-Object) -join ',') -cne 'INSTALLATION.json,runtime-use.lock' -or
        $null -ne $c.files['INSTALLATION.json'].before -or $null -ne $c.files['runtime-use.lock'].before)){
        throw 'First shared settings may only add the two bound controls.'
    }
    $beforeNames=@($c.files.Keys | Where-Object {$null -ne $c.files[$_].before -and $null -ne $c.files[$_].after})
    foreach($name in $c.files.Keys){
        [void](Resolve-BootstrapMember $targetPath $name -AllowMissingSuffix)
        foreach($side in @('before','after')){
            $hash=$c.files[$name][$side]
            if($null -ne $hash -and $hash -notmatch '^[0-9a-f]{64}$'){throw 'Invalid declared file hash.'}
            if($name -in $c.changes -and $null -ne $hash){
                if((Get-UpdateHash (Resolve-BootstrapMember $bundlePath "$side/$name")) -ne $hash){throw 'Staged before/after bytes differ.'}
            }
        }
    }
    foreach($side in @('before','after')){
        if($null -eq $c.directories[$side] -or @($c.directories[$side] | Sort-Object -Unique).Count -ne $c.directories[$side].Count){throw 'Invalid directory inventory.'}
        foreach($name in $c.directories[$side]){[void](Resolve-BootstrapMember $targetPath $name -AllowMissingSuffix)}
    }
    # Only first/fixed-entry transitions need this OS mutex. Normal readers
    # never acquire it; ordinary maintenance uses the existing gate below.
    if(-not $normal){
        $token=[Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($targetPath.ToLowerInvariant())))
        $mutex=[Threading.Mutex]::new($false,('Global\WorkMapBootstrap-'+$token))
        try{$ownsMutex=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$ownsMutex=$true}
        if(-not $ownsMutex){@{status='busy';changed=$false;reason='coordinated maintenance in progress'} | ConvertTo-Json;exit 3}
    }
    $marker=Join-Path $targetPath 'UPDATE_PENDING.json'
    if(Test-UpdateEntryPresent $marker){
        $p=Get-UpdatePath $targetPath 'UPDATE_PENDING.json'
        $pending=Get-Content -Raw -Encoding utf8 -LiteralPath $p | ConvertFrom-Json
        if(@($pending.PSObject.Properties.Name).Count -ne 4 -or $pending.protocol -ne 1 -or
           $pending.installation_id -ne $c.installation_id -or $pending.bundle_sha256 -ne $ExpectedBundleHash -or
           [IO.Path]::GetFullPath($pending.target).TrimEnd('\','/') -ne $targetPath){throw 'Interrupted initial transition marker differs; preserve it.'}
        $markerHash=Get-UpdateHash $p
    }
    $gate=Join-Path $targetPath 'runtime-use.lock'
    if(Test-UpdateEntryPresent $gate){
        [void](Get-UpdatePath $targetPath 'runtime-use.lock')
        try{$stream=[IO.File]::Open($gate,[IO.FileMode]::Open,[IO.FileAccess]::Read,
            $(if($Mode -eq 'Verify'){[IO.FileShare]::Read}else{[IO.FileShare]::None}))}
        catch [IO.IOException]{
            if(($_.Exception.HResult -band 0xffff) -in @(32,33)){
                @{status='busy';changed=$false;reason='participating reader or maintenance remains'} | ConvertTo-Json;exit 3
            };throw
        }
        if($stream.Length -gt 4096){throw 'Oversized gate.'}
        $bytes=[byte[]]::new([int]$stream.Length)
        if($stream.Read($bytes,0,$bytes.Length) -ne $bytes.Length){throw 'Incomplete gate read.'}
        $script:GateStream=$stream;$script:GatePath=$gate
        $script:GateHash=[Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
        $g=[Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
        if($script:GateHash -ne $c.files['runtime-use.lock'].after -or $g.protocol -ne 1 -or $g.installation_id -ne $c.installation_id){throw 'Gate identity differs.'}
    }
    if($normal){
        if(-not $stream){throw 'Normal updates require the existing gate.'}
        $bootPath=Get-UpdatePath $targetPath 'BOOTSTRAP.json'
        $spec=$c.files['BOOTSTRAP.json']
        if($null -eq $spec -or $null -eq $spec.before -or $spec.before -ne $spec.after -or
            (Get-UpdateHash $bootPath) -ne $spec.before){throw 'Fixed-entry inventory changes require a coordinated transition.'}
        $boot=Get-Content -Raw -Encoding utf8 -LiteralPath $bootPath | ConvertFrom-Json -AsHashtable
        if($boot.protocol -ne 1 -or (@($boot.control_files | Sort-Object) -join ',') -cne 'BOOTSTRAP.json,runtime-use.lock' -or
            -not $boot.files.ContainsKey('_workmap_runtime.py')){throw 'Unknown fixed-entry protocol.'}
        foreach($name in @($boot.files.Keys)+@($boot.control_files)){
            $spec=$c.files[$name]
            if($null -eq $spec -or $null -eq $spec.before -or $spec.before -ne $spec.after -or $name -in $c.changes){throw 'Fixed entry cannot change in a normal update.'}
            if($boot.files.ContainsKey($name) -and $boot.files[$name] -ne $spec.before){throw 'Fixed-entry hash differs.'}
        }
    }
    function Get-ProgramDirectories([string]$Root,[string]$Prefix=''){
        foreach($item in Get-ChildItem -LiteralPath $Root -Force){
            if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Redirected installed entry.'}
            if($item.PSIsContainer){
                $name=if($Prefix){$Prefix+'/'+$item.Name}else{$item.Name}
                $name;Get-ProgramDirectories $item.FullName $name
            }
        }
    }
    function Get-ProgramState{
        if(Test-UpdateEntryPresent (Join-Path $targetPath 'INSTALLATION.json')){
            $identity=Get-Content -Raw -Encoding utf8 -LiteralPath (Get-UpdatePath $targetPath 'INSTALLATION.json') | ConvertFrom-Json
            if($identity.installation_id -ne $c.installation_id -or [IO.Path]::GetFullPath($identity.installation_path).TrimEnd('\','/') -ne $targetPath){throw 'Installation identity or location differs.'}
        }elseif($c.purpose -ne 'workmap-program-initial'){throw 'Installation identity is missing.'}
        $actual=@(Get-UpdateFileList $targetPath | Sort-Object)
        if('UPDATE_PENDING.json' -in $actual){
            if(-not $markerHash -or (Get-UpdateHash (Get-UpdatePath $targetPath 'UPDATE_PENDING.json')) -ne $markerHash){throw 'Unexpected or changed recovery marker.'}
            $actual=@($actual | Where-Object {$_ -ne 'UPDATE_PENDING.json'})
        }
        if(@($actual | Where-Object {-not $c.files.ContainsKey($_)}).Count -or
           @($beforeNames | Where-Object {$_ -notin $actual}).Count){throw 'Installed membership differs; preserve unknown items.'}
        $versions=@{}
        foreach($name in $c.files.Keys){
            if($name -notin $actual){$value=$null}else{$value=Get-UpdateHash (Get-UpdatePath $targetPath $name)}
            if($value -ne $c.files[$name].before -and $value -ne $c.files[$name].after){throw "Later edit or unknown installed bytes: $name"}
            $versions[$name]=$value
        }
        $dirs=@(Get-ProgramDirectories $targetPath | Sort-Object)
        if(@($dirs | Where-Object {$_ -notin @($c.directories.before+$c.directories.after)}).Count -or
           @($c.directories.before | Where-Object {$_ -in $c.directories.after -and $_ -notin $dirs}).Count){throw 'Installed directories differ.'}
        foreach($side in @('before','after')){
            if(($dirs -join "`n") -cne (@($c.directories[$side] | Sort-Object) -join "`n")){continue}
            if(-not @($c.files.Keys | Where-Object {$versions[$_] -ne $c.files[$_][$side]}).Count){return $side}
        }
        'mixed'
    }
    function Remove-BoundMarker{
        if((Get-UpdateHash (Get-UpdatePath $targetPath 'UPDATE_PENDING.json')) -ne $markerHash){throw 'Recovery marker changed; preserve it.'}
        [IO.File]::Delete($marker)
    }
    function Restore-Program{
        [void](Get-ProgramState)
        foreach($name in $c.directories.before){
            [void](Resolve-BootstrapMember $targetPath $name -AllowMissingSuffix)
            [IO.Directory]::CreateDirectory((Join-Path $targetPath $name)) | Out-Null
        }
        foreach($name in $c.changes){
            if($null -eq $c.files[$name].before){continue}
            Restore-UpdateFile $targetPath $name $c.files[$name] $bundlePath
        }
        foreach($name in $c.changes){
            if($null -ne $c.files[$name].before){continue}
            $p=Resolve-BootstrapMember $targetPath $name -AllowMissingSuffix
            if([IO.File]::Exists($p)){Restore-UpdateFile $targetPath $name $c.files[$name] $bundlePath}
        }
        $newDirs=@($c.directories.after | Where-Object {$_ -notin $c.directories.before} | Sort-Object {$_.Split('/').Count} -Descending)
        foreach($name in $newDirs){
            $p=Join-Path $targetPath $name
            if([IO.Directory]::Exists($p)){
                [void](Get-UpdatePath $targetPath $name)
                if(@(Get-ChildItem -LiteralPath $p -Force).Count){throw 'Introduced directory is not empty; preserve it.'}
                [IO.Directory]::Delete($p)
            }
        }
        if((Get-ProgramState) -ne 'before'){throw 'Original program not restored.'}
    }
    $state=Get-ProgramState
    if($Mode -eq 'Verify'){@{status='verified';state=$state;changed=$false;recovery_required=[bool]$markerHash;bundle_sha256=$ExpectedBundleHash} | ConvertTo-Json;exit 0}
    if(-not $normal -and -not $UsageStopped){throw 'An external stopped-usage boundary is required for first/fixed-entry Apply/Restore.'}
    $desired=if($Mode -eq 'Apply'){'after'}else{'before'}
    if($c.changes.Count -eq 0 -or $state -eq $desired){
        $status='already_'+$desired
        if($markerHash){Remove-BoundMarker;if($Mode -eq 'Apply'){$status='completed_after'}}
        @{status=$status;state=$desired;changed=$false;recovery_required=$false} | ConvertTo-Json;exit 0
    }
    if($Mode -eq 'Apply' -and ($state -ne 'before' -or $markerHash)){throw 'Partial initial transition requires Restore before reapplying.'}
    if(-not $markerHash){
        $value=[ordered]@{protocol=1;installation_id=$c.installation_id;bundle_sha256=$ExpectedBundleHash;target=$targetPath}
        $data=[Text.UTF8Encoding]::new($false).GetBytes(($value | ConvertTo-Json -Compress)+"`n")
        $writer=[IO.File]::Open($marker,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try{$writer.Write($data,0,$data.Length);$writer.Flush($true)}finally{$writer.Dispose()}
        $markerHash=Get-UpdateHash $marker
    }
    # The gate/fixed entries can change during a coordinated transition. After its pending mark
    # is durable, close this probe; the external stop boundary stays in force.
    if(-not $normal -and $stream){$stream.Dispose();$stream=$null;$script:GateStream=$null}
    try{
        if($Mode -eq 'Restore'){Restore-Program}
        else{
            foreach($name in $c.directories.after){
                [void](Resolve-BootstrapMember $targetPath $name -AllowMissingSuffix)
                [IO.Directory]::CreateDirectory((Join-Path $targetPath $name)) | Out-Null
            }
            foreach($name in $c.changes){
                if($null -eq $c.files[$name].after){
                    $p=Resolve-BootstrapMember $targetPath $name -AllowMissingSuffix
                    if([IO.File]::Exists($p)){
                        if((Get-UpdateHash $p) -ne $c.files[$name].before){throw 'Removed program file changed; preserve it.'}
                        [IO.File]::Delete($p)
                    }
                    continue
                }
                $parent=Split-Path -Parent (Join-Path $targetPath $name)
                if(-not [IO.Directory]::Exists($parent)){[IO.Directory]::CreateDirectory($parent) | Out-Null}
                [void](Get-UpdatePath $targetPath $name -AllowMissingLeaf)
                Write-UpdateFile $targetPath $name (Resolve-BootstrapMember $bundlePath "after/$name") $false
            }
            $removedDirs=@($c.directories.before | Where-Object {$_ -notin $c.directories.after} | Sort-Object {$_.Split('/').Count} -Descending)
            foreach($name in $removedDirs){
                $p=Resolve-BootstrapMember $targetPath $name -AllowMissingSuffix
                if([IO.Directory]::Exists($p)){
                    if(@(Get-ChildItem -LiteralPath $p -Force).Count){throw 'Removed program directory is not empty; preserve it.'}
                    [IO.Directory]::Delete($p)
                }
            }
            if((Get-ProgramState) -ne 'after'){throw 'Program post-verification failed.'}
        }
        Remove-BoundMarker
        @{status=if($Mode -eq 'Apply'){'applied'}else{'restored'};state=$desired;changed=$true;recovery_required=$false;bundle_sha256=$ExpectedBundleHash} | ConvertTo-Json
    }catch{
        $problem=$_.Exception.Message
        if($Mode -eq 'Apply'){
            try{Restore-Program;Remove-BoundMarker
                @{status='failed_restored';state='before';error=$problem;rollback_verified=$true;recovery_required=$false} | ConvertTo-Json;exit 1
            }catch{throw "Program update failed: $problem; restoration requires inspection: $($_.Exception.Message)"}
        }
        throw "Program restoration interrupted; keep the external stop boundary: $problem"
    }
}catch{
    @{status='rejected_or_requires_inspection';error=$_.Exception.Message;changed_not_assumed=$true;
      recovery_required=if($marker){[IO.File]::Exists($marker)}else{$false}} | ConvertTo-Json;exit 2
}finally{
    if($stream){$stream.Dispose()}
    if($ownsMutex){$mutex.ReleaseMutex()}
    if($mutex){$mutex.Dispose()}
}
