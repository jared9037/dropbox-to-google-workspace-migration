#!/usr/bin/env python3
"""
dropbox_inventory.py -- READ-ONLY inventory of every Dropbox team folder.

  1. Lists every team folder in the tenant.
  2. Crawls each ACTIVE team folder and counts files, folders, items
     (files + folders -- what Google's 500,000-item caps count) and size.
  3. For any folder over 250,000 items (BigTeam), also counts items per
     subfolder, two levels deep, so it can be split across shared drives.

Only list endpoints are called. Nothing is moved, renamed, archived or deleted.

Writes, in the folder you run it from:
  team_folder_inventory.csv   one row per team folder
  team_folder_breakdown.csv   per-subfolder counts for the big folders
  inventory_state.json        progress checkpoint (delete it once you're done)

Resumable: after any interruption, rerun the same command. Finished folders
are skipped, and a half-crawled folder picks up from its last checkpoint.

Run:  python3 dropbox_inventory.py
"""
import csv
import http.client
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Credentials live outside the code (never commit them): ~/.config/dropbox_migration/credentials.json
# = {"app_key": ..., "app_secret": ..., "refresh_token": ...}, chmod 600. reauth_dropbox.py updates refresh_token.
CRED_FILE = os.path.expanduser("~/.config/dropbox_migration/credentials.json")
with open(CRED_FILE, encoding="utf-8") as _f:
    _cred = json.load(_f)
APP_KEY, APP_SECRET, REFRESH_TOKEN = _cred["app_key"], _cred["app_secret"], _cred["refresh_token"]
ACT_AS_MEMBER = "dbmid:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"  # dropbox-admin@example.org

API = "https://api.dropboxapi.com/2"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"

SUMMARY_CSV = "team_folder_inventory.csv"
BREAKDOWN_CSV = "team_folder_breakdown.csv"
STATE_FILE = "inventory_state.json"

DRIVE_CAP = 500000           # Google: max items in one shared drive
SAFE_PER_DRIVE = 400000      # planning target that leaves headroom under the cap
BREAKDOWN_THRESHOLD = 250000
CHECKPOINT_EVERY = 25        # pages (up to 2,000 entries each)
MAX_RETRIES = 10
BIG_LAST = ("big team folder",)  # crawl the giant one last
LOOSE = "\x00loose"          # bucket key for files sitting directly in a folder
LOOSE_LABEL = "(files directly in this folder)"
MODES = (None, "user", "admin")  # ways of acting as dropbox-admin, tried in this order

TOKEN = os.environ.get("DBX_TOKEN", "").strip()
_good_mode = None

SUMMARY_FIELDS = ["Team Folder", "Team Folder ID", "Status", "Items", "Size (GB)",
                  "Files", "Folders", "Shared drives needed (400k each)", "Note"]
BREAKDOWN_FIELDS = ["Team Folder", "Team Folder ID", "Level", "Subfolder",
                    "Items", "Size (GB)", "Files", "Folders"]


class ApiError(Exception):
    def __init__(self, endpoint, status, body):
        self.status, self.body = status, body
        try:
            self.summary = json.loads(body).get("error_summary", "") or ""
        except (ValueError, AttributeError):
            self.summary = ""
        super().__init__(f"{endpoint} failed (HTTP {status}): {self.summary or body[:300]}")


class NetworkError(Exception):
    pass


# ---------------------------------------------------------------- HTTP layer

def _backoff(attempt):
    return min(60, 2 ** attempt)


def _retry_after(err, attempt):
    try:
        return max(1, min(300, int(err.headers.get("Retry-After"))))
    except (TypeError, ValueError):
        return _backoff(attempt)


def _read_body(err):
    try:
        return err.read().decode(errors="replace")
    except Exception:
        return ""


def refresh_access_token():
    global TOKEN
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": APP_KEY,
        "client_secret": APP_SECRET,
    }).encode()
    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                TOKEN = json.loads(resp.read())["access_token"]
            print("  [access token refreshed automatically]")
            return
        except urllib.error.HTTPError as e:
            sys.exit(f"\nCouldn't refresh the Dropbox token (HTTP {e.code}): {_read_body(e)[:300]}\n"
                     "Mint a fresh access token by hand and rerun with DBX_TOKEN set.")
        except (OSError, http.client.HTTPException, ValueError) as e:
            wait = _backoff(attempt)
            print(f"  [network error refreshing token: {e}; retrying in {wait}s]")
            time.sleep(wait)
    raise NetworkError("token refresh kept failing")


def call(endpoint, payload, path_root_ns=None, mode=None):
    """POST to a Dropbox API endpoint with token refresh and retries built in.
    mode: None (no member header), "user" (Select-User) or "admin" (Select-Admin)."""
    url = f"{API}/{endpoint}"
    data = json.dumps(payload).encode()
    refreshed = False
    attempt = 0
    while True:
        headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        if path_root_ns:
            headers["Dropbox-API-Path-Root"] = json.dumps(
                {".tag": "namespace_id", "namespace_id": path_root_ns})
        if mode == "user":
            headers["Dropbox-API-Select-User"] = ACT_AS_MEMBER
        elif mode == "admin":
            headers["Dropbox-API-Select-Admin"] = ACT_AS_MEMBER
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = resp.read()
            return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = _read_body(e)
            if e.code == 401 and not refreshed:
                refresh_access_token()
                refreshed = True
                continue
            if e.code == 429 or e.code >= 500:
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise NetworkError(f"{endpoint}: HTTP {e.code} kept coming back")
                wait = _retry_after(e, attempt) if e.code == 429 else _backoff(attempt)
                why = "rate limited" if e.code == 429 else f"Dropbox server error {e.code}"
                print(f"  [{why}; retrying in {wait}s]")
                time.sleep(wait)
                continue
            raise ApiError(endpoint, e.code, body)
        except (OSError, http.client.HTTPException, ValueError) as e:
            attempt += 1
            if attempt > MAX_RETRIES:
                raise NetworkError(f"{endpoint}: {e}")
            wait = _backoff(attempt)
            print(f"  [network error: {e}; retrying in {wait}s]")
            time.sleep(wait)


# ---------------------------------------------------------------- crawling

def list_team_folders():
    result = call("team/team_folder/list", {"limit": 1000})
    folders = list(result.get("team_folders", []))
    while result.get("has_more"):
        result = call("team/team_folder/list/continue", {"cursor": result["cursor"]})
        folders.extend(result.get("team_folders", []))
    return folders


def first_page(fid):
    """First page of a recursive listing, trying each way of acting as dropbox-admin."""
    global _good_mode
    payload = {"path": "", "recursive": True, "limit": 2000, "include_deleted": False}
    order = [_good_mode] + [m for m in MODES if m != _good_mode]
    last_err = None
    for mode in order:
        try:
            result = call("files/list_folder", payload, fid, mode)
            _good_mode = mode
            return result, mode
        except ApiError as e:
            if e.status in (400, 403, 409, 422):
                last_err = e
                continue
            raise
    raise last_err


def new_agg():
    return {"files": 0, "folders": 0, "bytes": 0, "l1": {}, "l2": {}, "names": {}}


def items_of(agg):
    return agg["files"] + agg["folders"]


def _bump(table, key, is_file, size):
    row = table.setdefault(key, [0, 0, 0])  # files, folders, bytes
    row[0 if is_file else 1] += 1
    row[2] += size


def tally(agg, entry):
    tag = entry.get(".tag")
    if tag not in ("file", "folder"):
        return
    is_file = tag == "file"
    size = entry.get("size", 0) if is_file else 0
    agg["files" if is_file else "folders"] += 1
    agg["bytes"] += size
    # group on path_lower (consistent casing); take display names from each
    # folder's own entry, whose last path component is always cased correctly
    lower = [p for p in (entry.get("path_lower") or "").split("/") if p]
    shown = [p for p in (entry.get("path_display") or "").split("/") if p]
    depth = len(lower)
    if depth == 0:
        return
    if depth == 1 and is_file:
        k1 = LOOSE
    else:
        k1 = lower[0]
        if depth == 1 and shown:
            agg["names"][k1] = shown[-1]
    _bump(agg["l1"], k1, is_file, size)
    if depth >= 2:
        if depth == 2 and is_file:
            k2 = lower[0] + "/" + LOOSE
        else:
            k2 = lower[0] + "/" + lower[1]
            if depth == 2 and shown:
                agg["names"][k2] = shown[-1]
        _bump(agg["l2"], k2, is_file, size)


def crawl_folder(folder, state):
    fid = folder["team_folder_id"]
    cur = state.get("current")
    if cur and cur.get("folder_id") == fid:
        agg, cursor, mode, pages = cur["agg"], cur["cursor"], cur["mode"], cur["pages"]
        print(f"  resuming from checkpoint ({items_of(agg):,} items already counted)")
        result = None
    else:
        agg, pages, cursor = new_agg(), 0, None
        result, mode = first_page(fid)
    resets = 0
    while True:
        if result is None:
            try:
                result = call("files/list_folder/continue", {"cursor": cursor}, fid, mode)
            except ApiError as e:
                if e.summary.startswith("reset") and resets < 3:
                    resets += 1
                    print("  Dropbox invalidated the listing cursor; recounting this folder from the top")
                    agg, pages = new_agg(), 0
                    result, mode = first_page(fid)
                else:
                    raise
        for entry in result.get("entries", []):
            tally(agg, entry)
        pages += 1
        cursor = result.get("cursor")
        if not result.get("has_more"):
            return agg
        result = None
        if pages % CHECKPOINT_EVERY == 0:
            state["current"] = {"folder_id": fid, "cursor": cursor, "mode": mode,
                                "pages": pages, "agg": agg}
            save_state(state)
            print(f"  ... {items_of(agg):,} items counted so far")


# ---------------------------------------------------------------- files

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_FILE)


def clear_current(state, fid=None):
    """Drop the mid-crawl checkpoint -- only if it belongs to this folder."""
    if "current" in state and (fid is None or state["current"].get("folder_id") == fid):
        state.pop("current")
        save_state(state)


def read_summary():
    """Latest row per team folder ID (a rerun can append a newer row)."""
    rows = {}
    if os.path.exists(SUMMARY_CSV):
        with open(SUMMARY_CSV, newline="") as fh:
            for r in csv.DictReader(fh):
                rows[r["Team Folder ID"]] = r
    return rows


def write_summary(folder, agg, note):
    new_file = not os.path.exists(SUMMARY_CSV)
    row = {"Team Folder": folder["name"], "Team Folder ID": folder["team_folder_id"],
           "Status": folder.get("status", {}).get(".tag", ""), "Note": note}
    if agg is not None:
        n = items_of(agg)
        row.update({"Items": n, "Size (GB)": f"{agg['bytes'] / 1e9:.2f}",
                    "Files": agg["files"], "Folders": agg["folders"],
                    "Shared drives needed (400k each)": math.ceil(n / SAFE_PER_DRIVE)})
    with open(SUMMARY_CSV, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)


def write_breakdown(folder, agg):
    fid = folder["team_folder_id"]
    keep = []
    if os.path.exists(BREAKDOWN_CSV):
        with open(BREAKDOWN_CSV, newline="") as fh:
            keep = [r for r in csv.DictReader(fh) if r["Team Folder ID"] != fid]
    names = agg["names"]

    def label(key):
        parts = key.split("/")
        out = []
        for i, part in enumerate(parts):
            out.append(LOOSE_LABEL if part == LOOSE else names.get("/".join(parts[:i + 1]), part))
        return "/".join(out)

    rows = []
    for level, table in ((1, agg["l1"]), (2, agg["l2"])):
        for key, (files, nfolders, size) in sorted(table.items(), key=lambda kv: -(kv[1][0] + kv[1][1])):
            rows.append({"Team Folder": folder["name"], "Team Folder ID": fid, "Level": level,
                         "Subfolder": label(key), "Items": files + nfolders,
                         "Size (GB)": f"{size / 1e9:.2f}", "Files": files, "Folders": nfolders})
    with open(BREAKDOWN_CSV, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=BREAKDOWN_FIELDS)
        w.writeheader()
        w.writerows(keep + rows)


def breakdown_rows(fid, level):
    if not os.path.exists(BREAKDOWN_CSV):
        return []
    with open(BREAKDOWN_CSV, newline="") as fh:
        return [r for r in csv.DictReader(fh)
                if r["Team Folder ID"] == fid and r["Level"] == str(level)]


# ---------------------------------------------------------------- main

def print_report():
    rows = list(read_summary().values())
    counted = sorted((r for r in rows if r["Items"]), key=lambda r: -int(r["Items"]))
    print("\n=== Team folders still in Dropbox (largest first) ===")
    for r in counted:
        print(f"{int(r['Items']):>11,} items  {float(r['Size (GB)']):>10,.1f} GB   {r['Team Folder']}")
    for r in rows:
        if not r["Items"]:
            print(f"{'-':>11}        {'-':>10}      {r['Team Folder']}  [{r['Note']}]")
    if not counted:
        return
    total = sum(int(r["Items"]) for r in counted)
    total_gb = sum(float(r["Size (GB)"]) for r in counted)
    print(f"\nTotal: {len(counted)} folders, {total:,} items, {total_gb / 1000:,.2f} TB")
    big = [r for r in counted if int(r["Items"]) > DRIVE_CAP]
    rest = [r for r in counted if int(r["Items"]) <= DRIVE_CAP]
    for r in big:
        n = int(r["Items"])
        print(f"\n{r['Team Folder']}: {n:,} items = {n / DRIVE_CAP:.1f}x the {DRIVE_CAP:,}-item cap of one shared drive.")
        print(f"  Needs at least {math.ceil(n / DRIVE_CAP)} shared drives; "
              f"{math.ceil(n / SAFE_PER_DRIVE)} at {SAFE_PER_DRIVE:,} each.")
        print(f"  Top-level subfolders (two-level list is in {BREAKDOWN_CSV}):")
        for b in breakdown_rows(r["Team Folder ID"], 1)[:25]:
            print(f"    {int(b['Items']):>10,} items  {float(b['Size (GB)']):>9,.1f} GB   {b['Subfolder']}")
    if rest:
        n = sum(int(r["Items"]) for r in rest)
        gb = sum(float(r["Size (GB)"]) for r in rest)
        verdict = "fits" if n <= SAFE_PER_DRIVE else "does NOT fit"
        print(f"\nEverything else: {len(rest)} folders, {n:,} items, {gb / 1000:,.2f} TB "
              f"-- {verdict} in one shared drive at {SAFE_PER_DRIVE:,} items.")


def main():
    if not TOKEN:
        refresh_access_token()
    state = load_state()
    done = {fid for fid, r in read_summary().items() if not r["Note"].startswith("error")}

    print("Listing team folders...")
    folders = list_team_folders()
    active = [f for f in folders
              if f.get("status", {}).get(".tag") == "active" and not f.get("is_team_shared_dropbox")]
    active_ids = {f["team_folder_id"] for f in active}
    others = [f for f in folders if f["team_folder_id"] not in active_ids]
    print(f"{len(folders)} team folders: {len(active)} active, {len(others)} archived/other.\n")

    for f in others:
        if f["team_folder_id"] not in done:
            what = ("team space root" if f.get("is_team_shared_dropbox")
                    else f.get("status", {}).get(".tag", "unknown status"))
            write_summary(f, None, f"{what}, not crawled")
            done.add(f["team_folder_id"])
    if state.get("current", {}).get("folder_id") in done:
        clear_current(state)

    todo = [f for f in active if f["team_folder_id"] not in done]
    todo.sort(key=lambda f: f["name"].strip().lower() in BIG_LAST)
    if done & active_ids:
        print(f"{len(done & active_ids)} already counted on an earlier run; {len(todo)} to go.\n")

    for i, f in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {f['name']}")
        started = time.time()
        try:
            agg = crawl_folder(f, state)
        except ApiError as e:
            if e.status in (400, 403, 409, 422):
                print(f"  couldn't read this folder: {e}")
                write_summary(f, None, f"error: {(e.summary or e.body)[:150]}")
                clear_current(state, f["team_folder_id"])
                continue
            raise
        if items_of(agg) >= BREAKDOWN_THRESHOLD:
            write_breakdown(f, agg)
        write_summary(f, agg, "")
        clear_current(state, f["team_folder_id"])
        print(f"  {items_of(agg):,} items, {agg['bytes'] / 1e9:,.1f} GB "
              f"({(time.time() - started) / 60:.1f} min)")

    print_report()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nStopped. Progress is saved -- rerun the same command to resume.")
    except NetworkError as e:
        sys.exit(f"\nGave up after repeated network errors ({e}).\n"
                 "Progress is saved -- rerun the same command to resume.")
    except ApiError as e:
        sys.exit(f"\nDropbox refused a request: {e}")
