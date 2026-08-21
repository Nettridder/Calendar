#!/usr/bin/env python3
"""
Sync Spond group events into a Google Calendar.

Reads every event Spond returns for a group (optionally a single subgroup)
within a rolling time window, and mirrors it into a Google Calendar:

  * New Spond events  -> created in Google Calendar
  * Changed Spond events (time/title/description/location moved) -> updated
  * Cancelled / deleted Spond events -> removed from Google Calendar
  * Spond events untouched since last run -> left alone

Matching between the two systems is done with a Google Calendar
"private extended property" (spondEventId) stamped onto every event this
script creates, so no separate database/state file is needed - each run
reconciles itself against whatever is currently on the calendar.

Required environment variables:
  SPOND_USERNAME       Spond login email
  SPOND_PASSWORD       Spond login password
  GOOGLE_CALENDAR_ID   Target Google Calendar ID (e.g. xxxx@group.calendar.google.com)

Google authentication is picked up automatically via Application Default
Credentials (ADC) - no key file needed. In GitHub Actions this is set up by
the google-github-actions/auth step (Workload Identity Federation) before
this script runs. For local testing, run
`gcloud auth application-default login --impersonate-service-account=<sa-email>`
first (see README).

If GOOGLE_DELEGATED_USER is set, the script instead uses domain-wide
delegation: it signs a short-lived JWT as the service account (via the IAM
Credentials API - still no key file) asserting it wants to act as that
Workspace user, and exchanges it for an access token. This is the more
reliable option for a calendar owned inside a Google Workspace organization,
since it bypasses per-calendar "external sharing" restrictions entirely
(the service account acts *as* an existing member of your organization,
rather than as an outside collaborator). See README for the one-time setup
in the Workspace Admin console.

Optional environment variables:
  SPOND_GROUP_ID          Restrict to one Spond group (recommended; see list_groups.py)
  SPOND_SUBGROUP_ID       Restrict further to one subgroup within that group
  GOOGLE_DELEGATED_USER   A Workspace user email to impersonate via domain-wide
                          delegation (e.g. the calendar's owner). If unset,
                          falls back to plain ADC / calendar-sharing access.
  GOOGLE_SERVICE_ACCOUNT_EMAIL  Required if GOOGLE_DELEGATED_USER is set -
                          the service account's own email address.
  LOOKBACK_DAYS           How many days into the past to reconcile (default 1)
  LOOKAHEAD_DAYS          How many days into the future to reconcile (default 120)
  EVENT_TIMEZONE          IANA timezone for events without one baked in (default Europe/Oslo)
  MAX_EVENTS              Max events to fetch from Spond per run (default 250)
  DRY_RUN                 If set to "1", print planned changes without writing to Google Calendar
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import google.auth
import google.auth.transport.requests
import google.oauth2.credentials
import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from spond import spond

PRIVATE_KEY = "spondEventId"
SCOPES = ["https://www.googleapis.com/auth/calendar"]
DWD_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return val


def build_location_string(location: dict | None) -> str:
    if not location:
        return ""
    parts = [location.get("feature"), location.get("address")]
    return ", ".join(p for p in parts if p)


def spond_event_to_google_body(event: dict, tz_name: str) -> dict:
    """Translate one Spond event dict into a Google Calendar event body."""
    heading = event.get("heading") or "(untitled Spond event)"
    description = event.get("description") or ""
    footer = "\n\n— synced automatically from Spond, do not edit here —"
    body = {
        "summary": heading,
        "description": (description + footer).strip(),
        "start": {"dateTime": event["startTimestamp"], "timeZone": tz_name},
        "end": {"dateTime": event["endTimestamp"], "timeZone": tz_name},
        "extendedProperties": {"private": {PRIVATE_KEY: event["id"]}},
    }
    loc = build_location_string(event.get("location"))
    if loc:
        body["location"] = loc
    return body


def is_cancelled(event: dict) -> bool:
    # Spond marks a cancelled event with a top-level "cancelled" flag.
    return bool(event.get("cancelled"))


async def fetch_spond_events(
    username: str,
    password: str,
    group_id: str | None,
    subgroup_id: str | None,
    min_start: datetime,
    max_start: datetime,
    max_events: int,
) -> list[dict]:
    client = spond.Spond(username=username, password=password)
    try:
        events = await client.get_events(
            group_id=group_id,
            subgroup_id=subgroup_id,
            include_scheduled=True,
            include_hidden=True,
            min_start=min_start,
            max_start=max_start,
            max_events=max_events,
        )
    finally:
        await client.clientsession.close()
    return events or []


def list_existing_synced_events(service, calendar_id: str, time_min: str, time_max: str) -> dict:
    """Return {spondEventId: googleEvent} for every event this script previously created."""
    existing = {}
    page_token = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                showDeleted=False,
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        for gevent in resp.get("items", []):
            spond_id = (
                gevent.get("extendedProperties", {})
                .get("private", {})
                .get(PRIVATE_KEY)
            )
            if spond_id:
                existing[spond_id] = gevent
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return existing


def bodies_differ(existing: dict, desired: dict) -> bool:
    for key in ("summary", "description", "location"):
        if existing.get(key, "") != desired.get(key, ""):
            return True
    for part in ("start", "end"):
        if existing.get(part, {}).get("dateTime") != desired.get(part, {}).get("dateTime"):
            return True
    return False


def get_domain_wide_delegated_credentials(
    service_account_email: str, delegated_user: str
) -> google.oauth2.credentials.Credentials:
    """
    Obtain an access token that acts as `delegated_user`, using domain-wide
    delegation, without ever touching a service account key file.

    This signs a JWT bearer assertion via the IAM Credentials API's signJwt
    method (which uses Google's own copy of the key, not a downloaded one),
    then exchanges that assertion for an OAuth access token. Requires:
      - domain-wide delegation authorized for this service account's Client ID
        in the Workspace Admin console, for the calendar.events scope
      - the service account granted "Service Account Token Creator" on itself
    """
    base_credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    base_credentials.refresh(google.auth.transport.requests.Request())

    now = int(time.time())
    claims = {
        "iss": service_account_email,
        "sub": delegated_user,
        "scope": DWD_SCOPE,
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600,
    }

    sign_jwt_url = (
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        f"{service_account_email}:signJwt"
    )
    import json as _json

    sign_resp = requests.post(
        sign_jwt_url,
        headers={"Authorization": f"Bearer {base_credentials.token}"},
        json={"payload": _json.dumps(claims)},
        timeout=30,
    )
    if not sign_resp.ok:
        raise RuntimeError(
            "Failed to sign domain-wide delegation JWT "
            f"({sign_resp.status_code}): {sign_resp.text}"
        )
    signed_jwt = sign_resp.json()["signedJwt"]

    token_resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": signed_jwt,
        },
        timeout=30,
    )
    if not token_resp.ok:
        raise RuntimeError(
            "Failed to exchange domain-wide delegation JWT for an access "
            f"token ({token_resp.status_code}): {token_resp.text}"
        )
    access_token = token_resp.json()["access_token"]
    return google.oauth2.credentials.Credentials(token=access_token)


def main() -> None:
    spond_username = env("SPOND_USERNAME", required=True)
    spond_password = env("SPOND_PASSWORD", required=True)
    group_id = env("SPOND_GROUP_ID")
    subgroup_id = env("SPOND_SUBGROUP_ID")
    calendar_id = env("GOOGLE_CALENDAR_ID", required=True)
    delegated_user = env("GOOGLE_DELEGATED_USER")
    service_account_email = env("GOOGLE_SERVICE_ACCOUNT_EMAIL")
    tz_name = env("EVENT_TIMEZONE", "Europe/Oslo")
    lookback_days = int(env("LOOKBACK_DAYS", "1"))
    lookahead_days = int(env("LOOKAHEAD_DAYS", "120"))
    max_events = int(env("MAX_EVENTS", "250"))
    dry_run = env("DRY_RUN", "0") == "1"

    now = datetime.now(timezone.utc)
    min_start = now - timedelta(days=lookback_days)
    max_start = now + timedelta(days=lookahead_days)

    print(
        f"Fetching Spond events between {min_start.isoformat()} and "
        f"{max_start.isoformat()} ..."
    )
    spond_events = asyncio.run(
        fetch_spond_events(
            spond_username,
            spond_password,
            group_id,
            subgroup_id,
            min_start,
            max_start,
            max_events,
        )
    )
    print(f"Spond returned {len(spond_events)} event(s).")

    active_spond_events = {e["id"]: e for e in spond_events if not is_cancelled(e)}

    if delegated_user:
        if not service_account_email:
            print(
                "GOOGLE_DELEGATED_USER is set but GOOGLE_SERVICE_ACCOUNT_EMAIL "
                "is missing.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Using domain-wide delegation, acting as {delegated_user} ...")
        credentials = get_domain_wide_delegated_credentials(
            service_account_email, delegated_user
        )
    else:
        # Plain Application Default Credentials: picks up the Workload
        # Identity Federation credentials the auth step exports in CI, or
        # whatever `gcloud auth application-default login` set up locally.
        # Requires the calendar to be directly shared with the service
        # account with "Make changes to events" permission.
        credentials, _ = google.auth.default(scopes=SCOPES)

    service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

    existing = list_existing_synced_events(
        service,
        calendar_id,
        time_min=min_start.isoformat(),
        time_max=max_start.isoformat(),
    )
    print(f"Google Calendar currently has {len(existing)} previously-synced event(s) in this window.")

    created = updated = deleted = unchanged = 0

    for spond_id, sevent in active_spond_events.items():
        desired_body = spond_event_to_google_body(sevent, tz_name)
        if spond_id in existing:
            gevent = existing[spond_id]
            if bodies_differ(gevent, desired_body):
                print(f"UPDATE  {sevent.get('heading')!r} ({spond_id})")
                if not dry_run:
                    try:
                        service.events().patch(
                            calendarId=calendar_id,
                            eventId=gevent["id"],
                            body=desired_body,
                        ).execute()
                    except HttpError as exc:
                        print(f"  failed to update: {exc}", file=sys.stderr)
                        continue
                updated += 1
            else:
                unchanged += 1
        else:
            print(f"CREATE  {sevent.get('heading')!r} ({spond_id})")
            if not dry_run:
                try:
                    service.events().insert(
                        calendarId=calendar_id, body=desired_body
                    ).execute()
                except HttpError as exc:
                    print(f"  failed to create: {exc}", file=sys.stderr)
                    continue
            created += 1

    for spond_id, gevent in existing.items():
        if spond_id not in active_spond_events:
            print(f"DELETE  {gevent.get('summary')!r} ({spond_id})")
            if not dry_run:
                try:
                    service.events().delete(
                        calendarId=calendar_id, eventId=gevent["id"]
                    ).execute()
                except HttpError as exc:
                    print(f"  failed to delete: {exc}", file=sys.stderr)
                    continue
            deleted += 1

    print(
        f"Done. created={created} updated={updated} deleted={deleted} "
        f"unchanged={unchanged}{' (dry run, nothing was written)' if dry_run else ''}"
    )


if __name__ == "__main__":
    main()
