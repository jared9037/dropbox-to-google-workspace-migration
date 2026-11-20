#!/usr/bin/env python3
"""
plan_rclone_jobs.py -- READ-ONLY planner. Builds the rclone job package for the Windows jump box.

For every file still left in the team folders (kept_in_dropbox_*_nov10.csv, minus SUBFOLDER
A which rclone already copied Nov 12) it finds the deepest folder of that path that
already exists in Google (Nov 10 GAM exports, Transfer1-17) and sends the file to that drive's
import root, so rclone fills the gaps inside the folders that already exist.

Missing folders are listed separately (mkdirs.csv) and created first in one pass, so no two
accounts ever create the same folder; then single files are spread over the 15 copyworker
accounts by size.

Writes jumpbox/: jobs.csv, lists/*.txt (rclone --files-from-raw, paths relative to the team
folder), drives_needed.csv (which account needs which shared drive), run_all.ps1, check_all.ps1.
"""
import collections
import csv
import os
import pickle
import unicodedata

csv.field_size_limit(10 ** 9)
OUT = "jumpbox"
ACCOUNTS = ["copyworker@example.com"] + [f"copyworker{i}@example.com" for i in range(2, 16)]
RO = {4, 5, 6, 7, 8, 9}
DRIVE_ID = {1: "0AEXAMPLEDRIVE04", 2: "0AEXAMPLEDRIVE05", 3: "0AEXAMPLEDRIVE06",
            4: "0AEXAMPLEDRIVE07", 5: "0AEXAMPLEDRIVE08", 6: "0AEXAMPLEDRIVE09",
            7: "0AEXAMPLEDRIVE10", 8: "0AEXAMPLEDRIVE11", 9: "0AEXAMPLEDRIVE12",
            10: "0AEXAMPLEDRIVE13", 11: "0AEXAMPLEDRIVE14", 12: "0AEXAMPLEDRIVE15",
            13: "0AEXAMPLEDRIVE16", 14: "0AEXAMPLEDRIVE17", 15: "0AEXAMPLEDRIVE18",
            16: "0AEXAMPLEDRIVE19", 17: "0AEXAMPLEDRIVE20"}


def n(s):
    return unicodedata.normalize("NFC", s or "").strip().lower()


def main():
    kept = []
    for f in ("kept_in_dropbox_bigteam_nov10.csv", "kept_in_dropbox_others_nov10.csv"):
        kept += list(csv.DictReader(open(f, newline="", encoding="utf-8")))
    kept = [k for k in kept if not k["Path"].startswith("/SUBFOLDER A/")]
    teams = {n(k["Team Folder"]) for k in kept} | {"big team folder"}
    display_team = {n(k["Team Folder"]): k["Team Folder"] for k in kept}

    # (team, rel folder path) -> {drive: folder id}
    idx = collections.defaultdict(dict)
    gnm = {}   # (team, rel, drive) -> exact Google folder name (Drive is case-sensitive)
    dupc = collections.Counter()   # (team, rel, drive) -> how many Google folders share this path
    for d in range(1, 18):
        for r in csv.DictReader(open(f"drive{d}_files.csv", newline="", encoding="utf-8")):
            if not r["mimeType"].endswith("folder"):
                continue
            parts = r["path.0"].split("/")[2:]
            i = 1 if parts and n(parts[0]) == "dropbox" else 0
            if d in RO:
                team, rel = "big team folder", parts[i:]
            else:
                if i >= len(parts) or n(parts[i]) not in teams:
                    continue
                team, rel = n(parts[i]), parts[i + 1:]
            key = (team, "/".join(n(x) for x in rel))
            dupc[key + (d,)] += 1
            if d not in idx[key]:
                idx[key][d] = r["id"]
                if rel:
                    gnm[key + (d,)] = rel[-1]

    placed = []
    for k in kept:
        team = n(k["Team Folder"])
        raw = k["Path"].strip("/").split("/")
        segs = [n(x) for x in raw[:-1]]
        for j in range(len(segs), -1, -1):
            if (team, "/".join(segs[:j])) in idx:
                placed.append((k, team, raw, j, set(idx[(team, "/".join(segs[:j]))])))
                break
    majority = collections.defaultdict(collections.Counter)
    for k, team, raw, j, ds in placed:
        if len(ds) == 1:
            majority[team][next(iter(ds))] += 1
    units = collections.defaultdict(lambda: {"files": [], "mb": 0.0})
    missing = collections.defaultdict(set)
    for k, team, raw, j, ds in placed:
        d = next(iter(ds)) if len(ds) == 1 else max(ds, key=lambda x: majority[team][x])
        # walk again inside the chosen drive only
        segs = [n(x) for x in raw[:-1]]
        while j > 0 and d not in idx.get((team, "/".join(segs[:j])), {}):
            j -= 1
        # use Google's exact names for folders that already exist (Dropbox parent casing is unreliable)
        raw = [gnm[(team, "/".join(segs[:m]), d)] for m in range(1, j + 1)] + raw[j:]
        rel_path = "/".join(raw)
        # under a folder that exists twice in Google? -> one account per drive handles these, and
        # its rclone creates the missing folders itself (consistent twin choice, no races)
        dup = any(dupc[(team, "/".join(segs[:m]), d)] > 1 for m in range(1, j + 1))
        if not dup:
            for m in range(j + 1, len(raw)):      # folders that don't exist in Google yet
                missing[(team, d)].add("/".join(raw[:m]))
        ukey = (team, d, "DUP") if dup else (team, d, rel_path)   # DUP = one unit per team+drive
        units[ukey]["files"].append(rel_path)
        units[ukey]["mb"] += float(k["Size (MB)"] or 0)

    # spread units over accounts, biggest first
    load = {a: 0.0 for a in ACCOUNTS}
    assign = collections.defaultdict(list)   # (account, team, drive) -> files
    size = collections.Counter()
    dup_acct = {}   # drive -> the one account doing all its duplicated-folder files
    for ukey, u in sorted(units.items(), key=lambda x: (x[0][2] != "DUP", -x[1]["mb"])):
        if ukey[2] == "DUP":
            a = dup_acct.setdefault(ukey[1], min(load, key=load.get))
        else:
            a = min(load, key=load.get)
        load[a] += u["mb"]
        team, d, _ = ukey
        assign[(a, team, d)] += u["files"]
        size[(a, team, d)] += u["mb"]

    os.makedirs(f"{OUT}/lists", exist_ok=True)
    jobs = []
    for i, ((a, team, d), files) in enumerate(sorted(assign.items(), key=lambda x: (ACCOUNTS.index(x[0][0]), x[0][2], x[0][1]))):
        name = f"{a.split('@')[0]}_T{d}_{i:03d}.txt"
        with open(f"{OUT}/lists/{name}", "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(sorted(files)) + "\n")
        jobs.append({"Account": a, "Team Folder": display_team.get(team, "Big Team Folder"),
                     "Drive": f"Transfer{d}", "Shared Drive ID": DRIVE_ID[d], "Root Folder ID": idx[(team, "")][d],
                     "List": name, "Files": len(files), "GB": round(size[(a, team, d)] / 1000, 1)})
    with open(f"{OUT}/jobs.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(jobs[0]))
        w.writeheader()
        w.writerows(jobs)
    mk = []
    for (team, d), dirs in sorted(missing.items(), key=lambda x: (x[0][1], x[0][0])):
        name = f"mkdirs_T{d}_{len(mk):02d}.txt"
        with open(f"{OUT}/lists/{name}", "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(sorted(dirs, key=lambda x: (x.count("/"), x))) + "\n")
        mk.append({"Team Folder": display_team.get(team, "Big Team Folder"), "Drive": f"Transfer{d}",
                   "Shared Drive ID": DRIVE_ID[d], "Root Folder ID": idx[(team, "")][d], "List": name, "Folders": len(dirs)})
    with open(f"{OUT}/mkdirs.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(mk[0]))
        w.writeheader()
        w.writerows(mk)
    print(f"{sum(m['Folders'] for m in mk):,} folders to create first, in {len(mk)} lists")
    need = sorted({(j["Account"], j["Drive"], j["Shared Drive ID"]) for j in jobs}, key=lambda x: (ACCOUNTS.index(x[0]), x[1]))
    with open(f"{OUT}/drives_needed.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Account", "Drive", "Shared Drive ID"])
        w.writerows(need)

    print(f"{sum(j['Files'] for j in jobs):,} files, {sum(j['GB'] for j in jobs)/1000:.2f} TB, {len(jobs)} jobs, "
          f"{len(units):,} units, {len(need)} account/drive memberships needed")
    for a in ACCOUNTS:
        js = [j for j in jobs if j["Account"] == a]
        print(f"  {a:<30} {sum(j['Files'] for j in js):>7,} files {sum(j['GB'] for j in js):>8,.1f} GB  drives "
              + ",".join(sorted({j['Drive'].replace('Transfer', 'T') for j in js})))


if __name__ == "__main__":
    main()
