#!/usr/bin/env python3
"""
Read-only inventory of what's left in Dropbox outside the team folders: every member's
personal folder, and the shared folders mounted inside them.

Each file is counted once, against the namespace it actually lives in (the member's own
folder, or the shared folder it sits in). A shared folder that shows up in several members'
folders is counted the first time it's seen and credited to that member.

Usage:  python3 remaining_inventory.py      (resumable; Ctrl-C and rerun to continue)
Writes: remaining_member_folders.csv, remaining_shared_folders.csv
Changes nothing in Dropbox or Google.
"""
import csv
import json
import os
import sys

import dropbox_inventory as d

STATE = "remaining_inventory_state.json"
MEMBERS_CSV = "remaining_member_folders.csv"
SHARED_CSV = "remaining_shared_folders.csv"
CHECKPOINT_EVERY = 25


def paged(endpoint, cont, payload, key):
    r = d.call(endpoint, payload)
    out = list(r[key])
    while r.get("has_more"):
        r = d.call(cont, {"cursor": r["cursor"]})
        out += r[key]
    return out


def save_state(state):
    with open(STATE + ".tmp", "w") as fh:
        json.dump(state, fh)
    os.replace(STATE + ".tmp", STATE)


def crawl_member(ns, state):
    """Lists one member folder; returns {owner ns: [files, bytes]} and {shared ns: mount path}."""
    cur = state.get("current") or {}
    if cur.get("ns") == ns:
        counts, mounts, cursor, mode, pages = cur["counts"], cur["mounts"], cur["cursor"], cur["mode"], cur["pages"]
        print(f"  resuming ({sum(c[0] for c in counts.values()):,} files so far)")
        result = None
    else:
        counts, mounts, pages = {}, {}, 0
        result, mode = d.first_page(ns)
    while True:
        if result is None:
            result = d.call("files/list_folder/continue", {"cursor": cursor}, ns, mode)
        for e in result.get("entries", []):
            si = e.get("sharing_info") or {}
            if e.get(".tag") == "folder" and si.get("shared_folder_id"):
                mounts.setdefault(si["shared_folder_id"], e.get("path_display") or e.get("name", ""))
            elif e.get(".tag") == "file":
                c = counts.setdefault(si.get("parent_shared_folder_id") or ns, [0, 0])
                c[0] += 1
                c[1] += e.get("size", 0)
        pages += 1
        cursor = result.get("cursor")
        if not result.get("has_more"):
            return counts, mounts
        result = None
        if pages % CHECKPOINT_EVERY == 0:
            state["current"] = {"ns": ns, "counts": counts, "mounts": mounts, "cursor": cursor,
                                "mode": mode, "pages": pages}
            save_state(state)
            print(f"  ... {sum(c[0] for c in counts.values()):,} files listed")


def main():
    if not d.TOKEN:
        d.refresh_access_token()
    state = json.load(open(STATE)) if os.path.exists(STATE) else {"done": {}, "shared": {}}
    namespaces = paged("team/namespaces/list", "team/namespaces/list/continue", {"limit": 1000}, "namespaces")
    members = {m["profile"]["team_member_id"]: m["profile"]
               for m in paged("team/members/list_v2", "team/members/list/continue_v2",
                              {"limit": 1000, "include_removed": True}, "members")}
    names = {n["namespace_id"]: n["name"] for n in namespaces}
    folders = [n for n in namespaces if n["namespace_type"][".tag"] == "team_member_folder"]
    todo = [n for n in folders if n["namespace_id"] not in state["done"]]
    print(f"{len(folders)} member folders, {len(todo)} to list.\n")

    for i, n in enumerate(todo, 1):
        ns = n["namespace_id"]
        who = members.get(n.get("team_member_id"), {})
        email = who.get("email", n["name"])
        print(f"[{i}/{len(todo)}] {email}")
        try:
            counts, mounts = crawl_member(ns, state)
        except (d.ApiError, d.NetworkError) as e:
            print(f"  couldn't list this folder: {e}")
            state["done"][ns] = {"email": email, "status": who.get("status", {}).get(".tag", "?"),
                                 "error": str(e)[:200]}
            state.pop("current", None)
            save_state(state)
            continue
        own = counts.pop(ns, [0, 0])
        mine = {s: c for s, c in counts.items() if s not in state["shared"]}
        for s, c in mine.items():
            state["shared"][s] = {"files": c[0], "bytes": c[1], "member": email, "path": mounts.get(s, "")}
        for s, p in mounts.items():           # mounted but empty, or nothing inside listed
            state["shared"].setdefault(s, {"files": 0, "bytes": 0, "member": email, "path": p})
        state["done"][ns] = {"email": email, "status": who.get("status", {}).get(".tag", "?"),
                             "files": own[0], "bytes": own[1], "shares": len(mine),
                             "share_files": sum(c[0] for c in mine.values()),
                             "share_bytes": sum(c[1] for c in mine.values())}
        state.pop("current", None)
        save_state(state)
        r = state["done"][ns]
        print(f"  own: {r['files']:,} files, {r['bytes'] / 1e12:,.2f} TB; "
              f"shared folders first seen here: {r['shares']} ({r['share_files']:,} files, "
              f"{r['share_bytes'] / 1e12:,.2f} TB)")

    with open(MEMBERS_CSV, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Member", "Status", "Namespace ID", "Own files", "Own GB",
                    "Shared folders first seen here", "Shared files", "Shared GB", "Error"])
        for ns, r in sorted(state["done"].items(), key=lambda x: -(x[1].get("bytes", 0) + x[1].get("share_bytes", 0))):
            w.writerow([r["email"], r["status"], ns, r.get("files", ""), round(r.get("bytes", 0) / 1e9, 1),
                        r.get("shares", ""), r.get("share_files", ""), round(r.get("share_bytes", 0) / 1e9, 1),
                        r.get("error", "")])
    shared_ns = [n["namespace_id"] for n in namespaces if n["namespace_type"][".tag"] == "shared_folder"]
    with open(SHARED_CSV, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Shared folder", "Namespace ID", "First seen in member", "Path there", "Files", "GB"])
        for s in sorted(set(shared_ns) | set(state["shared"]), key=lambda s: -state["shared"].get(s, {}).get("bytes", -1)):
            r = state["shared"].get(s)
            if r:
                w.writerow([names.get(s, ""), s, r["member"], r["path"], r["files"], round(r["bytes"] / 1e9, 1)])
            else:
                w.writerow([names.get(s, ""), s, "(not in any member folder: inside a team folder, or unmounted)", "", "", ""])

    rows = list(state["done"].values())
    tf = sum(r.get("files", 0) for r in rows); tb = sum(r.get("bytes", 0) for r in rows)
    sf = sum(r.get("share_files", 0) for r in rows); sb = sum(r.get("share_bytes", 0) for r in rows)
    errs = sum(1 for r in rows if r.get("error"))
    print(f"\nMember folders' own files: {tf:,} files, {tb / 1e12:,.2f} TB")
    print(f"Shared folders inside them: {len(state['shared']):,} folders, {sf:,} files, {sb / 1e12:,.2f} TB")
    if errs:
        print(f"{errs} member folder(s) couldn't be listed (see Error column).")
    print(f"Details in {MEMBERS_CSV} and {SHARED_CSV}. Nothing was changed.")


if __name__ == "__main__":
    main()
