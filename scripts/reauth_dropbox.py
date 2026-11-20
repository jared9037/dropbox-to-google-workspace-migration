#!/usr/bin/env python3
"""
reauth_dropbox.py -- gets a new Dropbox refresh token (so newly added app permissions
take effect) and writes it into ~/.config/dropbox_migration/credentials.json. Never prints any credential.

  python3 reauth_dropbox.py

1. Opens Dropbox's "allow access" page in your default browser and also copies its
   link to the clipboard (paste it into Edge if that's where you're signed in as dropbox-admin).
2. Click Allow, copy the code Dropbox shows, paste it at the prompt here (it's hidden).
3. The script saves the new refresh token in the credentials file and checks that the
   new permissions work.
"""
import getpass
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import dropbox_inventory as d

url = "https://www.dropbox.com/oauth2/authorize?" + urllib.parse.urlencode(
    {"client_id": d.APP_KEY, "response_type": "code", "token_access_type": "offline"})
subprocess.run(["pbcopy"], input=url.encode(), check=False)
subprocess.run(["open", url], check=False)
print("Opened Dropbox's authorization page (link also copied to the clipboard).")
print("Sign in as dropbox-admin@example.org if asked, click Allow, then copy the code.")
code = getpass.getpass("Paste the code here (hidden) and press Enter: ").strip()
if not code:
    raise SystemExit("No code entered. Nothing changed.")

data = urllib.parse.urlencode({"code": code, "grant_type": "authorization_code",
                               "client_id": d.APP_KEY, "client_secret": d.APP_SECRET}).encode()
try:
    with urllib.request.urlopen(urllib.request.Request(d.TOKEN_URL, data=data, method="POST"), timeout=60) as r:
        resp = json.loads(r.read())
except urllib.error.HTTPError as e:
    raise SystemExit(f"Dropbox refused the code (HTTP {e.code}). Codes work once and expire fast; rerun and try again. Nothing changed.")
new_rt = resp.get("refresh_token")
if not new_rt:
    raise SystemExit("Dropbox didn't return a refresh token. Nothing changed.")

cred = json.load(open(d.CRED_FILE, encoding="utf-8"))
cred["refresh_token"] = new_rt
with open(d.CRED_FILE, "w", encoding="utf-8") as fh:
    json.dump(cred, fh)
print("New refresh token saved into", d.CRED_FILE)
print("Scopes granted include sharing.write:", "sharing.write" in (resp.get("scope") or ""))
