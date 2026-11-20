# status.ps1 -- last progress line and latest speed per account, plus how many rclone copies are running.
#   powershell -ExecutionPolicy Bypass -File C:\migration\scripts\windows\status.ps1
$logs = "C:\migration\logs"
Get-ChildItem "$logs\*.progress.txt" | Sort-Object Name | ForEach-Object {
    $u = $_.BaseName -replace '\.progress$', ''
    $speed = Select-String -Path "$logs\$u.log" -Pattern "ETA" -ErrorAction SilentlyContinue | Select-Object -Last 1
    "{0,-12} {1}" -f $u, (Get-Content $_.FullName -Tail 1)
    "{0,-12} {1}" -f "", ($(if ($speed) { $speed.Line } else { "(no stats yet)" }))
}
"rclone processes running: " + @(Get-Process rclone -ErrorAction SilentlyContinue).Count
