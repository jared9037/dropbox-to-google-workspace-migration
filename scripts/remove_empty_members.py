#!/usr/bin/env python3
"""
Removes Dropbox team members whose member folder is empty.

  python3 remove_empty_members.py plan     read-only: re-checks each folder live, writes empty_members_plan.csv
  python3 remove_empty_members.py remove   asks for REMOVE, re-checks each folder again, then removes
                                           the member (their remaining data, if any, is transferred to dropbox-admin). Log: empty_members_removed.csv

Only members listed in EMAILS are touched, and only if their folder is still empty
(no files, no folders, no shared folders mounted) at the moment of removal.
"""
import csv, sys
import dropbox_inventory as d

ITADMIN = "dbmid:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"

EMAILS = """user01@example.com user02@example.com user03@example.com
user04@example.com user05@example.com user06@example.com
user07@example.com user08@example.com user09@example.com
user10@example.com user11@example.com user12@example.com
user13@example.com user14@example.com user15@example.com
user16@example.com user17@example.com user18@example.com
user19@example.com user20@example.com user21@example.com
user22@example.com user23@example.com user24@example.com
user25@example.com user26@example.com user27@example.com""".split()


def members():
    r = d.call("team/members/list_v2", {"limit": 1000})
    out = list(r["members"])
    while r.get("has_more"):
        r = d.call("team/members/list/continue_v2", {"cursor": r["cursor"]})
        out += r["members"]
    return {m["profile"]["email"].lower(): m["profile"] for m in out}


def contents(ns):
    """Number of entries (files + folders) in a member folder; 0 means empty."""
    result, mode = d.first_page(ns)
    n = len(result.get("entries", []))
    while result.get("has_more") and n == 0:
        result = d.call("files/list_folder/continue", {"cursor": result["cursor"]}, ns, mode)
        n += len(result.get("entries", []))
    return n


def check(profiles):
    rows = []
    for e in EMAILS:
        p = profiles.get(e)
        if not p:
            rows.append([e, "", "", "not a member any more", "skip"]); continue
        ns = p.get("member_folder_id", "")
        status = p["status"][".tag"]
        n = contents(ns) if ns and status != "invited" else 0
        rows.append([e, status, ns, n, "remove" if n == 0 else "skip (not empty)"])
    return rows


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if not d.TOKEN:
        d.refresh_access_token()
    profiles = members()
    rows = check(profiles)
    with open("empty_members_plan.csv", "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["Member", "Status", "Member folder", "Items inside", "Action"]); w.writerows(rows)
    go = [r for r in rows if r[4] == "remove"]
    for r in rows:
        print(f"  {r[4]:18} {r[1]:10} {r[0]}  ({r[3]} items)")
    print(f"\n{len(go)} of {len(EMAILS)} would be removed. Plan in empty_members_plan.csv.")
    if cmd != "remove":
        print("Nothing was changed."); return
    if input(f"Type REMOVE to remove these {len(go)} members from the Dropbox team: ").strip() != "REMOVE":
        print("Cancelled."); return
    with open("empty_members_removed.csv", "a", newline="") as fh:
        w = csv.writer(fh)
        for r in go:
            p = profiles[r[0]]
            if p["status"][".tag"] != "invited" and contents(r[2]) != 0:
                w.writerow([r[0], "skipped: no longer empty"]); print(f"  skipped (no longer empty): {r[0]}"); continue
            try:
                body = {"user": {".tag": "team_member_id", "team_member_id": p["team_member_id"]},
                        "wipe_data": True, "keep_account": False}
                if p["status"][".tag"] != "invited":   # anything they still own (e.g. unmounted shared folders) goes to dropbox-admin
                    body["transfer_dest_id"] = {".tag": "team_member_id", "team_member_id": ITADMIN}
                    body["transfer_admin_id"] = {".tag": "team_member_id", "team_member_id": ITADMIN}
                d.call("team/members/remove", body)
                w.writerow([r[0], "removed"]); print(f"  removed: {r[0]}")
            except Exception as ex:
                w.writerow([r[0], f"error: {str(ex)[:200]}"]); print(f"  ERROR {r[0]}: {str(ex)[:200]}")
    print("Done. Log in empty_members_removed.csv")


if __name__ == "__main__":
    main()
