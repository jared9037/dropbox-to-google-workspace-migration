# Windows jump box / server version

The same parallel rclone copy as [`../mac/run_parallel_copy.sh`](../mac/run_parallel_copy.sh), written in PowerShell
for an always-on Windows machine.

**Works on:** Windows Server 2016 or newer and Windows 10/11. It only uses Windows PowerShell 5.1, which is built
into all of them, plus `rclone.exe`.

| Script | What it does |
|---|---|
| `run_all.ps1` | Starts one window per copy account, 10 minutes apart (Dropbox throttles if they all start at once) |
| `run_account.ps1` | Runs one account's jobs in order; retries Google's false "daily limit" stop twice before giving up |
| `status.ps1` | Last progress line and current speed for every account |
| `stop_all.ps1` | Stops every copy (safe: running `run_all.ps1` again resumes and skips what's done) |
| `check_all.ps1` | Read-only: confirms every listed file is in Google with the same size |

Setup on the machine: put these files plus `jobs.csv` and `lists\` in `C:\migration\jumpbox`, rclone at
`C:\rclone\rclone.exe`, then:

```powershell
powershell -ExecutionPolicy Bypass -File C:\migration\jumpbox\run_all.ps1
```

Set the machine to never sleep while plugged in (`powercfg /change standby-timeout-ac 0`).
