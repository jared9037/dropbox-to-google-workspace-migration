#!/bin/bash
# run_parallel_copy.sh -- Mac version of the jump box run (jumpbox/run_all.ps1 + run_account.ps1).
# Copies the Dropbox leftovers into Google with rclone, 15 copyworker accounts in parallel, each
# running its own jobs from jumpbox/jobs.csv one by one. COPY ONLY (--ignore-existing): never
# deletes, moves or overwrites anything. Re-runnable: files already in Google are skipped.
# NEVER run this while the jump box is also copying (two copies of one file = duplicates in Drive).
#   bash run_parallel_copy.sh start    -> starts all 15 accounts in the background (keeps the Mac awake)
#   bash run_parallel_copy.sh status   -> last progress line + latest speed per account
#   bash run_parallel_copy.sh stop     -> stops everything (safe; re-run start later to resume)
#   bash run_parallel_copy.sh dry LIST -> dry run of one job (e.g. copyworker_T1_000.txt), changes nothing
cd ~/Downloads || exit 1
RC=~/bin/rclone
LOGS=~/Downloads/mac_logs
mkdir -p "$LOGS"

jobs_tsv() {  # Account, Team Folder, Shared Drive ID, Root Folder ID, List, Files, GB
  python3 -I -c '
import csv
for r in csv.DictReader(open("jumpbox/jobs.csv", encoding="utf-8")):
    print(r["Account"], r["Team Folder"], r["Shared Drive ID"], r["Root Folder ID"], r["List"], r["Files"], r["GB"], sep="\t")'
}

copy_job() {  # acct team sd root list log [extra flags]
  local acct=$1 team=$2 sd=$3 root=$4 list=$5 log=$6; shift 6
  "$RC" copy "dropbox:/$team" "gdrive1,team_drive=$sd,root_folder_id=$root,impersonate=$acct:" \
    --files-from-raw "jumpbox/lists/$list" --ignore-existing \
    --drive-stop-on-upload-limit --no-update-dir-modtime --transfers 4 --checkers 4 --tpslimit 4 \
    --drive-chunk-size 32M --retries 5 --low-level-retries 20 \
    --log-file "$log" --log-level INFO --stats 10m --stats-one-line "$@" </dev/null
}

run_account() {
  local acct=$1 user=${1%@*} prog="$LOGS/${1%@*}.progress.txt"
  while IFS=$'\t' read -r _ team sd root list files gb; do
    local try=1
    while :; do
      echo "$(date '+%F %T') START $list ($files files, $gb GB) try $try" >> "$prog"
      copy_job "$acct" "$team" "$sd" "$root" "$list" "$LOGS/$user.log"
      code=$?
      echo "$(date '+%F %T') END   $list exit $code" >> "$prog"
      # exit 7 = rclone's "upload limit" stop. Google also sends that message for short rate limits,
      # so wait and retry the same job twice before treating it as the real 750 GB/day cap.
      if [ $code -eq 7 ] && [ $try -lt 3 ]; then try=$((try+1)); sleep 900; continue; fi
      if [ $code -eq 7 ]; then echo "$(date '+%F %T') STOPPED (750 GB/day cap?). Run 'bash run_parallel_copy.sh start' again tomorrow." >> "$prog"; exit 0; fi
      break
    done
  done < <(jobs_tsv | awk -F'\t' -v a="$acct" '$1==a')
  echo "$(date '+%F %T') DONE $acct" >> "$prog"
}

case ${1:-} in
  start)
    if pgrep -f "run_parallel_copy.sh (account|launch)" >/dev/null; then echo "Already running. Use: bash run_parallel_copy.sh status"; exit 1; fi
    nohup bash "$0" launch >/dev/null 2>&1 &
    echo "Starting 15 accounts, one every ${STAGGER:-600} s (Dropbox throttles if they all list files at once). Check: bash run_parallel_copy.sh status" ;;
  launch)
    for acct in $(jobs_tsv | cut -f1 | awk '!s[$0]++'); do
      nohup caffeinate -i bash "$0" account "$acct" >/dev/null 2>&1 &
      sleep "${STAGGER:-600}"
    done ;;
  account) run_account "$2" ;;
  status)
    for f in "$LOGS"/*.progress.txt; do
      u=$(basename "$f" .progress.txt)
      speed=$(grep "ETA" "$LOGS/$u.log" 2>/dev/null | tail -1 | sed -E 's/^([0-9\/]+ [0-9:]+) INFO *: */\1 /')
      printf "%-12s %s\n%12s %s\n" "$u" "$(tail -1 "$f")" "" "$speed"
    done
    echo "rclone processes running: $(pgrep -f 'bin/rclone copy dropbox:' | wc -l | tr -d ' ')" ;;
  stop)
    pkill -f "run_parallel_copy.sh launch"; pkill -f "run_parallel_copy.sh account"; pkill -f "bin/rclone copy dropbox:"; echo "Stopped." ;;
  dry)
    jobs_tsv | awk -F'\t' -v l="$2" '$5==l' | while IFS=$'\t' read -r acct team sd root list files gb; do
      echo "Dry run $list: $acct, $files files, $gb GB"
      copy_job "$acct" "$team" "$sd" "$root" "$list" "$LOGS/dry_$list.log" --dry-run
      echo "exit $?; would copy: $(grep -c 'Skipped copy as --dry-run' "$LOGS/dry_$list.log"), errors: $(grep -c ERROR "$LOGS/dry_$list.log")"
    done ;;
  *) sed -n 2,10p "$0" ;;
esac
