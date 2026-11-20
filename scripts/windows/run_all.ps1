# run_all.ps1 -- opens one minimized PowerShell window per copyworker account, each running
# run_account.ps1, one every 10 minutes (keep this window open ~2.5 h until all 15 have started).
# Closing an account's window stops that account. NEVER run this while the Mac copy (run_parallel_copy.sh) is running.
#   powershell -ExecutionPolicy Bypass -File C:\migration\scripts\windows\run_all.ps1
$base = "C:\migration"
if (-not (Test-Path "$base\oauth2service.json")) { throw "Missing $base\oauth2service.json" }
if (-not ((& C:\rclone\rclone.exe listremotes) -contains "gd:")) {   # 'config show' exits 0 even if missing (v1.76)
    & C:\rclone\rclone.exe config create gd drive scope=drive service_account_file="$base\oauth2service.json" --non-interactive | Out-Null
}
$accounts = Import-Csv "$base\jumpbox\jobs.csv" | Select-Object -ExpandProperty Account -Unique
foreach ($a in $accounts) {
    Start-Process powershell -WindowStyle Minimized -ArgumentList @(
        "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "$base\jumpbox\run_account.ps1", "-Account", $a)
    "$(Get-Date -Format T) started $a"
    Start-Sleep -Seconds 600   # 10 min apart: Dropbox throttles if all accounts list files at once
}
"Started $($accounts.Count) accounts. Progress: Get-Content $base\logs\*.progress.txt -Tail 2"
