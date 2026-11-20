#!/bin/bash
# precreate_folders.sh -- creates the 23,690 missing folders (jumpbox/mkdirs.csv) in Google BEFORE the
# jump box starts copying, so the 15 parallel accounts never create the same folder twice.
# Only creates empty folders (as migration-admin via GAM's service account); never deletes anything.
# Re-runnable: folders that already exist are skipped. Uses almost no bandwidth.
#   bash precreate_folders.sh dry    -> counts what it would create, changes nothing
#   bash precreate_folders.sh go     -> creates them (~2-3 h; keeps the Mac awake while running)
set -u
cd ~/Downloads || exit 1
MODE=${1:-dry}
FLAG="--dry-run"; [ "$MODE" = go ] && FLAG=""
TREE=~/Downloads/mkdirs_tree
LOG=~/Downloads/mkdirs_logs
mkdir -p "$LOG"; rm -rf "$TREE"   # rebuild from the current lists every time

# 1. local empty copy of the folder tree, one subfolder per list
python3 -I - <<'EOF'
import csv, os
tree = os.path.expanduser("~/Downloads/mkdirs_tree")
for r in csv.DictReader(open("jumpbox/mkdirs.csv", encoding="utf-8")):
    base = os.path.join(tree, r["List"][:-4])
    os.makedirs(base, exist_ok=True)
    for line in open("jumpbox/lists/" + r["List"], encoding="utf-8"):
        p = line.rstrip("\n")
        if p:
            os.makedirs(os.path.join(base, p), exist_ok=True)
EOF

# 2. push each list's tree into its existing Google root folder
tail -n +2 jumpbox/mkdirs.csv | python3 -I -c '
import csv, sys
for r in csv.reader(sys.stdin):
    print(r[2], r[3], r[4][:-4], r[5], sep="\t")' |
while IFS=$'\t' read -r drive root name want; do
  caffeinate -i ~/bin/rclone copy "$TREE/$name" "gdrive1,team_drive=$drive,root_folder_id=$root:" \
    --create-empty-src-dirs --ignore-existing --tpslimit 3 --retries 5 --low-level-retries 20 \
    $FLAG --log-file "$LOG/$name.$MODE.log" --log-level INFO </dev/null
  code=$?
  todo=$(grep -c "Skipped make directory" "$LOG/$name.$MODE.log" 2>/dev/null)
  made=$(grep -c "Making directory" "$LOG/$name.$MODE.log" 2>/dev/null)
  errs=$(grep -c "ERROR" "$LOG/$name.$MODE.log" 2>/dev/null)
  echo "$(date +%H:%M) $name: listed $want, would-create $todo, created $made, errors $errs, exit $code"
  rm -f "$LOG/$name.$MODE.log.prev"; mv "$LOG/$name.$MODE.log" "$LOG/$name.$MODE.log.prev"
done
echo "Done ($MODE). Logs in $LOG (*.prev). Run 'bash precreate_folders.sh dry' again after 'go': would-create should be 0 everywhere."
