# run_account.ps1 -- copies one copyworker account's share of the Dropbox leftovers into Google.
# Started by run_all.ps1 (one window per account). Safe to re-run: --ignore-existing skips files
# already in Google, so after an account hits Google's 750 GB/day limit just run it again the next day.
param([Parameter(Mandatory = $true)][string]$Account)

$base = "C:\migration"
$rc   = "C:\rclone\rclone.exe"
$user = $Account.Split("@")[0]
New-Item -ItemType Directory -Force "$base\logs" | Out-Null
$log  = "$base\logs\$user.log"
$jobs = Import-Csv "$base\jumpbox\jobs.csv" | Where-Object { $_.Account -eq $Account }
$host.UI.RawUI.WindowTitle = "rclone $user"

foreach ($j in $jobs) {
    $src = "dropbox:/" + $j.'Team Folder'
    $dst = "gd,team_drive=$($j.'Shared Drive ID'),root_folder_id=$($j.'Root Folder ID'),impersonate=${Account}:"
    $list = "$base\jumpbox\lists\$($j.List)"
    $stop = $false
    for ($try = 1; $try -le 3; $try++) {
        "$(Get-Date -Format s) START $($j.List) -> $($j.Drive) ($($j.Files) files, $($j.GB) GB) try $try" | Tee-Object -FilePath "$base\logs\$user.progress.txt" -Append
        # No --no-traverse: with it, every account looks up each file before copying and Dropbox throttles
        # ("Too many requests ... 300 seconds"). Without it rclone lists folders and starts copying right away.
        & $rc copy $src $dst --files-from-raw $list --ignore-existing `
            --drive-stop-on-upload-limit --no-update-dir-modtime --transfers 4 --checkers 4 --tpslimit 4 `
            --drive-chunk-size 32M --retries 5 --low-level-retries 20 `
            --log-file $log --log-level INFO --stats 10m --stats-one-line
        $code = $LASTEXITCODE
        "$(Get-Date -Format s) END   $($j.List) exit $code" | Tee-Object -FilePath "$base\logs\$user.progress.txt" -Append
        if ($code -ne 7) { break }
        # exit 7 = rclone's "upload limit" stop. Google also sends that message for short rate limits,
        # so wait 15 min and retry the same job twice before treating it as the real 750 GB/day cap.
        if ($try -lt 3) { Start-Sleep -Seconds 900 } else { $stop = $true }
    }
    if ($stop) {
        "$(Get-Date -Format s) STOPPED (750 GB/day cap?). Run run_all.ps1 again tomorrow." | Tee-Object -FilePath "$base\logs\$user.progress.txt" -Append
        break
    }
}
"$(Get-Date -Format s) DONE $user" | Tee-Object -FilePath "$base\logs\$user.progress.txt" -Append
