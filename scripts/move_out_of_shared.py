#!/usr/bin/env python3
"""
move_out_of_shared.py -- moves the BigTeam content that sits inside nested shared
folders (which Google's import can't see) into a normal folder in the same team folder:

    /Big Team Folder/_Moved for import/<original path>

Only things inside a shared folder move. Normal files next to them stay where they are.
The folder structure is kept under "_Moved for import", and the emptied shared folders
are left in place. Moves happen on Dropbox's servers (nothing is downloaded) and keep
every file's version history; nothing is deleted.

  python3 move_out_of_shared.py plan                    # read-only: what would move
  python3 move_out_of_shared.py move                    # asks you to type MOVE
  add --only "SUBFOLDER A" to do one folder

Then import ONE row:  /Big Team Folder/_Moved for import  -> Transfer9.
Outputs: moved_plan.csv, moved_results.csv (log). Needs dropbox_inventory.py.
"""
import collections
import csv
import os
import sys
import time

import dropbox_inventory as d
from delete_confirmed import slim, ancestors

BIGTEAM_NS = "1234567890"
DEST = "/_Moved for import"
TOPS = ["SUBFOLDER A", "SUBFOLDER B", "SUBFOLDER C", "SUBFOLDER D", "SUBFOLDER E", "SUBFOLDER F"]
PLAN_CSV = "moved_plan.csv"
RESULTS_CSV = "moved_results.csv"
BATCH = 500
MODE = None


def call(endpoint, payload):
    global MODE
    if MODE:
        return d.call(endpoint, payload, BIGTEAM_NS, MODE)
    last = None
    for m in ("user", "admin"):
        try:
            r = d.call(endpoint, payload, BIGTEAM_NS, m)
            MODE = m
            return r
        except d.ApiError as e:
            last = e
            if e.status not in (400, 403):
                raise
    raise last


def list_tree(top):
    r = call("files/list_folder", {"path": "/" + top, "recursive": True, "limit": 2000})
    out = [slim(e) for e in r["entries"]]
    while r.get("has_more"):
        r = call("files/list_folder/continue", {"cursor": r["cursor"]})
        out += [slim(e) for e in r["entries"]]
    return out


def plan_top(top):
    """Returns (units to move, folders to create at the destination, file count moved, files staying)."""
    entries = list_tree(top)
    by_path = {e["path_lower"]: e for e in entries}
    shared = {e["path_lower"] for e in entries
              if e.get(".tag") == "folder" and (e.get("sharing_info") or {}).get("shared_folder_id")}
    holds_shared = set()
    for s in shared:
        holds_shared.update(ancestors(s))
        holds_shared.add(s)
    split = holds_shared  # folders we go inside instead of moving whole
    units, files_moved, files_stay = [], 0, 0
    files_under = collections.Counter()
    for e in entries:
        if e.get(".tag") == "file":
            for a in ancestors(e["path_lower"]):
                files_under[a] += 1
    for e in entries:
        pl = e["path_lower"]
        parent = pl.rsplit("/", 1)[0]
        inside_shared = any(a in shared for a in ancestors(pl))
        if not inside_shared or pl in split or parent not in split:
            if e.get(".tag") == "file" and not inside_shared:
                files_stay += 1
            continue
        n = 1 if e.get(".tag") == "file" else files_under[pl]
        units.append((e.get(".tag"), e["path_display"], n))
        files_moved += n
    mk = sorted({by_path[p]["path_display"] for p in split if p in by_path and any(a in shared for a in ancestors(p) + [p])},
                key=lambda p: p.count("/"))
    return units, mk, files_moved, files_stay


def wait_job(endpoint, launch):
    status = launch
    while status.get(".tag") in ("async_job_id", "in_progress"):
        time.sleep(2)
        status = call(endpoint, {"async_job_id": launch["async_job_id"]})
    return status


def move_units(units, log, top):
    errors = 0
    chunks, cur, cur_files = [], [], 0          # keep each batch under ~8k files and BATCH moves
    for u in units:
        if cur and (len(cur) >= BATCH or cur_files + u[2] > 8000):
            chunks.append(cur)
            cur, cur_files = [], 0
        cur.append(u)
        cur_files += u[2]
    if cur:
        chunks.append(cur)
    done = 0
    for chunk in chunks:
        entries = [{"from_path": p, "to_path": DEST + p} for _, p, _ in chunk]
        status = wait_job("files/move_batch/check_v2",
                          call("files/move_batch_v2", {"entries": entries, "autorename": False}))
        if status.get(".tag") != "complete":
            # whole batch refused (e.g. too many files in one go): send them one at a time
            results = []
            for e in entries:
                try:
                    call("files/move_v2", {**e, "autorename": False})
                    results.append({".tag": "success"})
                except d.ApiError as ex:
                    results.append({".tag": "failure", "failure": ex.summary or ex.body[:200]})
        else:
            results = status.get("entries", [])
        for (kind, p, n), r in zip(chunk, results):
            ok = r.get(".tag") == "success"
            errors += not ok
            log.writerow({"Top": top, "Type": kind, "From": p, "To": DEST + p, "Files": n,
                          "Result": "moved" if ok else f"ERROR: {str(r.get('failure', r))[:200]}"})
        done += len(chunk)
        print(f"  ... {done:,}/{len(units):,} moves sent ({errors:,} errors)", end="\r", flush=True)
    print()
    return errors


def count_files(path):
    try:
        r = call("files/list_folder", {"path": path, "recursive": True, "limit": 2000})
    except d.ApiError:
        return 0
    n = sum(1 for e in r["entries"] if e[".tag"] == "file")
    while r.get("has_more"):
        r = call("files/list_folder/continue", {"cursor": r["cursor"]})
        n += sum(1 for e in r["entries"] if e[".tag"] == "file")
    return n


def main():
    args = sys.argv[1:]
    mode = args[0] if args else ""
    if mode not in ("plan", "move"):
        sys.exit(__doc__)
    tops = TOPS
    if "--only" in args:
        name = args[args.index("--only") + 1]
        tops = [t for t in TOPS if t.lower() == name.lower()]
        if not tops:
            sys.exit(f"--only must be one of: {', '.join(TOPS)}")
    if not d.TOKEN:
        d.refresh_access_token()

    if mode == "move":
        if not os.path.exists(PLAN_CSV):
            sys.exit("Run plan first so you can see what will move.")
        with open(PLAN_CSV, newline="") as fh:
            prev = [r for r in csv.DictReader(fh) if r["Top"] in tops]
        print(f"The plan moves {sum(int(r['Files']) for r in prev):,} files ({len(prev):,} moves) into "
              f"'Big Team Folder{DEST}'.")
        if input("Type MOVE to move them (each folder is re-listed first): ").strip() != "MOVE":
            sys.exit("Cancelled. Nothing was moved.")
        new = not os.path.exists(RESULTS_CSV)
        log_fh = open(RESULTS_CSV, "a", newline="")
        log = csv.DictWriter(log_fh, fieldnames=["Top", "Type", "From", "To", "Files", "Result"])
        if new:
            log.writeheader()

    rows, total_move, total_stay = [], 0, 0
    for top in tops:
        print(f"{top}")
        units, mk, n_move, n_stay = plan_top(top)
        total_move += n_move
        total_stay += n_stay
        print(f"  {n_move:,} files inside shared folders to move ({len(units):,} moves); {n_stay:,} normal files stay")
        for kind, p, n in units:
            rows.append({"Top": top, "Type": kind, "From": p, "To": DEST + p, "Files": n})
        if mode == "move" and units:
            for folder in mk:
                try:
                    call("files/create_folder_v2", {"path": DEST + folder, "autorename": False})
                except d.ApiError as e:
                    if "conflict" not in (e.summary or e.body):
                        raise
            errors = move_units(units, log, top)
            log_fh.flush()
            after = count_files(DEST + "/" + top)
            print(f"  moved ({errors:,} errors); files now under {DEST}/{top}: {after:,} (planned {n_move:,})")
    if mode == "plan":
        with open(PLAN_CSV, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["Top", "Type", "From", "To", "Files"])
            w.writeheader()
            w.writerows(rows)
        print(f"\nWould move {total_move:,} files; {total_stay:,} normal files stay put. Nothing changed. Plan in {PLAN_CSV}.")
    else:
        log_fh.close()
        print(f"\nDone. Log in {RESULTS_CSV}. Next: import '/Big Team Folder{DEST}' -> Transfer9.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nStopped. Rerun plan, then move; already-moved items just won't be listed again.")
    except d.NetworkError as e:
        sys.exit(f"\nGave up after repeated network errors ({e}).")
    except d.ApiError as e:
        sys.exit(f"\nDropbox refused a request: {e}")
