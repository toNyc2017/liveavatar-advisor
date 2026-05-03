"""Print streaming avatars on your HeyGen account.

LiveAvatar shares the HeyGen avatar catalog. You need an avatar_id to put in
.env (HEYGEN_AVATAR_ID). Run:
    python list_avatars.py

If your account doesn't have streaming-eligible avatars, your plan probably
doesn't include LiveAvatar. Check https://app.heygen.com.
"""

import os
import sys
import httpx
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("HEYGEN_API_KEY")
if not API_KEY:
    sys.exit("HEYGEN_API_KEY missing from .env")


def get(url: str) -> dict:
    res = httpx.get(url, headers={"X-Api-Key": API_KEY}, timeout=30)
    res.raise_for_status()
    return res.json()


def main() -> None:
    # HeyGen's streaming-avatar list endpoint. (LiveAvatar reuses it.)
    payload = get("https://api.heygen.com/v1/streaming/avatar.list")
    rows = payload.get("data") or []
    if not rows:
        print("No streaming avatars on this account. You may need to upgrade or use a public avatar.")
        return
    print(f"{'avatar_id':<40}  status      created_at")
    print("-" * 70)
    for a in rows:
        print(f"{a.get('avatar_id',''):<40}  {a.get('status',''):<10}  {a.get('created_at','')}")


if __name__ == "__main__":
    main()
