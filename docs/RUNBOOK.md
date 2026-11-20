# Runbook

The order things were run in. IDs and emails are placeholders.

## 0. Setup

```bash
# Dropbox app credentials, outside any repo
mkdir -p ~/.config/dropbox_migration && chmod 700 ~/.config/dropbox_migration
cp config/credentials.example.json ~/.config/dropbox_migration/credentials.json   # fill in, then:
chmod 600 ~/.config/dropbox_migration/credentials.json
python3 scripts/reauth_dropbox.py        # only when app scopes change
```

## 1. Inventory (read-only)

```bash
python3 scripts/dropbox_inventory.py
python3 scripts/remaining_inventory.py   # resumable
```

## 2. Plan and run Google's Data import

```bash
bash scripts/make_shared_drives.sh                 # destination drives + managers
python3 scripts/plan_import_split.py plan          # ~1 h, resumable, read-only
python3 scripts/plan_import_split.py csv           # writes import CSVs (and a folder script if needed)
```
Upload each CSV in Admin console → Data import → Dropbox as a **delta run on the same job**. Wait for "Complete".

## 3. Export Google and delete what's confirmed

```bash
gam redirect csv ./drive1_files.csv user migration-admin@example.com print filelist \
  select shareddriveid 0AEXAMPLEDRIVE01 fields id,name,mimetype,size,modifiedtime fullpath excludetrashed
python3 scripts/delete_confirmed.py plan                       # dry run
python3 scripts/delete_confirmed.py delete                     # asks for DELETE, re-checks live
```

## 4. rclone for the leftovers

```bash
python3 scripts/plan_rclone_jobs.py                # jobs.csv + per-account lists + folders to pre-create
bash scripts/precreate_folders.sh dry && bash scripts/precreate_folders.sh go
bash scripts/run_parallel_copy.sh dry <list>.txt   # one job, changes nothing
bash scripts/run_parallel_copy.sh start            # 15 accounts, staggered
bash scripts/run_parallel_copy.sh status
bash scripts/run_parallel_copy.sh stop             # safe; start again resumes
bash scripts/retry_missing_files.sh                # stragglers by exact path
```
Windows jump box equivalent: `scripts/windows/run_all.ps1`, `status.ps1`, `stop_all.ps1`, `check_all.ps1`.

## 5. Final verify and delete

Re-export the drives that received files, then repeat step 3. What stays is listed with a reason in `kept_in_dropbox.csv`.
