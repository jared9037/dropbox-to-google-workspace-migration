#!/usr/bin/env python3
"""
find_drive_duplicates.py -- READ-ONLY. Finds files that exist more than once across the 17
"Migration Drive " shared drives because of repeated imports.

A duplicate = same content (md5 + size) AND same file name AND same path inside the import
(after dropping the drive name, the top "Dropbox"/"dropbox" folder and "BigTeam Group Team
Folder", which the different runs placed differently). The copy created FIRST is kept;
every later copy is listed for the trash.

  python3 find_drive_duplicates.py tree      # top folders per drive (sanity check)
  python3 find_drive_duplicates.py plan      # writes the plan, changes nothing

Reads exports_nov10/driveN_files.csv (GAM, with md5checksum + createdtime).
Outputs: dup_files.csv (every extra copy + the copy that stays),
         dup_trash_targets.csv (the fewest items to trash: whole folders when everything
         under them is an extra copy, otherwise single files),
         dup_summary.txt.
"""
import collections
import csv
import sys
import unicodedata

csv.field_size_limit(10 ** 9)
DIR = "."
DRIVES = range(1, 18)
STRIP = {"dropbox", "big team folder"}
FOLDER = "application/vnd.google-apps.folder"


def norm(s):
    return unicodedata.normalize("NFC", s or "").strip().lower()


def load():
    items = []
    for n in DRIVES:
        try:
            fh = open(f"{DIR}/drive{n}_files.csv", newline="", encoding="utf-8")
        except FileNotFoundError:
            continue
        for r in csv.DictReader(fh):
            p = r.get("path.0") or ""
            parts = p.split("/")[2:]            # drop "SharedDrives/<drive name>"
            items.append({"drive": n, "id": r["id"], "name": r["name"], "mime": r["mimeType"],
                          "size": r.get("size") or "", "md5": r.get("md5Checksum") or "",
                          "created": r.get("createdTime") or "", "parts": parts, "path": p})
    return items


def rel(parts):
    i = 0
    while i < len(parts) - 1 and norm(parts[i]) in STRIP:
        i += 1
    return "/".join(norm(x) for x in parts[i:])


def tree(items):
    c = collections.Counter()
    for it in items:
        if it["mime"] != FOLDER:
            c[(it["drive"], "/".join(it["parts"][:3]))] += 1
    for (d, top), k in sorted(c.items()):
        if k >= 50:
            print(f"Transfer{d:<3} {k:>8,}  {top}")


def plan(items):
    groups = collections.defaultdict(list)
    for it in items:
        if it["mime"] == FOLDER or not it["md5"]:
            continue
        groups[(rel(it["parts"]), it["size"], it["md5"])].append(it)
    extra, keep_of = [], {}
    for key, g in groups.items():
        if len(g) < 2:
            continue
        g.sort(key=lambda x: (x["created"], x["drive"]))
        for x in g[1:]:
            extra.append(x)
            keep_of[x["id"]] = g[0]
    extra_ids = {x["id"] for x in extra}

    # folders whose whole subtree is extra copies (or empty folders) -> trash the folder once
    by_drive_path = collections.defaultdict(lambda: {"files": 0, "extra": 0})
    folder_id = {}
    for it in items:
        anc = it["parts"][:-1]
        for i in range(1, len(anc) + 1):
            k = (it["drive"], "/".join(anc[:i]))
            if it["mime"] != FOLDER:
                by_drive_path[k]["files"] += 1
                by_drive_path[k]["extra"] += it["id"] in extra_ids
        if it["mime"] == FOLDER:
            folder_id[(it["drive"], "/".join(it["parts"]))] = it["id"]
            by_drive_path[(it["drive"], "/".join(it["parts"]))]
    whole = {k for k, v in by_drive_path.items() if v["files"] and v["files"] == v["extra"] and k in folder_id}
    targets = []
    for k in sorted(whole, key=lambda k: k[1].count("/")):
        d, p = k
        segs = p.split("/")
        if any((d, "/".join(segs[:i])) in whole for i in range(1, len(segs))):
            continue
        if len(segs) <= 1:          # never the drive's top "dropbox" folder itself
            continue
        targets.append({"Drive": f"Transfer{d}", "Type": "folder", "ID": folder_id[k],
                        "Path": p, "Files": by_drive_path[k]["files"]})
    covered = {(t["Drive"], t["Path"]) for t in targets}
    for x in extra:
        segs = x["parts"]
        if any((f"Transfer{x['drive']}", "/".join(segs[:i])) in covered for i in range(1, len(segs))):
            continue
        targets.append({"Drive": f"Transfer{x['drive']}", "Type": "file", "ID": x["id"],
                        "Path": "/".join(x["parts"]), "Files": 1})

    with open("dup_files.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Drive", "ID", "Path", "Size", "Created", "Kept drive", "Kept ID", "Kept path", "Kept created"])
        for x in extra:
            k = keep_of[x["id"]]
            w.writerow([f"Transfer{x['drive']}", x["id"], "/".join(x["parts"]), x["size"], x["created"],
                        f"Transfer{k['drive']}", k["id"], "/".join(k["parts"]), k["created"]])
    with open("dup_trash_targets.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["Drive", "Type", "ID", "Path", "Files"])
        w.writeheader()
        w.writerows(targets)

    out = []
    tb = sum(int(x["size"] or 0) for x in extra) / 1e12
    out.append(f"Extra copies: {len(extra):,} files, {tb:.2f} TB (one copy of each is kept).")
    out.append(f"Trash targets: {sum(t['Type']=='folder' for t in targets):,} whole folders + "
               f"{sum(t['Type']=='file' for t in targets):,} single files.")
    out.append("\nExtra copies by drive (where they'd be trashed from) -> where the kept copy is:")
    c = collections.Counter((f"Transfer{x['drive']}", f"Transfer{keep_of[x['id']]['drive']}") for x in extra)
    s = collections.Counter()
    for x in extra:
        s[(f"Transfer{x['drive']}", f"Transfer{keep_of[x['id']]['drive']}")] += int(x["size"] or 0)
    for (a, b), k in c.most_common():
        out.append(f"  {a:<12} {k:>9,} files {s[(a, b)]/1e12:6.2f} TB   kept in {b}")
    out.append("\nBiggest places the extra copies sit:")
    t = collections.Counter((f"Transfer{x['drive']}", "/".join(x["parts"][:3])) for x in extra)
    for (a, p), k in t.most_common(25):
        out.append(f"  {a:<12} {k:>9,}  {p}")
    text = "\n".join(out)
    open("dup_summary.txt", "w").write(text + "\n")
    print(text)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("tree", "plan"):
        sys.exit(__doc__)
    data = load()
    print(f"Loaded {len(data):,} items.")
    tree(data) if mode == "tree" else plan(data)
