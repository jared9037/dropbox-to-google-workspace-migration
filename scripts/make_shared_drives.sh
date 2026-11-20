#!/bin/bash
# Creates Migration Drive 10–17, same format as Transfer9:
# 3 Managers + a "dropbox" folder. Logs name, drive ID, folder ID to new_drives.tsv.
set -eo pipefail
cd "$(dirname "$0")"
G="$HOME/bin/gam7/gam select default"
U=migration-admin@example.com
ok() { [[ "$1" =~ ^[A-Za-z0-9_-]{10,}$ ]] || { echo "GAM did not return an ID for $2"; exit 1; }; }
for n in 10 11 12 13 14 15 16 17; do
  name="Migration Drive $n"
  drive=$($G user $U create shareddrive "$name" returnidonly | tail -1); ok "$drive" "$name"
  sleep 5
  for m in migration-admin2@example.com migration-admin3@example.com; do
    $G user $U create drivefileacl "$drive" user $m role organizer > /dev/null
  done
  folder=$($G user $U create drivefile drivefilename dropbox mimetype gfolder parentid "$drive" returnidonly | tail -1); ok "$folder" "$name/dropbox"
  printf '%s\t%s\t%s\n' "$name" "$drive" "$folder" | tee -a new_drives.tsv
done
