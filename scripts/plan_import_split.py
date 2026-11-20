#!/usr/bin/env python3
"""
plan_import_split.py -- splits "Big Team Folder" across several Google
shared drives and writes the Google import CSVs for it.

A shared drive holds at most 500,000 items, so BigTeam (~1.9M) can't go into one.
Google's import CSV accepts subfolder paths, so the split happens in the CSV: each
BigTeam subfolder becomes its own row, pointed at the shared drive it should land
in. Nothing in Dropbox moves, except loose files sitting directly in a folder that
had to be split (the optional "loose" step tucks them into a "_Loose Files"
subfolder so a row can cover them).

  1. python3 plan_import_split.py plan
       Read-only. Lists BigTeam in Dropbox (about an hour, resumable), skips whatever
       the GAM exports show is already in Google, splits the rest into chunks of at
       most 400,000 items, and says how many new shared drives that takes. Writes
       bigteam_plan.csv (what goes where) and a bigteam_drives.csv template.
  2. Create that many shared drives, add the Target GUsers as Managers, make a folder
     in each, and paste each folder's ID (or URL) into bigteam_drives.csv.
  3. python3 plan_import_split.py loose    (only if plan says there are loose files)
  4. python3 plan_import_split.py csv
       If any folder had to be split, this first writes bigteam_precreate_folders.sh, a few GAM
       commands that recreate the split folder's name inside each new drive so its pieces
       keep their place in the folder tree. Run  bash bigteam_precreate_folders.sh  then csv again.
       Writes bigteam_import_1.csv, bigteam_import_2.csv, ... Upload one per import
       run, in order (each run respects Google's 150-row and per-GUser limits).

Needs dropbox_inventory.py and the three GAM exports (drive1_files.csv,
drive2_files.csv, drive3_files.csv) in the same folder.
"""
import collections
import csv
import json
import os
import re
import shlex
import sys
import time
import unicodedata

import dropbox_inventory as d

TEAM = "Big Team Folder"
FILL_TO = 400000           # most items we'll put in any shared drive (Google's cap is 500,000)
GUSER_CAP = 400000         # most new items per Target GUser in one import run
MAX_ROWS = 150             # Google: up to 150 folders per import
MAX_DEPTH = 5              # how deep we'll split folders that are too big
GUSERS = ["migration-admin@example.com", "migration-admin2@example.com", "migration-admin3@example.com"]
EXISTING = {"drive1": "1EXAMPLEFOLDER01",   # Migration Drive 1
            "drive2": "1EXAMPLEFOLDER02",   # Migration Drive 2
            "drive3": "1EXAMPLEFOLDER03"}   # Migration Drive 3
DRIVE_CSVS = ["drive1_files.csv", "drive2_files.csv", "drive3_files.csv"]
STATE = "bigteam_crawl_state.json"
PLAN_JSON = "bigteam_plan.json"
PLAN_CSV = "bigteam_plan.csv"
DRIVES_CSV = "bigteam_drives.csv"
LOOSE_CSV = "bigteam_loose_moves.csv"
LOOSE_DIR = "_Loose Files"
PARENTS_TSV = "bigteam_parent_folders.tsv"
MAKE_SH = "bigteam_precreate_folders.sh"
GAM_USER = "migration-admin@example.com"
FOLDER_MIME = "application/vnd.google-apps.folder"
CHECKPOINT_EVERY = 25

csv.field_size_limit(10 ** 9)


def norm(s):
    return unicodedata.normalize("NFC", s or "").lower()


def bigteam_ns():
    for f in d.list_team_folders():
        if norm(f["name"]) == norm(TEAM) and f.get("status", {}).get(".tag") == "active":
            return f["team_folder_id"]
    sys.exit(f'No active team folder named "{TEAM}".')


# ---------------------------------------------------------------- Google side

def gam_signature():
    return [[p, os.path.getsize(p), int(os.path.getmtime(p))] for p in DRIVE_CSVS if os.path.exists(p)]


def load_google():
    """Every BigTeam path already in Google -> which drive it's in; plus items per drive."""
    paths, totals = {}, {}
    key = norm(TEAM)
    for path in DRIVE_CSVS:
        if not os.path.exists(path):
            sys.exit(f"Missing {path}. Run the three GAM exports first.")
        label = path.split("_")[0]
        depth, n, rows = None, 0, 0
        for pass_no in (1, 2):
            with open(path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                cols = {c.lower(): c for c in reader.fieldnames or []}
                pathcols = [c for c in reader.fieldnames or []
                            if re.fullmatch(r"(full)?paths?(\.\d+)?", c.lower()) and c.lower() != "paths"]
                if "name" not in cols or "mimetype" not in cols or not pathcols:
                    rows = sum(1 for _ in reader)
                    break
                for row in reader:
                    if pass_no == 1:
                        rows += 1
                        if row[cols["mimetype"]] == FOLDER_MIME and norm(row[cols["name"]]) == key:
                            for pc in pathcols:
                                parts = (row.get(pc) or "").split("/")
                                if parts[-1] and norm(parts[-1]) == key:
                                    depth = len(parts) - 1 if depth is None else min(depth, len(parts) - 1)
                        continue
                    name = row[cols["name"]]
                    for pc in pathcols:
                        q = row.get(pc) or ""
                        if not q:
                            continue
                        parts = q.split("/")
                        if parts[-1] != name:
                            parts.append(name)
                        if len(parts) > depth + 1 and norm(parts[depth]) == key:
                            paths.setdefault(norm("/" + "/".join(parts[depth + 1:])), label)
                            n += 1
                            break
            if depth is None:
                break
        totals[label] = rows
        print(f"{path}: {rows:,} items, {n:,} of them BigTeam")
    return paths, totals


# ---------------------------------------------------------------- Dropbox side

def new_agg():
    return {"total": 0, "in_google": 0, "units": {}, "names": {}, "loose": {}, "shares": 0}


def tally(agg, e, ns, google):
    tag = e.get(".tag")
    if tag not in ("file", "folder"):
        return
    lower = [p for p in (e.get("path_lower") or "").split("/") if p]
    shown = [p for p in (e.get("path_display") or "").split("/") if p]
    k = len(lower)
    if not k:
        return
    agg["total"] += 1
    if tag == "folder" and k <= MAX_DEPTH and shown:
        agg["names"]["/".join(lower)] = shown[-1]
    if norm(e.get("path_display") or e.get("path_lower")) in google:
        agg["in_google"] += 1
        return
    si = e.get("sharing_info") or {}
    nested = bool(si.get("parent_shared_folder_id")) and si.get("parent_shared_folder_id") != ns
    if tag == "folder" and si.get("shared_folder_id"):
        agg["shares"] += 1
    top = k if tag == "folder" else k - 1
    for depth in range(1, min(MAX_DEPTH, top) + 1):
        u = agg["units"].setdefault("/".join(lower[:depth]), [0, 0])   # new items, of which in nested shares
        u[0] += 1
        u[1] += nested
    if tag == "file" and k - 1 < MAX_DEPTH:
        parent = "/".join(lower[:k - 1])
        agg["loose"][parent] = agg["loose"].get(parent, 0) + 1


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE)


def crawl(ns, google):
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    if state and state.get("gam") != gam_signature():
        print("The GAM exports changed since the last listing, so BigTeam gets listed again.")
        state = {}
    elif state and state.get("depth") != MAX_DEPTH:
        print(f"Listing BigTeam again to capture subfolders {MAX_DEPTH} levels deep.")
        state = {}
    if state.get("done"):
        print(f"Using the finished listing in {STATE} (delete it to list BigTeam again).")
        return state["agg"]
    if state.get("cursor"):
        agg, cursor, mode, pages = state["agg"], state["cursor"], state["mode"], state["pages"]
        print(f"Resuming the BigTeam listing ({agg['total']:,} items so far)")
        result = None
    else:
        agg, pages, cursor = new_agg(), 0, None
        result, mode = d.first_page(ns)
    resets = 0
    while True:
        if result is None:
            try:
                result = d.call("files/list_folder/continue", {"cursor": cursor}, ns, mode)
            except d.ApiError as e:
                if e.summary.startswith("reset") and resets < 3:
                    resets += 1
                    print("  Dropbox reset the listing; starting BigTeam over")
                    agg, pages = new_agg(), 0
                    result, mode = d.first_page(ns)
                else:
                    raise
        for entry in result.get("entries", []):
            tally(agg, entry, ns, google)
        pages += 1
        cursor = result.get("cursor")
        if not result.get("has_more"):
            save_state({"done": True, "agg": agg, "gam": gam_signature(), "depth": MAX_DEPTH})
            return agg
        result = None
        if pages % CHECKPOINT_EVERY == 0:
            save_state({"cursor": cursor, "mode": mode, "pages": pages, "agg": agg, "gam": gam_signature(),
                        "depth": MAX_DEPTH})
            print(f"  ... {agg['total']:,} items listed")


# ---------------------------------------------------------------- planning

def display(agg, key):
    parts = key.split("/")
    out = []
    for i, part in enumerate(parts):
        out.append(LOOSE_DIR if part == norm(LOOSE_DIR) else agg["names"].get("/".join(parts[:i + 1]), part))
    return "/".join(out)


def plan(agg, google, totals):
    children = collections.defaultdict(list)
    for key in agg["units"]:
        children[key.rsplit("/", 1)[0] if "/" in key else ""].append(key)
    free = {lab: max(0, FILL_TO - totals.get(lab, 0)) for lab in EXISTING}
    units, loose, warnings = [], [], []

    def add_loose(parent):
        n = agg["loose"].get(parent, 0)
        if n:
            loose.append(parent)
            key = (parent + "/" if parent else "") + norm(LOOSE_DIR)
            agg["names"][key] = LOOSE_DIR
            units.append({"key": key, "new": n + 1, "nested": 0, "where": None})

    def place(key, depth):
        new, nested = agg["units"][key]
        where = google.get(norm("/" + key))           # this folder already exists in Google
        room = free[where] if where else FILL_TO
        if new <= room or depth >= MAX_DEPTH or not children[key]:
            if new > room:
                warnings.append(f"{display(agg, key)}: {new:,} new items, more than one drive takes at this depth")
            if where:
                free[where] -= new
            units.append({"key": key, "new": new, "nested": nested, "where": where})
            return
        add_loose(key)                                # files sitting directly in a split folder
        for c in sorted(children[key], key=lambda c: -agg["units"][c][0]):
            place(c, depth + 1)

    add_loose("")
    for c in sorted(children[""], key=lambda c: -agg["units"][c][0]):
        place(c, 1)

    bins = []                                         # new shared drives, first-fit decreasing
    for u in sorted((u for u in units if not u["where"]), key=lambda u: -u["new"]):
        for i, b in enumerate(bins):
            if b + u["new"] <= FILL_TO:
                bins[i] += u["new"]
                u["where"] = f"new{i + 1}"
                break
        else:
            bins.append(u["new"])
            u["where"] = f"new{len(bins)}"

    runs = []                                         # import runs: rows, prefix clashes, GUser loads
    for u in sorted(units, key=lambda u: (u["where"], -u["new"])):
        src = "/" + TEAM + "/" + display(agg, u["key"])
        u["source"] = src
        target = None
        for i, r in enumerate(runs):
            if len(r["rows"]) >= MAX_ROWS:
                continue
            if any(o.startswith(src.lower()) or src.lower().startswith(o) for o in r["sources"]):
                continue                              # Google rejects rows where one path starts another
            if min(r["load"].values()) + u["new"] > GUSER_CAP:
                continue
            target = i
            break
        if target is None:
            runs.append({"rows": [], "sources": [], "load": {g: 0 for g in GUSERS}})
            target = len(runs) - 1
        r = runs[target]
        g = min(GUSERS, key=lambda x: r["load"][x])
        r["rows"].append(u)
        r["sources"].append(src.lower())
        r["load"][g] += u["new"]
        u["run"], u["guser"] = target + 1, g
    return units, loose, bins, runs, free, warnings


def cmd_plan():
    if not d.TOKEN:
        d.refresh_access_token()
    google, totals = load_google()
    ns = bigteam_ns()
    print(f"Listing {TEAM} in Dropbox (read-only)...")
    agg = crawl(ns, google)
    units, loose, bins, runs, free, warnings = plan(agg, google, totals)
    new_total = sum(u["new"] for u in units)
    nested_total = sum(u["nested"] for u in units)

    with open(PLAN_JSON, "w") as fh:
        json.dump({"units": units, "loose": loose, "drives_needed": len(bins), "names": agg["names"]}, fh)
    with open(PLAN_CSV, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Run", "Goes to", "Target GUser", "Source DropboxValue", "New items", "In nested shared folders"])
        for u in sorted(units, key=lambda u: (u["run"], u["where"], -u["new"])):
            w.writerow([u["run"], u["where"], u["guser"], u["source"], u["new"], u["nested"]])
    if not os.path.exists(DRIVES_CSV):
        with open(DRIVES_CSV, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Drive", "Folder ID or URL"])
            for i in range(len(bins)):
                w.writerow([f"new{i + 1}", ""])

    print(f"\n{TEAM}: {agg['total']:,} items in Dropbox, {agg['in_google']:,} already in Google, "
          f"{new_total:,} still to come over.")
    print(f"\nNew shared drives needed: {len(bins)} (each filled to at most {FILL_TO:,} items)")
    for i, b in enumerate(bins):
        print(f"  new{i + 1}: {b:,} items")
    for lab in EXISTING:
        used = sum(u["new"] for u in units if u["where"] == lab)
        if used:
            print(f"  {lab} (already holds part of BigTeam): +{used:,} items")
    print(f"\n{len(units)} rows across {len(runs)} import run(s):")
    for i, r in enumerate(runs, 1):
        loads = ", ".join(f"{g.split('@')[0]} {n:,}" for g, n in r["load"].items() if n)
        print(f"  run {i}: {len(r['rows'])} rows, {sum(u['new'] for u in r['rows']):,} items ({loads})")
    if loose:
        print(f"\nLoose files to tuck into a '{LOOSE_DIR}' subfolder first: "
              f"{sum(agg['loose'][p] for p in loose):,} files in {len(loose)} folder(s) -> run the loose step.")
    if nested_total:
        print(f"\nFYI: {nested_total:,} of those items sit inside {agg['shares']:,} shared subfolders. Most shared "
              "subfolders imported fine in the other team folders;\nonly some specific ones couldn't be opened, and "
              "the import report will show which.")
    for w_ in warnings:
        print(f"WARNING: {w_}")
    print(f"\nNothing was changed. Details in {PLAN_CSV}.")
    print(f"Next: create {len(bins)} shared drives, add {', '.join(GUSERS)} as Managers, make a folder in each,\n"
          f"and paste each folder's ID or URL into {DRIVES_CSV}. Then run:  python3 plan_import_split.py csv")


# ---------------------------------------------------------------- loose files / CSVs

def load_plan():
    if not os.path.exists(PLAN_JSON):
        sys.exit("Run plan first.")
    return json.load(open(PLAN_JSON))


def call_any(endpoint, payload):
    last = None
    for mode in ("user", "admin"):
        for attempt in range(6):
            try:
                return d.call(endpoint, payload, None, mode)
            except d.ApiError as e:
                last = e
                if "too_many_write_operations" in e.summary or "internal_error" in e.summary:
                    time.sleep(3 * (attempt + 1))
                    continue
                break
    raise last


def cmd_loose():
    p = load_plan()
    if not p["loose"]:
        sys.exit("No loose files to move.")
    if not d.TOKEN:
        d.refresh_access_token()
    google, _ = load_google()
    ns = bigteam_ns()
    moves = []
    for parent in p["loose"]:
        base = "/" + parent if parent else ""
        result = call_any("files/list_folder", {"path": f"ns:{ns}{base}", "recursive": False, "limit": 2000})
        entries = list(result.get("entries", []))
        while result.get("has_more"):
            result = call_any("files/list_folder/continue", {"cursor": result["cursor"]})
            entries += result.get("entries", [])
        for e in entries:
            if e.get(".tag") != "file":
                continue                              # shared subfolders can come back without a path
            shown = e.get("path_display") or f"{base}/{e['name']}"
            if norm(shown) not in google:
                moves.append((e.get("path_lower") or shown.lower(), shown.rsplit("/", 1)[0] + "/" + LOOSE_DIR + "/" + e["name"]))
    print(f"{len(moves):,} loose files to move into a '{LOOSE_DIR}' subfolder of the folder they're in.")
    if not moves:
        sys.exit("Nothing to move.")
    if input("Type MOVE to move them: ").strip() != "MOVE":
        sys.exit("Cancelled. Nothing was moved.")
    errors = 0
    with open(LOOSE_CSV, "a", newline="") as fh:
        w = csv.writer(fh)
        for i, (src, dst) in enumerate(moves, 1):
            try:
                call_any("files/move_v2", {"from_path": f"ns:{ns}{src}", "to_path": f"ns:{ns}{dst}", "autorename": False})
                res = "moved"
            except d.ApiError as e:
                res = f"ERROR: {e.summary or e.body[:150]}"
            errors += res != "moved"
            w.writerow([src, dst, res])
            fh.flush()
            if i % 100 == 0 or i == len(moves):
                print(f"  ... {i:,}/{len(moves):,} ({errors} errors)", end="\r", flush=True)
    print(f"\nDone, {errors} errors (details in {LOOSE_CSV}).")


def folder_id(raw):
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", raw or "")
    fid = (m.group(1) if m else (raw or "")).strip()
    if fid.startswith("0A"):
        sys.exit(f"{fid} is a shared drive ID. Use the ID of the folder inside the drive.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", fid):
        return None
    return fid


def cmd_csv():
    p = load_plan()
    if not os.path.exists(DRIVES_CSV):
        sys.exit(f"Missing {DRIVES_CSV}.")
    targets = dict(EXISTING)
    with open(DRIVES_CSV, newline="") as fh:
        for r in csv.DictReader(fh):
            fid = folder_id(r.get("Folder ID or URL"))
            if fid:
                targets[r["Drive"].strip()] = fid
    need = [f"new{i + 1}" for i in range(p["drives_needed"])]
    missing = [n for n in need if n not in targets]
    if missing:
        sys.exit(f"{DRIVES_CSV} still needs a folder ID for: {', '.join(missing)}")
    if len(set(targets[n] for n in need)) < len(need) or set(targets[n] for n in need) & set(EXISTING.values()):
        sys.exit(f"Each new drive in {DRIVES_CSV} needs its own folder, different from drives 1-3.")
    if p["loose"] and not os.path.exists(LOOSE_CSV):
        sys.exit("The plan has loose files to tuck away first: run  python3 plan_import_split.py loose")
    # split folders: their pieces go inside a same-named folder in each new drive
    names = p["names"]
    chains = set()
    for u in p["units"]:
        if u["where"].startswith("new") and "/" in u["key"]:
            parts = u["key"].split("/")[:-1]
            for i in range(1, len(parts) + 1):
                chains.add((u["where"], "/".join(parts[:i])))
    parent_ids = {}
    if os.path.exists(PARENTS_TSV):
        with open(PARENTS_TSV, newline="") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == 3 and parts[2].strip():
                    parent_ids[(parts[0], parts[1])] = parts[2].strip()
    todo = sorted((c for c in chains if c not in parent_ids), key=lambda c: (c[0], c[1].count("/"), c[1]))
    if todo:
        var = {c: f"id_{i}" for i, c in enumerate(todo, 1)}
        with open(MAKE_SH, "w") as fh:
            fh.write("#!/bin/bash\n# Recreates the split BigTeam folders inside the new shared drives (GAM).\n")
            fh.write('set -eo pipefail\ncd "$(dirname "$0")"\n')
            fh.write(f"mk() {{ gam user {GAM_USER} create drivefile drivefilename \"$1\" mimetype gfolder parentid \"$2\" returnidonly | tail -1; }}\n")
            fh.write('check() { [[ "$1" =~ ^[A-Za-z0-9_-]{10,}$ ]] || { echo "GAM did not return a folder ID for $2"; exit 1; }; }\n')
            for c in todo:
                drive, key = c
                up = (drive, key.rsplit("/", 1)[0]) if "/" in key else None
                if up is None:
                    parent = shlex.quote(targets[drive])
                elif up in parent_ids:
                    parent = shlex.quote(parent_ids[up])
                else:
                    parent = f'"${var[up]}"'
                fname = names.get(key, key.rsplit("/", 1)[-1])
                v = var[c]
                fh.write(f"{v}=$(mk {shlex.quote(fname)} {parent}); check \"${v}\" {shlex.quote(drive + ': ' + fname)}\n")
                fh.write(f"printf '%s\\t%s\\t%s\\n' {shlex.quote(drive)} {shlex.quote(key)} \"${v}\" >> {PARENTS_TSV}\n")
                fh.write(f"echo {shlex.quote('created in ' + drive + ': ' + fname)}\n")
        os.chmod(MAKE_SH, 0o755)
        sys.exit(f"{len(todo)} folder(s) need recreating inside the new drives first. Run:\n  bash {MAKE_SH}\n"
                 f"then run  python3 plan_import_split.py csv  again.")

    runs = collections.defaultdict(list)
    for u in p["units"]:
        runs[u["run"]].append(u)
    for run, rows in sorted(runs.items()):
        name = f"bigteam_import_{run}.csv"
        with open(name, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Source DropboxValue", "Target Drive FolderID", "Target GUser"])
            for u in sorted(rows, key=lambda u: u["source"].lower()):
                if u["where"].startswith("new") and "/" in u["key"]:
                    tgt = parent_ids[(u["where"], u["key"].rsplit("/", 1)[0])]
                else:
                    tgt = targets[u["where"]]
                w.writerow([u["source"], tgt, u["guser"]])
        print(f"{name}: {len(rows)} rows, {sum(u['new'] for u in rows):,} items")
    print("\nUpload them one run at a time as the Step 2 file (identity map unchanged), run the delta,")
    print("and let each finish before the next.")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if mode == "plan":
        cmd_plan()
    elif mode == "loose":
        cmd_loose()
    elif mode == "csv":
        cmd_csv()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nStopped. Rerun the same command; the listing resumes where it left off.")
    except d.NetworkError as e:
        sys.exit(f"\nGave up after repeated network errors ({e}). Rerun to resume.")
    except d.ApiError as e:
        sys.exit(f"\nDropbox refused a request: {e}")
