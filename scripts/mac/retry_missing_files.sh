#!/bin/bash
# retry_missing_files.sh -- copies stragglers one by one by exact path (rclone copyto), 6 at a time.
# Why: the job lists use Google's folder casing (e.g. QUIPS) while Dropbox may say Quips; rclone's
# --files-from matching is case-sensitive, so those files were silently skipped. Dropbox path lookups
# are case-insensitive, so copyto with the list path reads the right file and writes it into
# Google's existing folder. COPY ONLY (--ignore-existing).
#   input: retry/files.tsv  (Account, Team Folder, Shared Drive ID, Root Folder ID, Path) tab-separated
cd "$(dirname "$0")/.." || exit 1
L=retry/logs; mkdir -p "$L"
one() {
  IFS=$'\t' read -r acct team sd root p <<< "$1"
  rclone copyto "dropbox:/$team/$p" "gdrive,team_drive=$sd,root_folder_id=$root,impersonate=$acct:$p" \
    --ignore-existing --no-update-dir-modtime --drive-chunk-size 64M --retries 5 --low-level-retries 20 \
    --log-file retry/logs/copyto.log --log-level INFO </dev/null
  echo "$? $p" >> retry/logs/copyto_results.txt
}
export -f one
# -n 1 (not -I {}): macOS xargs -I refuses arguments longer than 255 bytes and stops early.
tr '\n' '\0' < retry/files.tsv | xargs -0 -P 6 -n 1 bash -c 'one "$1"' _
echo "$(date '+%T') ALLDONE" >> retry/logs/copyto_results.txt
