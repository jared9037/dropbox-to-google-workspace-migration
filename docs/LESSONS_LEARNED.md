# Lessons learned

The problems that came up, what caused them, and how each was solved.

## Google's Data import

| Problem | Cause | What I did |
|---|---|---|
| Runs "completed" but ~142k items were never discovered | The import's crawler doesn't see inside **nested shared folders**, and skips files **last modified by external accounts** (83% of leftovers) | Proved both with targeted 1-row test runs and an API count of "last modified by", then moved the leftovers with rclone |
| Every row failed with misleading codes (13 / 9) | Destination shared drive was full | Check drive item counts before every run |
| Target folder ID silently did nothing | A shared drive ID (`0A…`) was used instead of a folder ID (`1…`) | Always point rows at a folder inside the drive |
| ~1/3 of CSV rows randomly rejected with "Internal error" | Google-side flakiness (same row succeeds on another upload) | Upload in passes; run the rows that went in, retry the rest |
| Re-pointing a row at a new drive **moved** ~157k already-imported files there | The import tracks items by source ID and relocates them | Documented as a hard rule: never re-point an imported folder |
| Uploads all failed one evening | Two admin console tabs open on the same import job | Keep exactly one Data import tab open |

## rclone at scale

| Problem | Cause | What I did |
|---|---|---|
| 15 accounts sat at 0 B for hours | `--no-traverse` makes each account look up every file individually → Dropbox "Too many requests, retry in 300 s" | Dropped `--no-traverse` (rclone lists folders and streams copies) and staggered starts 10 min apart |
| Accounts "stopped for the day" after ~10 GB | `--drive-stop-on-upload-limit` treats Google's short "User rate limit exceeded" as the 750 GB/day cap; it came from setting folder modtimes | Added `--no-update-dir-modtime`, and retry exit 7 twice after 15 min before believing it |
| 757 files would have landed in look-alike folders (`FOOTAGE` next to `Footage`) | Dropbox only guarantees casing of the last path part; Drive is case-sensitive | Planner writes paths with Google's exact folder casing |
| …which then made 258 files "nothing to transfer" | `--files-from` matching is case-sensitive against Dropbox's listing | Retried those by exact path with `rclone copyto` (Dropbox path lookups are case-insensitive) |
| Retry stopped after 26 files | macOS `xargs -I` gives up on arguments over 255 bytes | `xargs -0 -n 1` instead |
| Parallel accounts creating duplicate folders | Two accounts creating the same missing folder at once | Pre-create all missing folders from one account first; give twin-folder paths to a single account |
| Jump box went offline overnight, twice | Sleep / ISP outages | Power settings, re-runnable jobs (`--ignore-existing`), and a Mac runner as a fallback; never run both at once |
| `rclone config show <remote>` exits 0 for a missing remote | rclone v1.76 behavior | Check `rclone listremotes` instead |

## Dropbox

| Problem | Cause | What I did |
|---|---|---|
| Moving files out of nested shared folders failed (`insufficient_quota`) | The team was far over its storage quota; the move counts as new storage | Abandoned server-side moves; rclone copy instead |
| Nested shared folders can't be unshared | Dropbox design: a shared folder inside a team folder can't be unshared | Same: copy instead of restructuring |
| Some files can't be migrated at all | Paper docs and `.web` link shortcuts aren't real files | Listed and left for manual export |
| Adding an API scope didn't take effect | Saved scope changes don't apply to an existing refresh token | `reauth_dropbox.py` re-authorizes and stores the new token without printing it |
| Admin dashboard still showed TBs after deletes | Storage breakdown refreshes slowly | Trust the live API listing, re-check the next day |

## Process

- **Write things down as you go.** A running handoff file (what ran, what it found, what's next) is what made it
  possible to pause for days, switch machines, and pick up without mistakes.
- **Prove a theory with the smallest test.** One-folder test runs settled questions that full runs couldn't.
- **Measure from the destination.** Counting files in Google by createdTime showed real progress (and an
  unnoticed overlap between two runners) better than any runner's own logs.
