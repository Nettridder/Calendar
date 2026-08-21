#!/usr/bin/env python3
"""
One-off helper: log in to Spond and print your groups (and their subgroups),
each with its ID, so you can copy the right SPOND_GROUP_ID / SPOND_SUBGROUP_ID
into your GitHub secrets.

Run locally (not on GitHub Actions):

    SPOND_USERNAME="you@example.com" SPOND_PASSWORD="yourpassword" python3 list_groups.py

Nothing is stored or sent anywhere except to Spond's own login API.
"""

from __future__ import annotations

import asyncio
import os
import sys

from spond import spond


async def main() -> None:
    username = os.environ.get("SPOND_USERNAME")
    password = os.environ.get("SPOND_PASSWORD")
    if not username or not password:
        print("Set SPOND_USERNAME and SPOND_PASSWORD environment variables first.", file=sys.stderr)
        sys.exit(1)

    client = spond.Spond(username=username, password=password)
    try:
        groups = await client.get_groups()
    finally:
        await client.clientsession.close()

    if not groups:
        print("No groups found for this account.")
        return

    for group in groups:
        print(f"Group: {group.get('name')}")
        print(f"  SPOND_GROUP_ID = {group.get('id')}")
        for sub in group.get("subGroups", []) or []:
            print(f"  Subgroup: {sub.get('name')}")
            print(f"    SPOND_SUBGROUP_ID = {sub.get('id')}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
