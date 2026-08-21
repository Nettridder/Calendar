# Spond → Google Calendar sync

Automatically mirrors events from a Spond group into a Google Calendar, on a
schedule, using GitHub Actions. Once it's set up, nobody has to add anything
by hand in either place: new Spond events appear in the Google Calendar
within ~30 minutes, edits get updated, and cancelled events get removed.

**Important honesty note before you set this up:** Spond has no official
public API. This uses a well-maintained but *unofficial*, reverse-engineered
Python client ([`spond`](https://pypi.org/project/spond/)) that logs in the
same way the mobile app does. It works reliably today, but if Spond changes
its login flow it could break until the library is updated — check
https://github.com/Olen/Spond for updates if the sync job starts failing.
For that reason it's worth using a dedicated Spond login for this (e.g. a
"calendar-bot" account added as a member/admin of the group) rather than
your own personal one, so nothing else depends on that password.

## What you'll need

- A GitHub account (free tier is enough) and a **private** repository.
- A Google Cloud project (free) to create a service account.
- A Google Calendar that your organisation already has people subscribed to
  (or a new one you create for this).
- Your Spond login (ideally a dedicated account, see above) and the ID of the
  group you want to sync.

## 1. Get your Spond group ID

Locally, with Python 3.11+ installed:

```bash
pip install spond
SPOND_USERNAME="you@example.com" SPOND_PASSWORD="yourpassword" python3 list_groups.py
```

This prints every group (and subgroup) you belong to, with its ID. Note down
the `SPOND_GROUP_ID` for the group you want synced (and `SPOND_SUBGROUP_ID`
too, only if you want just one sub-team rather than the whole group).

## 2. Create a Google service account and give it calendar access

1. Go to https://console.cloud.google.com/, create a project (or reuse one).
2. Enable the **Google Calendar API** for that project (APIs & Services →
   Enable APIs and Services → search "Google Calendar API" → Enable).
3. Create a service account: APIs & Services → Credentials → Create
   Credentials → Service account. Give it any name, e.g. `spond-calendar-sync`.
4. Open the new service account → Keys → Add key → Create new key → JSON.
   This downloads a `.json` file — keep it private, treat it like a password.
5. Note the service account's email address (looks like
   `spond-calendar-sync@your-project.iam.gserviceaccount.com`).
6. In Google Calendar, open the settings of the calendar you want events to
   land in (the shared org calendar people already subscribe to) → **Share
   with specific people** → add the service account's email → give it
   **Make changes to events** permission.
7. In the same settings page, scroll to **Integrate calendar** and copy the
   **Calendar ID** (for a dedicated calendar it looks like
   `xxxxxxxx@group.calendar.google.com`; for someone's personal calendar it's
   their email address).

## 3. Create the GitHub repo

1. Create a new **private** repository on GitHub.
2. Push everything in this folder (`sync.py`, `list_groups.py`,
   `requirements.txt`, `.github/workflows/sync.yml`, `.gitignore`) to it.
   Do **not** commit your real `.env` file or the service account JSON key —
   `.gitignore` already excludes them, but double check before pushing.

## 4. Add your secrets

In the GitHub repo: Settings → Secrets and variables → Actions → New
repository secret. Add each of these:

| Secret name | Value |
|---|---|
| `SPOND_USERNAME` | the Spond login email |
| `SPOND_PASSWORD` | the Spond login password |
| `SPOND_GROUP_ID` | the group ID from step 1 |
| `SPOND_SUBGROUP_ID` | (optional) subgroup ID from step 1 |
| `GOOGLE_CALENDAR_ID` | the calendar ID from step 2 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the **entire contents** of the downloaded JSON key file, pasted as-is |

## 5. Run it

Go to the repo's **Actions** tab, open "Sync Spond events to Google
Calendar", and click **Run workflow** to trigger it manually the first time.
Check the log output — it prints `CREATE` / `UPDATE` / `DELETE` for every
event it touches. After that first successful run, it will keep running
automatically every 30 minutes (edit the `cron` line in
`.github/workflows/sync.yml` to change that cadence).

## Testing locally before pushing (optional but recommended)

```bash
cp .env.example .env
# fill in real values in .env, leave DRY_RUN=1
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python3 sync.py
```

With `DRY_RUN=1` it prints what it *would* create/update/delete without
touching the calendar. Set `DRY_RUN=0` (or remove it) once you're happy.

## How matching works (so edits and cancellations behave correctly)

Every event this script creates is stamped with the Spond event's ID in a
private field on the Google Calendar event. Each run re-fetches the current
state of Spond and the calendar and reconciles them:

- Spond event with no matching Google event → **created**
- Spond event whose title/time/location/description changed → **updated**
- Spond event that's cancelled, or no longer returned by Spond → the
  matching Google event is **deleted**
- Everything else is left untouched

Only events inside a rolling window (1 day in the past to 120 days in the
future by default — see `LOOKBACK_DAYS` / `LOOKAHEAD_DAYS`) are reconciled,
so very old events aren't repeatedly checked.

## Troubleshooting

- **"Missing required environment variable"** — a secret name doesn't match
  what `sync.py` expects; check the table above for exact spelling.
- **403 errors from the Calendar API** — the service account email hasn't
  been shared on the calendar with "Make changes to events" permission.
- **Login/authentication errors from Spond** — the unofficial library may
  need updating (`pip install -U spond`), or Spond may have changed
  something; check https://github.com/Olen/Spond/issues.
