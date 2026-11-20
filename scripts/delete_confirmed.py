#!/usr/bin/env python3
"""
delete_confirmed.py -- deletes from Dropbox only the files that are confirmed to
be in Google Drive. Everything else stays exactly where it is in Dropbox.

  1. Export the shared drives with GAM into drive1_files.csv ... drive9_files.csv
     next to this script (drive4-9 only matter for BigTeam).
  2. python3 delete_confirmed.py plan     # dry run: matches Dropbox against Drive, changes nothing
  3. python3 delete_confirmed.py delete   # deletes the confirmed files (asks first)

A Dropbox file counts as confirmed only if Drive has a file at the same path
inside the same team folder, with the same size, and the Drive copy isn't older
than the Dropbox version (files whose only newer timestamp came from the
Sept 30 folder renames count as unchanged). Anything that doesn't match stays in Dropbox and is
listed in kept_in_dropbox.csv with the reason.

When every file in a folder is confirmed, the folder is deleted in one go.
Shared folders nested inside team folders are never deleted themselves, only
the confirmed files in them. Delete re-checks each team folder against Dropbox
right before deleting, so nothing added after the plan gets caught.

Skips Big Team Folder unless you name it: --only "Big Team Folder".
Add --only "Old Products" to do any single team folder.
Deletes go to Dropbox's deleted files, so they can still be restored for a while.
Needs dropbox_inventory.py in the same folder. Python standard library only.
"""
import collections
import csv
import datetime
import os
import re
import sys
import time
import unicodedata

import dropbox_inventory as d

DRIVE_CSVS = [f"drive{i}_files.csv" for i in range(1, 18)]  # Transfer10-17 added Nov 10
# Transfer4-9 hold only BigTeam, imported row by row under their "dropbox" folder, so the path
# there starts inside BigTeam (no "Big Team Folder" in it).
BIGTEAM_ONLY = {"drive4", "drive5", "drive6", "drive7", "drive8", "drive9"}
BIGTEAM = "big team folder"
PLAN_CSV = "delete_plan.csv"
KEEP_CSV = "kept_in_dropbox.csv"
RESULTS_CSV = "delete_results.csv"
SKIP = {"big team folder"}
FOLDER_MIME = "application/vnd.google-apps.folder"
BATCH = 250
# rename_bad_characters.py renamed folders on this day, which bumped Dropbox's server_modified on
# every file inside them without touching their contents.
RENAME_DAY = "2026-09-30"

csv.field_size_limit(10 ** 9)


def norm(s):
    return unicodedata.normalize("NFC", s or "").lower()


def ts(s):
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    m = re.match(r"(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)", s)
    if m and m.group(2):                       # trim fractions to 6 digits for older Pythons
        s = m.group(1) + (m.group(2) + "000000")[:7] + m.group(3)
    try:
        t = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------- Google side

def load_drive(team_keys):
    """(team folder, path inside it) -> [(size, modified, drive)] for every file in the three drives.
    A team folder only counts at the level where the import created the team folders, so a
    subfolder that happens to share a team folder's name deeper down never matches."""
    index = collections.defaultdict(list)
    rows_seen = 0
    for path in DRIVE_CSVS:
        label = path.split("_")[0]
        bigteam_only = label in BIGTEAM_ONLY
        if bigteam_only and BIGTEAM not in team_keys:
            continue
        if not os.path.exists(path):
            sys.exit(f"Missing {path}. Run the GAM exports first.")
        root_depth, indexed = (2 if bigteam_only else None), 0
        for pass_no in (1, 2):
            with open(path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                cols = {c.lower(): c for c in reader.fieldnames or []}
                pathcols = [c for c in reader.fieldnames or []
                            if re.fullmatch(r"(full)?paths?(\.\d+)?", c.lower()) and c.lower() != "paths"]
                missing = [n for n in ("name", "mimetype", "size", "modifiedtime") if n not in cols]
                if missing or not pathcols:
                    # GAM leaves out columns nothing in the export has, e.g. no size column when
                    # the drive holds only folders. That's fine as long as there are no files.
                    rows = list(reader)
                    mime = cols.get("mimetype")
                    if mime and all(r.get(mime) == FOLDER_MIME for r in rows):
                        if pass_no == 1:
                            print(f"{path}: {len(rows)} item(s), no files in it.")
                            rows_seen += len(rows)
                        break
                    sys.exit(f"{path} is missing columns {missing or ['path']}. Rerun its GAM export exactly as given.")
                for row in reader:
                    is_folder = row[cols["mimetype"]] == FOLDER_MIME
                    if pass_no == 1:
                        rows_seen += 1
                        if is_folder and not bigteam_only:
                            for pc in pathcols:
                                parts = (row.get(pc) or "").split("/")
                                if parts[-1] and norm(parts[-1]) in team_keys:
                                    dpt = len(parts) - 1
                                    root_depth = dpt if root_depth is None else min(root_depth, dpt)
                        continue
                    if is_folder or not row[cols["size"]]:
                        continue
                    name = row[cols["name"]]
                    for pc in pathcols:
                        p = row.get(pc) or ""
                        if not p:
                            continue
                        parts = p.split("/")
                        if parts[-1] != name:
                            parts.append(name)
                        if bigteam_only:
                            if len(parts) < 4 or norm(parts[2]) != "dropbox":
                                continue
                            team, rel = BIGTEAM, "/" + "/".join(parts[3:])
                        elif len(parts) > root_depth + 1 and norm(parts[root_depth]) in team_keys:
                            team, rel = norm(parts[root_depth]), "/" + "/".join(parts[root_depth + 1:])
                        elif len(parts) > 4 and norm(parts[2]) == "dropbox" and norm(parts[3]) in team_keys:
                            # newer import roots under a top-level "dropbox" folder, in a drive whose
                            # shallowest team folder sits at the root (Transfer3's TF_00)
                            team, rel = norm(parts[3]), "/" + "/".join(parts[4:])
                        else:
                            continue
                        index[(team, norm(rel))].append(
                            (int(row[cols["size"]]), ts(row[cols["modifiedtime"]]), label))
                        indexed += 1
                        break
            if pass_no == 1 and root_depth is None:
                if os.path.getsize(path) and rows_seen:
                    print(f"{path}: no team folders in it.")
                break
        if root_depth is not None:
            print(f"{path}: {indexed:,} files under team folders.")
    print(f"Read {rows_seen:,} Drive items in total.")
    return index


# ---------------------------------------------------------------- Dropbox side

KEEP = (".tag", "id", "path_lower", "path_display", "size", "client_modified", "server_modified")


def slim(e):
    out = {k: e[k] for k in KEEP if k in e}
    si = e.get("sharing_info") or {}
    if si:
        out["sharing_info"] = {k: si[k] for k in ("parent_shared_folder_id", "shared_folder_id") if k in si}
    return out


def list_all(ns):
    for attempt in range(3):
        entries = []
        try:
            result, mode = d.first_page(ns)
            while True:
                entries.extend(slim(e) for e in result.get("entries", []))   # keeps memory down on BigTeam
                if not result.get("has_more"):
                    return entries
                result = d.call("files/list_folder/continue", {"cursor": result["cursor"]}, ns, mode)
        except d.ApiError as e:
            if e.summary.startswith("reset") and attempt < 2:
                print("  Dropbox reset the listing; starting this folder over")
                continue
            raise


def is_shared(e):
    return e.get(".tag") == "folder" and bool((e.get("sharing_info") or {}).get("shared_folder_id"))


def ancestors(path_lower):
    parts = path_lower.strip("/").split("/")
    return ["/" + "/".join(parts[:i]) for i in range(1, len(parts))]


def verdict(e, cands):
    """(None, drive) if confirmed in Google, otherwise (reason it stays, Google's modified time)."""
    if not cands:
        return "not in Google", ""
    size = e.get("size", 0)
    same = [(m, lab) for s, m, lab in cands if s == size]
    if not same:
        return "different size in Google", ""
    cm, sm = ts(e.get("client_modified")), ts(e.get("server_modified"))
    for m, lab in same:
        if not (m and cm):
            continue
        if abs((m - cm).total_seconds()) <= 2 or (sm and m >= sm):
            return None, lab
        # only newer because our rename pass touched its folder: content time no newer than Google's copy
        if sm and sm.date().isoformat() == RENAME_DAY and cm <= m + datetime.timedelta(seconds=2):
            return None, lab
    return "changed in Dropbox after Google copied it", max((m for m, _ in same if m), default="")


def plan_folder(ns, team, index):
    entries = list_all(ns)
    files = [e for e in entries if e.get(".tag") == "file"]
    tkey = norm(team)
    keep, ok, where = [], set(), {}
    for e in files:
        why, extra = verdict(e, index.get((tkey, norm(e.get("path_display") or e["path_lower"]))))
        if why:
            keep.append((e, why, extra))
        else:
            ok.add(e["path_lower"])
            where[e["path_lower"]] = extra
    folder_at = {e["path_lower"]: e for e in entries if e.get(".tag") == "folder"}
    shared_below = set()
    for e in entries:
        if is_shared(e):
            shared_below.update(ancestors(e["path_lower"]))
            shared_below.add(e["path_lower"])
    tot, good, size_under = collections.Counter(), collections.Counter(), collections.Counter()
    drives_under = collections.defaultdict(set)
    for e in files:
        for a in ancestors(e["path_lower"]):
            tot[a] += 1
            size_under[a] += e.get("size", 0)
            good[a] += e["path_lower"] in ok
            if e["path_lower"] in ok:
                drives_under[a].add(where[e["path_lower"]])
    whole = sorted(a for a in tot if tot[a] == good[a] and a in folder_at and a not in shared_below)
    targets, covered = [], set()
    for a in whole:                                   # parents sort before their contents
        if any(x in covered for x in ancestors(a)):
            continue
        covered.add(a)
        targets.append(("folder", a, tot[a], size_under[a], "+".join(sorted(drives_under[a]))))
    for e in files:
        pl = e["path_lower"]
        if pl in ok and not any(x in covered for x in ancestors(pl)):
            targets.append(("file", pl, 1, e.get("size", 0), where[pl]))
    return targets, keep, len(files)


def run_batch(paths, mode):
    """One delete_batch job; returns its per-entry results."""
    launch = d.call("files/delete_batch", {"entries": [{"path": p} for p in paths]}, None, mode)
    status = launch
    while status.get(".tag") in ("async_job_id", "in_progress"):
        time.sleep(2)
        status = d.call("files/delete_batch/check", {"async_job_id": launch["async_job_id"]}, None, mode)
    if status.get(".tag") != "complete":
        raise RuntimeError(f"batch ended as {status.get('.tag')}")
    return status.get("entries", [])


def delete_one(path):
    last = "unknown error"
    for mode in ("user", "admin"):
        for attempt in range(5):
            try:
                d.call("files/delete_v2", {"path": path}, None, mode)
                return "deleted"
            except d.ApiError as e:
                last = e.summary or e.body[:200]
                if "not_found" in last:
                    return "already gone"
                if any(k in last for k in ("internal_error", "too_many_write_operations", "too_many_files")):
                    time.sleep(3 * (attempt + 1))
                    continue
                break
    return f"ERROR: {last}"


def delete_batch(paths):
    """Deletes up to BATCH paths; returns {path: result}. Deleting is idempotent, so when
    Dropbox's job errors out on its side the same batch is simply sent again, and anything
    still failing after that is deleted one at a time."""
    results, pending = {}, list(paths)
    for attempt in range(4):
        if not pending:
            break
        mode = "user" if attempt < 2 else "admin"
        try:
            entries = run_batch(pending, mode)
        except (d.ApiError, RuntimeError) as e:
            print(f"\n  [Dropbox couldn't finish a batch ({getattr(e, 'summary', '') or e}); retrying]")
            time.sleep(5 * (attempt + 1))
            continue
        retry = []
        for p, r in zip(pending, entries):
            if r.get(".tag") == "success":
                results[p] = "deleted"
                continue
            text = str(r.get("failure", r))
            if "not_found" in text:
                results[p] = "already gone"
            else:
                results[p] = f"ERROR: {text[:200]}"
                retry.append(p)
        pending = retry
    for p in pending:
        results[p] = delete_one(p)
    return results


# ---------------------------------------------------------------- commands

def folders_in_scope(only):
    folders = [f for f in d.list_team_folders()
               if f.get("status", {}).get(".tag") == "active" and not f.get("is_team_shared_dropbox")
               and (only or norm(f["name"]) not in SKIP)]      # BigTeam only when asked for by name
    if only:
        folders = [f for f in folders if norm(f["name"]) == norm(only)]
        if not folders:
            sys.exit(f'No active team folder named "{only}".')
    return folders


def run(do_delete, only):
    folders = folders_in_scope(only)
    index = load_drive({norm(f["name"]) for f in folders})
    if do_delete:
        if not os.path.exists(PLAN_CSV):
            sys.exit("Run plan first so you can see what will go.")
        with open(PLAN_CSV, newline="") as fh:
            prev = list(csv.DictReader(fh))
        n_files = sum(int(r["Files"]) for r in prev)
        gb = sum(float(r["Size (GB)"]) for r in prev)
        print(f"\nThe plan has {n_files:,} confirmed files ({gb:,.1f} GB) across "
              f"{len({r['Team Folder'] for r in prev})} team folders.")
        if input("Type DELETE to delete them from Dropbox (each folder is re-checked first): ").strip() != "DELETE":
            sys.exit("Cancelled. Nothing was deleted.")

    plan_rows, keep_rows, summary = [], [], []
    log = None
    if do_delete:
        new = not os.path.exists(RESULTS_CSV)
        log_fh = open(RESULTS_CSV, "a", newline="")
        log = csv.DictWriter(log_fh, fieldnames=["Team Folder", "Type", "Path", "Files", "Result"])
        if new:
            log.writeheader()
    for i, f in enumerate(folders, 1):
        ns, team = f["team_folder_id"], f["name"]
        print(f"[{i}/{len(folders)}] {team}")
        try:
            targets, keep, total = plan_folder(ns, team, index)
        except d.ApiError as e:
            print(f"  couldn't list it: {e}")
            continue
        n_ok = sum(t[2] for t in targets)
        gb = sum(t[3] for t in targets) / 1e9
        summary.append((team, total, n_ok, len(keep)))
        print(f"  {total:,} files: {n_ok:,} confirmed in Google ({gb:,.1f} GB), {len(keep):,} stay")
        for kind, pl, n, size, drv in targets:
            plan_rows.append({"Team Folder": team, "Type": kind, "Path": pl, "Files": n,
                              "Size (GB)": f"{size / 1e9:.3f}", "In Google": drv})
        for e, why, gm in keep:
            keep_rows.append({"Team Folder": team, "Path": e.get("path_display") or e["path_lower"],
                              "Size (MB)": f"{e.get('size', 0) / 1e6:.2f}", "Reason": why,
                              "Dropbox client_modified": e.get("client_modified", ""),
                              "Dropbox server_modified": e.get("server_modified", ""),
                              "Google modified": gm.isoformat() if gm else ""})
        if do_delete and targets:
            errors = 0
            for start in range(0, len(targets), BATCH):
                chunk = targets[start:start + BATCH]
                res = delete_batch([f"ns:{ns}{t[1]}" for t in chunk])
                for kind, pl, n, _, _ in chunk:
                    r = res.get(f"ns:{ns}{pl}", "ERROR: no result")
                    errors += r.startswith("ERROR")
                    log.writerow({"Team Folder": team, "Type": kind, "Path": pl, "Files": n, "Result": r})
                log_fh.flush()
                done = min(start + BATCH, len(targets))
                print(f"  ... {done:,}/{len(targets):,} deletes sent ({errors:,} errors)", end="\r", flush=True)
            print(f"\n  deleted ({errors:,} errors)")
    if log:
        log_fh.close()

    if not do_delete:
        with open(PLAN_CSV, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["Team Folder", "Type", "Path", "Files", "Size (GB)", "In Google"])
            w.writeheader()
            w.writerows(plan_rows)
    with open(KEEP_CSV, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["Team Folder", "Path", "Size (MB)", "Reason", "Dropbox client_modified",
                                           "Dropbox server_modified", "Google modified"])
        w.writeheader()
        w.writerows(keep_rows)

    tot = sum(s[1] for s in summary)
    ok = sum(s[2] for s in summary)
    print(f"\n{'Deleted' if do_delete else 'Would delete'} {ok:,} of {tot:,} files; {tot - ok:,} stay in Dropbox.")
    reasons = collections.Counter(r["Reason"] for r in keep_rows)
    for why, n in reasons.most_common():
        print(f"  {n:>9,} {why}")
    drives = collections.Counter()
    for r in plan_rows:
        drives[r["In Google"]] += int(r["Files"])
    print("  confirmed copies found in: " + ", ".join(f"{k or '?'} {v:,}" for k, v in drives.most_common()))
    changed = collections.Counter(r["Team Folder"] for r in keep_rows if r["Reason"].startswith("changed"))
    if changed:
        print("  'changed' files by team folder: " + ", ".join(f"{k} {v:,}" for k, v in changed.most_common(6)))
    fully = [s[0] for s in summary if s[1] and s[3] == 0]
    if fully:
        print(f"\nFully confirmed, so nothing stays ({len(fully)}): " + ", ".join(fully))
    if do_delete:
        print(f"\nLog in {RESULTS_CSV}; what stayed is in {KEEP_CSV}.")
    else:
        print(f"\nNothing was changed. Plan in {PLAN_CSV}; what would stay is in {KEEP_CSV}.")
        print("If it looks right:  python3 delete_confirmed.py delete")


def main():
    args = sys.argv[1:]
    mode = args[0] if args else "plan"
    only = args[args.index("--only") + 1] if "--only" in args and args.index("--only") + 1 < len(args) else None
    if mode not in ("plan", "delete"):
        sys.exit(__doc__)
    if not d.TOKEN:
        d.refresh_access_token()
    run(mode == "delete", only)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nStopped. Rerun the same command; it re-checks everything, so it picks up cleanly.")
    except d.NetworkError as e:
        sys.exit(f"\nGave up after repeated network errors ({e}). Rerun to continue.")
    except d.ApiError as e:
        sys.exit(f"\nDropbox refused a request: {e}")
