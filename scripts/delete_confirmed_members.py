#!/usr/bin/env python3
"""
delete_confirmed_members.py -- the member-folder version of delete_confirmed.py.
Deletes from a member's Dropbox folder only the files confirmed to be in Google,
where the Dropbox import put them: <that person's My Drive>/Root/...

  1. python3 delete_confirmed_members.py exports --csv import_batch1_members_plus_bigteam.csv
     writes member_exports.sh (one GAM export per person into member_exports/); run it:
        PATH="$HOME/bin/gam7:$PATH" bash member_exports.sh
  2. python3 delete_confirmed_members.py plan   --csv import_batch1_members_plus_bigteam.csv
  3. python3 delete_confirmed_members.py delete --csv import_batch1_members_plus_bigteam.csv

  --csv FILE        use the personal (email-only) rows of an import CSV
  --only EMAIL      just one person (can be repeated)
  --include-shared  also delete confirmed files inside shared folders mounted in the
                    person's folder. Off by default: deleting a file in a Dropbox shared
                    folder removes it for everyone it's shared with.

Same matching rule as delete_confirmed.py: same path under Root, same size, Google copy not
older. Whole folders go in one call when every file in them is confirmed (never a folder
that is or contains a shared folder). Delete re-lists each person right before deleting.
Acts in Dropbox as that member (their own folder), so nothing else is reachable.
Outputs: member_delete_plan.csv, member_kept_in_dropbox.csv, member_delete_results.csv.
"""
import collections
import csv
import os
import re
import sys
import time

import dropbox_inventory as d
from delete_confirmed import verdict, ancestors, is_shared, slim, norm, ts, FOLDER_MIME, BATCH

EXPORT_DIR = "member_exports"
EXPORT_SH = "member_exports.sh"
PLAN_CSV = "member_delete_plan.csv"
KEEP_CSV = "member_kept_in_dropbox.csv"
RESULTS_CSV = "member_delete_results.csv"
ROOT_PREFIX = "my drive/root/"
# Dropbox member -> Google account whose My Drive got the import, when it isn't the same email.
GOOGLE_USER = {"dropbox-admin@example.org": "migration-admin@example.com"}

csv.field_size_limit(10 ** 9)


def google_user(email):
    return GOOGLE_USER.get(email.lower(), email.lower())


def export_path(email):
    return os.path.join(EXPORT_DIR, google_user(email) + ".csv")


# ---------------------------------------------------------------- who

def pick_members(args):
    emails = []
    if "--csv" in args:
        f = args[args.index("--csv") + 1]
        with open(f, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                v = (r.get("Source DropboxValue") or "").strip()
                if v and not v.startswith("/") and "@" in v:
                    emails.append(v.lower())
    for i, a in enumerate(args):
        if a == "--only" and i + 1 < len(args):
            emails.append(args[i + 1].lower())
    if not emails:
        sys.exit("Say who: --csv <import csv> and/or --only <email>.")
    shared_g = collections.Counter(google_user(e) for e in emails)
    clash = [e for e in emails if shared_g[google_user(e)] > 1]
    if clash:
        sys.exit("These would share one Google My Drive/Root, so paths can't be told apart: " + ", ".join(clash))
    return list(dict.fromkeys(emails))


def dropbox_members():
    r = d.call("team/members/list_v2", {"limit": 1000})
    out = list(r["members"])
    while r.get("has_more"):
        r = d.call("team/members/list/continue_v2", {"cursor": r["cursor"]})
        out += r["members"]
    return {m["profile"]["email"].lower(): m["profile"] for m in out}


# ---------------------------------------------------------------- Google side

def load_google(email):
    """norm(path inside Root) -> [(size, modified, 'My Drive')] from the person's GAM export."""
    path = export_path(email)
    if not os.path.exists(path):
        sys.exit(f"Missing {path}. Run member_exports.sh first.")
    index = collections.defaultdict(list)
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        cols = {c.lower(): c for c in reader.fieldnames or []}
        pathcols = [c for c in reader.fieldnames or []
                    if re.fullmatch(r"(full)?paths?(\.\d+)?", c.lower()) and c.lower() != "paths"]
        if "size" not in cols or not pathcols:
            return index                       # nothing with a size in there, so no files
        for row in reader:
            if row[cols["mimetype"]] == FOLDER_MIME or not row[cols["size"]]:
                continue
            for pc in pathcols:
                p = row.get(pc) or ""
                if norm(p).startswith(ROOT_PREFIX):
                    rel = "/" + p[len(ROOT_PREFIX):]
                    index[norm(rel)].append((int(row[cols["size"]]), ts(row[cols["modifiedtime"]]), "My Drive"))
                    break
    return index


# ---------------------------------------------------------------- Dropbox side (acting as the member)

def as_member(profile):
    d.ACT_AS_MEMBER = profile["team_member_id"]
    return profile["member_folder_id"]


def list_member(ns):
    payload = {"path": "", "recursive": True, "limit": 2000, "include_deleted": False}
    r = d.call("files/list_folder", payload, ns, "user")
    entries = [slim(e) for e in r.get("entries", [])]
    while r.get("has_more"):
        r = d.call("files/list_folder/continue", {"cursor": r["cursor"]}, ns, "user")
        entries += [slim(e) for e in r.get("entries", [])]
    return entries


def plan_member(ns, index, include_shared):
    entries = list_member(ns)
    files = [e for e in entries if e.get(".tag") == "file"]

    def in_shared(e):
        pid = (e.get("sharing_info") or {}).get("parent_shared_folder_id")
        return bool(pid) and pid != ns

    keep, ok = [], set()
    shared_ok = 0
    for e in files:
        why, extra = verdict(e, index.get(norm(e.get("path_display") or e["path_lower"])))
        if not why and in_shared(e) and not include_shared:
            shared_ok += 1
            why, extra = "in a shared folder (confirmed; needs --include-shared)", ""
        if why:
            keep.append((e, why, extra))
        else:
            ok.add(e["path_lower"])
    folder_at = {e["path_lower"] for e in entries if e.get(".tag") == "folder"}
    shared_below = set()
    for e in entries:
        if is_shared(e):
            shared_below.update(ancestors(e["path_lower"]))
            shared_below.add(e["path_lower"])
    tot, good, size_under = collections.Counter(), collections.Counter(), collections.Counter()
    for e in files:
        for a in ancestors(e["path_lower"]):
            tot[a] += 1
            size_under[a] += e.get("size", 0)
            good[a] += e["path_lower"] in ok
    whole = sorted(a for a in tot if tot[a] == good[a] and a in folder_at and a not in shared_below)
    targets, covered = [], set()
    for a in whole:
        if any(x in covered for x in ancestors(a)):
            continue
        covered.add(a)
        targets.append(("folder", a, tot[a], size_under[a]))
    for e in files:
        pl = e["path_lower"]
        if pl in ok and not any(x in covered for x in ancestors(pl)):
            targets.append(("file", pl, 1, e.get("size", 0)))
    return targets, keep, len(files), shared_ok


def delete_paths(ns, paths):
    """delete_batch as the member inside their own folder; returns {path: result}."""
    results, pending = {}, list(paths)
    for attempt in range(4):
        if not pending:
            break
        try:
            launch = d.call("files/delete_batch", {"entries": [{"path": p} for p in pending]}, ns, "user")
            status = launch
            while status.get(".tag") in ("async_job_id", "in_progress"):
                time.sleep(2)
                status = d.call("files/delete_batch/check", {"async_job_id": launch["async_job_id"]}, ns, "user")
            if status.get(".tag") != "complete":
                raise RuntimeError(f"batch ended as {status.get('.tag')}")
        except (d.ApiError, RuntimeError) as e:
            print(f"\n  [Dropbox couldn't finish a batch ({getattr(e, 'summary', '') or e}); retrying]")
            time.sleep(5 * (attempt + 1))
            continue
        retry = []
        for p, r in zip(pending, status.get("entries", [])):
            if r.get(".tag") == "success":
                results[p] = "deleted"
            elif "not_found" in str(r.get("failure", r)):
                results[p] = "already gone"
            else:
                results[p] = f"ERROR: {str(r.get('failure', r))[:200]}"
                retry.append(p)
        pending = retry
    for p in pending:
        results.setdefault(p, "ERROR: gave up after retries")
    return results


# ---------------------------------------------------------------- commands

def write_exports(emails):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    with open(EXPORT_SH, "w") as fh:
        fh.write("#!/bin/bash\n# Read-only GAM exports of each person's My Drive (files they own).\nset -e\n")
        fh.write(f'mkdir -p "$HOME/Downloads/{EXPORT_DIR}"\n')
        for e in emails:
            g = google_user(e)
            fh.write(f'echo "{g}"\n')
            fh.write(f'gam redirect csv "$HOME/Downloads/{export_path(e)}" user {g} print filelist '
                     f"query \"'me' in owners\" fields id,name,mimetype,size,modifiedtime fullpath excludetrashed\n")
    print(f"Wrote {EXPORT_SH} ({len(emails)} people). Run it with:")
    print(f'  PATH="$HOME/bin/gam7:$PATH" bash {EXPORT_SH}')


def run(do_delete, emails, include_shared):
    members = dropbox_members()
    missing = [e for e in emails if e not in members]
    if missing:
        print("Not Dropbox members (skipped): " + ", ".join(missing))
    emails = [e for e in emails if e in members]
    indexes = {e: load_google(e) for e in emails}       # fail early if an export is missing

    if do_delete:
        if not os.path.exists(PLAN_CSV):
            sys.exit("Run plan first so you can see what will go.")
        with open(PLAN_CSV, newline="") as fh:
            prev = [r for r in csv.DictReader(fh) if r["Member"] in emails]
        print(f"\nThe plan has {sum(int(r['Files']) for r in prev):,} confirmed files "
              f"({sum(float(r['Size (GB)']) for r in prev):,.1f} GB) for {len({r['Member'] for r in prev})} people.")
        if input("Type DELETE to delete them from Dropbox (each person is re-checked first): ").strip() != "DELETE":
            sys.exit("Cancelled. Nothing was deleted.")
        new = not os.path.exists(RESULTS_CSV)
        log_fh = open(RESULTS_CSV, "a", newline="")
        log = csv.DictWriter(log_fh, fieldnames=["Member", "Type", "Path", "Files", "Result"])
        if new:
            log.writeheader()

    plan_rows, keep_rows, summary = [], [], []
    for i, email in enumerate(emails, 1):
        prof = members[email]
        status = prof.get("status", {}).get(".tag")
        print(f"[{i}/{len(emails)}] {email} ({status}; Google: {google_user(email)})")
        if status != "active":
            print("  not active in Dropbox, so it can't be acted as; skipped")
            continue
        ns = as_member(prof)
        try:
            targets, keep, total, shared_ok = plan_member(ns, indexes[email], include_shared)
        except d.ApiError as e:
            print(f"  couldn't list it: {e}")
            continue
        n_ok = sum(t[2] for t in targets)
        gb = sum(t[3] for t in targets) / 1e9
        summary.append((email, total, n_ok, len(keep), shared_ok))
        extra = f"; {shared_ok:,} more confirmed in shared folders (held back)" if shared_ok else ""
        print(f"  {total:,} files: {n_ok:,} confirmed in Google ({gb:,.1f} GB), {len(keep):,} stay{extra}")
        for kind, pl, n, size in targets:
            plan_rows.append({"Member": email, "Type": kind, "Path": pl, "Files": n, "Size (GB)": f"{size / 1e9:.3f}"})
        for e, why, gm in keep:
            keep_rows.append({"Member": email, "Path": e.get("path_display") or e["path_lower"],
                              "Size (MB)": f"{e.get('size', 0) / 1e6:.2f}", "Reason": why,
                              "Dropbox client_modified": e.get("client_modified", ""),
                              "Dropbox server_modified": e.get("server_modified", ""),
                              "Google modified": gm.isoformat() if gm else ""})
        if do_delete and targets:
            errors = gone = 0
            for start in range(0, len(targets), BATCH):
                chunk = targets[start:start + BATCH]
                res = delete_paths(ns, [t[1] for t in chunk])
                for kind, pl, n, _ in chunk:
                    r = res.get(pl, "ERROR: no result")
                    errors += r.startswith("ERROR")
                    gone += r == "already gone"
                    log.writerow({"Member": email, "Type": kind, "Path": pl, "Files": n, "Result": r})
                log_fh.flush()
                done = min(start + BATCH, len(targets))
                print(f"  ... {done:,}/{len(targets):,} deletes sent ({errors:,} errors)", end="\r", flush=True)
                if gone > 20 and gone > done * 0.05:
                    log_fh.close()
                    sys.exit(f"\nStopped: {gone:,} of {done:,} paths came back 'not found', which shouldn't "
                             f"happen right after listing them. Check {RESULTS_CSV}.")
            print(f"\n  deleted ({errors:,} errors)")
    if do_delete:
        log_fh.close()
    else:
        with open(PLAN_CSV, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["Member", "Type", "Path", "Files", "Size (GB)"])
            w.writeheader()
            w.writerows(plan_rows)
    with open(KEEP_CSV, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["Member", "Path", "Size (MB)", "Reason", "Dropbox client_modified",
                                           "Dropbox server_modified", "Google modified"])
        w.writeheader()
        w.writerows(keep_rows)

    tot = sum(s[1] for s in summary)
    ok = sum(s[2] for s in summary)
    gb = sum(float(r["Size (GB)"]) for r in plan_rows)
    print(f"\n{'Deleted' if do_delete else 'Would delete'} {ok:,} of {tot:,} files ({gb:,.1f} GB); {tot - ok:,} stay in Dropbox.")
    for why, n in collections.Counter(r["Reason"] for r in keep_rows).most_common():
        print(f"  {n:>9,} {why}")
    if not do_delete:
        print(f"\nNothing was changed. Plan in {PLAN_CSV}; what would stay is in {KEEP_CSV}.")


def main():
    args = sys.argv[1:]
    mode = args[0] if args else ""
    if mode not in ("exports", "plan", "delete"):
        sys.exit(__doc__)
    emails = pick_members(args)
    if mode == "exports":
        return write_exports(emails)
    if not d.TOKEN:
        d.refresh_access_token()
    run(mode == "delete", emails, "--include-shared" in args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nStopped. Rerun the same command; it re-checks everything.")
    except d.NetworkError as e:
        sys.exit(f"\nGave up after repeated network errors ({e}). Rerun to continue.")
    except d.ApiError as e:
        sys.exit(f"\nDropbox refused a request: {e}")
