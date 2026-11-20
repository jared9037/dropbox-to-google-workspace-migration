# check_all.ps1 -- read-only. For every job, checks that each listed file is in Google with the
# same size. Writes C:\migration\logs\check_summary.csv (job, files, missing).
#   powershell -ExecutionPolicy Bypass -File C:\migration\jumpbox\check_all.ps1
$base = "C:\migration"
$rc   = "C:\rclone\rclone.exe"
$out  = @()
foreach ($j in Import-Csv "$base\jumpbox\jobs.csv") {
    $src = "dropbox:/" + $j.'Team Folder'
    $dst = "gd,team_drive=$($j.'Shared Drive ID'),root_folder_id=$($j.'Root Folder ID'),impersonate=$($j.Account):"
    $miss = "$base\logs\missing_$($j.List)"
    & $rc check $src $dst --files-from-raw "$base\jumpbox\lists\$($j.List)" --one-way --size-only `
        --missing-on-dst $miss --differ "$miss.differ" --tpslimit 8 2> $null
    $m = 0; if (Test-Path $miss) { $m = @(Get-Content $miss).Count }
    $d = 0; if (Test-Path "$miss.differ") { $d = @(Get-Content "$miss.differ").Count }
    $out += [pscustomobject]@{ List = $j.List; Drive = $j.Drive; Files = $j.Files; Missing = $m; Different = $d }
    "$($j.List): $m missing, $d different (of $($j.Files))"
}
$out | Export-Csv "$base\logs\check_summary.csv" -NoTypeInformation
"TOTAL missing: $(($out | Measure-Object Missing -Sum).Sum), different: $(($out | Measure-Object Different -Sum).Sum)"
