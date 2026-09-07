"""One-time Roborock authentication.

Run this once, interactively, to log in with your Roborock account and cache the
credentials to disk. The MCP server then loads that cache and never needs the
interactive email-code step again (the cached user_data holds long-lived tokens).

Usage:
    export ROBOROCK_USERNAME="you@example.com"   # optional; will prompt if unset
    python authenticate.py

Writes: roborock_user_data.json  (override path with ROBOROCK_USER_DATA)

Keep that file private: it grants control of your vacuum.
"""

import asyncio
import json
import os
from pathlib import Path

from roborock.web_api import RoborockApiClient


async def main() -> None:
    email = os.environ.get("ROBOROCK_USERNAME") or input("Roborock account email: ").strip()
    out = Path(os.environ.get("ROBOROCK_USER_DATA", "roborock_user_data.json"))

    web_api = RoborockApiClient(username=email)

    print(f"Requesting a login code for {email} ...")
    await web_api.request_code()
    code = input("Enter the 6-digit code from your email: ").strip()

    user_data = await web_api.code_login(code)
    out.write_text(json.dumps(user_data.as_dict(), indent=2))
    print(f"\nSaved credentials to: {out.resolve()}")

    # Sanity check: list the vacuums on the account so you can pick one later.
    home = await web_api.get_home_data_v2(user_data)
    print("\nDevices on this account:")
    for d in home.devices + home.received_devices:
        print(f"  - name={d.name!r}  duid={d.duid}")
    print("\nSet ROBOROCK_DEVICE to a name or duid if you have more than one vacuum.")


if __name__ == "__main__":
    asyncio.run(main())
