#!/bin/bash
# progress_check.sh -- READ-ONLY. Counts files the jump box rclone run has created in the transfer
# drives since it started (Nov 13 ~10:05 AM CST = 16:05Z; we use 15:30Z to be safe) and their size.
#   bash progress_check.sh
cd ~/Downloads || exit 1
OUT=~/Downloads/progress_logs; mkdir -p "$OUT"
STAMP=$(date +%Y%m%d_%H%M)
Q="createdTime > '2026-11-13T15:30:00' and mimeType != 'application/vnd.google-apps.folder'"
for pair in 1:0AEXAMPLEDRIVE04 2:0AEXAMPLEDRIVE05 3:0AEXAMPLEDRIVE06 4:0AEXAMPLEDRIVE07 \
            5:0AEXAMPLEDRIVE08 6:0AEXAMPLEDRIVE09 7:0AEXAMPLEDRIVE10 8:0AEXAMPLEDRIVE11 10:0AEXAMPLEDRIVE13; do
  n=${pair%%:*}; id=${pair#*:}
  PATH="$HOME/bin/gam7:$PATH" gam redirect csv "$OUT/T${n}_$STAMP.csv" user migration-admin@example.com \
    print filelist select shareddriveid "$id" query "$Q" fields id,size,createdtime >/dev/null 2>&1
done
python3 -I - "$OUT" "$STAMP" <<'EOF'
import csv, glob, os, sys
out, stamp = sys.argv[1], sys.argv[2]
tf = tb = 0; last = ""
for f in sorted(glob.glob(f"{out}/T*_{stamp}.csv"), key=lambda x: int(os.path.basename(x)[1:].split("_")[0])):
    rows = list(csv.DictReader(open(f, encoding="utf-8")))
    b = sum(int(r.get("size") or 0) for r in rows)
    if rows:
        last = max([last] + [r.get("createdTime", "") for r in rows])
        print(f"  {os.path.basename(f).split('_')[0]:>4}: {len(rows):>7,} files {b/1e9:>8,.1f} GB")
    tf += len(rows); tb += b
print(f"TOTAL since start: {tf:,} files, {tb/1e9:,.1f} GB (of 156,742 files / 16,130 GB). Newest file created: {last}")
EOF
