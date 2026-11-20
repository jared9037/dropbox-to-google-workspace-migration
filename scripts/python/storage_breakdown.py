#!/usr/bin/env python3
"""
Crawls every Dropbox namespace this team token can see (team folders, members'
personal root folders, and standalone shared folders) and sums actual file
bytes in each -- there's no "get folder size" endpoint, so this walks every
file. Writes namespace_sizes.csv with one row per namespace, and prints a
running total by namespace type as it goes so you can see it's making
progress on a data set this size.

Usage:
    export DBX_TOKEN="sl.u.AG..."
    python3 storage_breakdown.py
"""
import csv
import json
import os
import sys
import time
import urllib.request
import urllib.error

TOKEN = os.environ.get("DBX_TOKEN")
if not TOKEN:
    sys.exit("Set DBX_TOKEN env var to a fresh team-linked access token first.")

# Acting-as identity for reading namespaces the team token alone can't reach
# (personal folders, ad-hoc shared folders). This is dropbox-admin@example.org's
# team_member_id, pulled from the earlier members/list export.
ACT_AS_MEMBER = "dbmid:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"

API = "https://api.dropboxapi.com/2"


def call(url, payload, path_root_ns=None, select_user=False, retries=5):
    data = json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    if path_root_ns:
        headers["Dropbox-API-Path-Root"] = json.dumps(
            {".tag": "namespace_id", "namespace_id": path_root_ns}
        )
    if select_user:
        headers["Dropbox-API-Select-User"] = ACT_AS_MEMBER
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429 and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"{url} failed ({e.code}): {body}")
    raise RuntimeError(f"{url} failed after {retries} retries")


def list_all_namespaces():
    namespaces = []
    cursor = None
    while True:
        url = f"{API}/team/namespaces/list/continue" if cursor else f"{API}/team/namespaces/list"
        payload = {"cursor": cursor} if cursor else {}
        result = call(url, payload)
        namespaces.extend(result.get("namespaces", []))
        if not result.get("has_more"):
            break
        cursor = result["cursor"]
    return namespaces


def sum_namespace_bytes(namespace_id):
    """Recursively sums file sizes within one namespace. Tries plain team auth
    first (works for formal Team Folders); falls back to acting as a team
    member (needed for personal folders and ad-hoc shared folders). Returns
    (total_bytes, file_count, note)."""
    total = 0
    count = 0
    url = f"{API}/files/list_folder"
    payload = {"path": "", "recursive": True, "limit": 2000}

    def do_list(select_user):
        return call(url, payload, path_root_ns=namespace_id, select_user=select_user)

    select_user = False
    try:
        result = do_list(select_user=False)
    except RuntimeError as e:
        if "Dropbox-API-Select-User" in str(e):
            select_user = True
            result = do_list(select_user=True)
        else:
            raise

    while True:
        for entry in result.get("entries", []):
            if entry.get(".tag") == "file":
                total += entry.get("size", 0)
                count += 1
        if not result.get("has_more"):
            break
        cont_url = f"{API}/files/list_folder/continue"
        result = call(cont_url, {"cursor": result["cursor"]}, path_root_ns=namespace_id, select_user=select_user)
    return total, count


def human(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def tally_existing():
    """Per-type byte totals already recorded in namespace_sizes.csv from a prior run."""
    totals = {}
    if os.path.exists("namespace_sizes.csv"):
        with open("namespace_sizes.csv", newline="") as f:
            for row in csv.DictReader(f):
                totals[row["Namespace Type"]] = totals.get(row["Namespace Type"], 0) + int(row["Bytes"])
    return totals


def already_done():
    """Namespace IDs already recorded in namespace_sizes.csv from a prior run,
    so re-running after a Ctrl+C picks up where it left off instead of
    reprocessing everything."""
    done = set()
    if os.path.exists("namespace_sizes.csv"):
        with open("namespace_sizes.csv", newline="") as f:
            for row in csv.DictReader(f):
                done.add(row["Namespace ID"])
    return done


def main():
    print("Listing all namespaces...")
    namespaces = list_all_namespaces()
    skip_ids = already_done()
    remaining = [ns for ns in namespaces if ns["namespace_id"] not in skip_ids]
    print(f"Found {len(namespaces)} namespaces total.")
    if skip_ids:
        print(f"{len(skip_ids)} already done from a prior run -- resuming with {len(remaining)} left.")
    print("This will take a while -- writing to namespace_sizes.csv as it goes, safe to Ctrl+C and re-run to resume.\n")

    totals_by_type = {}
    for ns_type, total in tally_existing().items():
        totals_by_type[ns_type] = total
    out_f = open("namespace_sizes.csv", "a", newline="")
    writer = csv.writer(out_f)
    if not skip_ids:
        writer.writerow(["Name", "Namespace Type", "Namespace ID", "Bytes", "File Count", "Status"])

    for i, ns in enumerate(remaining, 1):
        name, ns_id, ns_type = ns["name"], ns["namespace_id"], ns["namespace_type"][".tag"]
        try:
            size, count = sum_namespace_bytes(ns_id)
            status = "ok"
        except Exception as e:
            size, count = 0, 0
            msg = str(e)
            status = f"ERROR: {msg[:120]}"  # keep console/CSV readable; full text not needed
        totals_by_type[ns_type] = totals_by_type.get(ns_type, 0) + size
        writer.writerow([name, ns_type, ns_id, size, count, status])
        out_f.flush()
        tag = "" if status == "ok" else "  <-- no access, skipped"
        print(f"[{i}/{len(remaining)}] ({ns_type}) {name}: {human(size)} in {count} files{tag}")

    out_f.close()

    print("\n=== Totals by namespace type (including any prior run) ===")
    for ns_type, total in sorted(totals_by_type.items(), key=lambda x: -x[1]):
        print(f"  {ns_type}: {human(total)}")
    print(f"\nGrand total: {human(sum(totals_by_type.values()))}")
    print("Full detail in namespace_sizes.csv")


if __name__ == "__main__":
    main()
