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

## 2. Create a Google service account and give it calendar access (keyless)

Newer Google Cloud organizations block downloading service-account key
files by default (a security feature called "Secure by Default"). So
instead of a key file, this uses **Workload Identity Federation (WIF)**:
GitHub Actions proves its identity to Google directly via a short-lived
token on every run, and no long-lived secret ever exists to leak.

1. Go to https://console.cloud.google.com/, create a project (or reuse one;
   it's fine to leave "Organization" as **No organization**).
2. Enable the **Google Calendar API** for that project (APIs & Services →
   Enable APIs and Services → search "Google Calendar API" → Enable).
3. Create a service account: APIs & Services → Credentials → Create
   Credentials → Service account. Give it any name, e.g. `spond-calendar-sync`.
   Note its email address (looks like
   `spond-calendar-sync@your-project.iam.gserviceaccount.com`) — you will
   **not** create a key for it.
4. Decide how the service account will get write access to the calendar —
   see **2a** vs **2b** below before continuing, since this determines what
   you do next. If the calendar lives inside a Google Workspace
   organization (a calendar owned by an `@yourdomain` account, like this
   one), use **2a**. If it's a calendar on a plain personal Gmail account,
   use **2b**.
5. In the calendar's settings page (Settings → your calendar → scroll to
   **Integrate calendar**), copy the
   **Calendar ID** (for a dedicated calendar it looks like
   `xxxxxxxx@group.calendar.google.com`; for someone's personal calendar it's
   their email address).

### 2a. Workspace calendar → domain-wide delegation (recommended for this case)

Google Workspace organizations have a separate, org-wide cap on what
*external* accounts (like this service account) are allowed to do with
calendars owned inside the organization — and on many Workspace domains,
especially newly created ones, that cap silently overrides any per-calendar
"Make changes to events" permission you try to grant, even after raising
the org's external sharing setting. Domain-wide delegation avoids the whole
problem: instead of the service account acting as an *outside collaborator*
on the calendar, it acts *as* an existing member of your organization who
already owns/can edit the calendar (e.g. `nettridder@armeriddere.no`) — so
none of the external-sharing rules apply.

1. In the Cloud Console, open your service account (IAM & Admin → Service
   Accounts → click it) → **Details** tab → copy its **Unique ID** (a long
   number). This is the "Client ID" Workspace needs.
2. In Cloud Shell, grant the service account permission to sign tokens as
   itself (needed for delegation to work without a key file):

   ```bash
   gcloud iam service-accounts add-iam-policy-binding \
     spond-calendar-sync@${PROJECT_ID}.iam.gserviceaccount.com \
     --project="$PROJECT_ID" \
     --role="roles/iam.serviceAccountTokenCreator" \
     --member="serviceAccount:spond-calendar-sync@${PROJECT_ID}.iam.gserviceaccount.com"
   ```

3. In the Workspace **Admin console** (admin.google.com, as a super admin):
   Security → Access and data control → API controls → **Domain-wide
   delegation** → **Add new**. Paste the Unique ID from step 1 as the
   Client ID, and for OAuth scopes enter exactly:

   ```
   https://www.googleapis.com/auth/calendar.events
   ```

   Click Authorize.

   Be aware this scope lets the service account manage *events* (not
   calendar settings/sharing/deletion of the calendar itself) on behalf of
   **any** user in your Workspace, not just one calendar's owner — that's
   how domain-wide delegation works. Keeping the scope to `calendar.events`
   rather than the broader `calendar` scope limits what it can actually do.
4. Add a `GOOGLE_DELEGATED_USER` secret in step 4 below set to the email of
   the Workspace user who owns (or can already edit) the target calendar —
   e.g. `nettridder@armeriddere.no`. You do **not** need to share the
   calendar with the service account at all with this approach; if you'd
   already added it under "Share with specific people" while troubleshooting,
   feel free to remove that entry.

### 2b. Personal/non-Workspace calendar → direct sharing

If the calendar isn't inside a Google Workspace organization, domain-wide
delegation isn't available (it's a Workspace-only feature) — share the
calendar directly instead:

In Google Calendar, open the settings of the calendar you want events to
land in → **Share with specific people** → add the service account's email
→ set its permission to **Make changes to events**. Leave the
`GOOGLE_DELEGATED_USER` secret in step 4 unset; `sync.py` falls back to
this method automatically when that secret isn't present.

6. Open **Cloud Shell** in the Google Cloud Console (the `>_` icon top
   right) — no local install needed — and run the following, replacing the
   placeholders (`PROJECT_ID` is shown at the top of the console;
   `GITHUB_USER` and `REPO_NAME` are your GitHub username/org and the repo
   name you'll create in step 3 below):

   ```bash
   PROJECT_ID="your-project-id"
   PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
   SERVICE_ACCOUNT="spond-calendar-sync@${PROJECT_ID}.iam.gserviceaccount.com"
   GITHUB_USER="your-github-username-or-org"
   REPO_NAME="your-repo-name"

   gcloud services enable iamcredentials.googleapis.com --project="$PROJECT_ID"

   gcloud iam workload-identity-pools create "github" \
     --project="$PROJECT_ID" --location="global" \
     --display-name="GitHub Actions Pool"

   gcloud iam workload-identity-pools providers create-oidc "github-repo" \
     --project="$PROJECT_ID" --location="global" \
     --workload-identity-pool="github" \
     --display-name="GitHub repo provider" \
     --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
     --attribute-condition="assertion.repository == '${GITHUB_USER}/${REPO_NAME}'" \
     --issuer-uri="https://token.actions.githubusercontent.com"

   gcloud iam service-accounts add-iam-policy-binding "$SERVICE_ACCOUNT" \
     --project="$PROJECT_ID" \
     --role="roles/iam.workloadIdentityUser" \
     --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${GITHUB_USER}/${REPO_NAME}"

   gcloud iam workload-identity-pools providers describe "github-repo" \
     --project="$PROJECT_ID" --location="global" \
     --workload-identity-pool="github" --format="value(name)"
   ```

   The last command prints the **Workload Identity Provider** resource name
   (something like
   `projects/123456789/locations/global/workloadIdentityPools/github/providers/github-repo`)
   — copy it, you'll need it for a secret below.

   The `--attribute-condition` locks this down so only workflows running in
   *your* specific GitHub repo can use this identity — nobody else can
   impersonate the service account even if they knew its name.

## 3. Create the GitHub repo

1. Create a new **private** repository on GitHub, named to match `REPO_NAME`
   above (e.g. `spond-gcal-sync`), owned by `GITHUB_USER` above.
2. Push everything in this folder (`sync.py`, `list_groups.py`,
   `requirements.txt`, `.github/workflows/sync.yml`, `.gitignore`) to it.
   Do **not** commit your real `.env` file — `.gitignore` already excludes
   it, but double check before pushing.

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
| `GCP_PROJECT_ID` | your Google Cloud project ID |
| `GCP_SERVICE_ACCOUNT_EMAIL` | the service account's email from step 2 |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | the provider resource name printed at the end of step 2's Cloud Shell commands |
| `GOOGLE_DELEGATED_USER` | only if you did **2a**: the Workspace user email to impersonate (e.g. `nettridder@armeriddere.no`). Leave unset for **2b**. |

No key file or password for Google goes anywhere here — that's the point of
Workload Identity Federation.

## 5. Run it

Go to the repo's **Actions** tab, open "Sync Spond events to Google
Calendar", and click **Run workflow** to trigger it manually the first time.
Check the log output — it prints `CREATE` / `UPDATE` / `DELETE` for every
event it touches. After that first successful run, it will keep running
automatically every 30 minutes (edit the `cron` line in
`.github/workflows/sync.yml` to change that cadence).

## Testing locally before pushing (optional but recommended)

Because there's no key file, local testing borrows your own `gcloud` login
to temporarily act as the service account:

```bash
gcloud auth application-default login \
  --impersonate-service-account=spond-calendar-sync@your-project-id.iam.gserviceaccount.com

gcloud iam service-accounts add-iam-policy-binding \
  spond-calendar-sync@your-project-id.iam.gserviceaccount.com \
  --member="user:you@example.com" \
  --role="roles/iam.serviceAccountTokenCreator"

cp .env.example .env
# fill in real values in .env, leave DRY_RUN=1
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python3 sync.py
```

(The `add-iam-policy-binding` command only needs to be run once — it's what
lets your own account temporarily borrow the service account's identity.)

If you're using domain-wide delegation (**2a**), also set
`GOOGLE_DELEGATED_USER` and `GOOGLE_SERVICE_ACCOUNT_EMAIL` in your `.env`
before running — the impersonation used above for local testing is a
different mechanism from domain-wide delegation, but calling the
`signJwt` API still works fine through it as long as your own account also
has `roles/iam.serviceAccountTokenCreator` on the service account (the
command above already grants that).

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
- **403 "You need to have writer access to this calendar" (route 2b)** — the
  service account email hasn't been shared on the calendar with "Make
  changes to events" permission, or (very common on Workspace-owned
  calendars) your organization's external sharing cap silently overrides
  that permission even after you set it. If the calendar is inside a
  Workspace org, switch to route **2a** (domain-wide delegation) instead of
  continuing to fight that setting.
- **403 errors with domain-wide delegation (route 2a)** — double check: the
  scope authorized in the Admin console is exactly
  `https://www.googleapis.com/auth/calendar.events` (typos aren't caught
  until run time), the service account has `roles/iam.serviceAccountTokenCreator`
  on itself, and `GOOGLE_DELEGATED_USER` is a real user in your Workspace
  who actually has edit access to that calendar. Admin console delegation
  changes can take a few minutes to propagate.
- **`google.auth.exceptions.DefaultCredentialsError` in GitHub Actions** —
  the `google-github-actions/auth` step didn't run before `sync.py`, or one
  of `GCP_PROJECT_ID` / `GCP_SERVICE_ACCOUNT_EMAIL` / `GCP_WORKLOAD_IDENTITY_PROVIDER`
  is missing or misspelled, or the job is missing the
  `permissions: id-token: write` line.
- **`unauthorized_client` / `invalid_target` errors from the auth step** —
  the `--attribute-condition` in step 2 doesn't match your actual GitHub
  `owner/repo`; double-check `GITHUB_USER`/`REPO_NAME` against the repo you
  created in step 3.
- **Login/authentication errors from Spond** — the unofficial library may
  need updating (`pip install -U spond`), or Spond may have changed
  something; check https://github.com/Olen/Spond/issues.
