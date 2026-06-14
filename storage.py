"""Per-visitor file storage — local filesystem in dev, S3 in production.

The avatar's per-visitor memory subsystem (see MEMORY_SUBSYSTEM.md) stores
three kinds of files under `users/<user_id>/`:

  USER.md          ← durable profile (hand-curatable name + notes)
  MEMORY.md        ← LLM-compiled wiki of all past sessions
  memory/<ts>.md   ← raw per-session digests, one per conversation

In local dev these live on disk and `pathlib.Path` does the work. In
production on App Runner there's no persistent local disk — these need
to live in S3 instead.

This module hides the difference behind a small `UserStorage` interface
so the four call sites in `advisor_backend.py` don't need to care which
backend they're talking to. Backend is picked at process start from
env vars:

  USERS_STORAGE_BACKEND    "local" (default) or "s3"
  USERS_LOCAL_DIR          path to root for local backend
                           (default: <repo>/users)
  USERS_S3_BUCKET          required when backend == "s3"
  USERS_S3_PREFIX          key prefix in the bucket (default: "users")
  AWS_REGION               passed to boto3 (default: "us-east-1")

A LocalUserStorage instance is the default — `./run.sh` keeps working
without setting any new env vars.
"""

from __future__ import annotations

import io
import os
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

log = logging.getLogger("advisor.storage")


class UserStorage(Protocol):
    """Per-visitor file storage. See module docstring for layout."""

    def user_dir_exists(self, user_id: str) -> bool: ...
    def profile_exists(self, user_id: str) -> bool: ...

    # USER.md — durable profile
    def read_profile(self, user_id: str) -> str | None: ...
    def write_profile(self, user_id: str, body: str) -> None: ...

    # MEMORY.md — LLM-compiled wiki
    def read_compiled_memory(self, user_id: str) -> str | None: ...
    def write_compiled_memory(self, user_id: str, body: str) -> None: ...
    def compiled_memory_mtime(self, user_id: str) -> float | None: ...

    # memory/<ts>.md — raw per-session digests
    def list_session_digests(self, user_id: str) -> list[str]:
        """Return sorted list of digest filenames (oldest first)."""
        ...
    def read_session_digest(self, user_id: str, filename: str) -> str: ...
    def write_session_digest(self, user_id: str, filename: str, body: str) -> None: ...
    def session_digest_mtime(self, user_id: str, filename: str) -> float | None: ...

    # For the CLI compiler — enumerate every user folder
    def list_user_ids(self) -> list[str]: ...


# ---------------------------------------------------------------------------
# Local filesystem implementation
# ---------------------------------------------------------------------------
class LocalUserStorage:
    """Default backend — `pathlib.Path` everywhere, same shape as before."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _user_dir(self, user_id: str) -> Path:
        return self.root / user_id

    def user_dir_exists(self, user_id: str) -> bool:
        return self._user_dir(user_id).exists()

    def profile_exists(self, user_id: str) -> bool:
        return (self._user_dir(user_id) / "USER.md").exists()

    def read_profile(self, user_id: str) -> str | None:
        p = self._user_dir(user_id) / "USER.md"
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8")

    def write_profile(self, user_id: str, body: str) -> None:
        ud = self._user_dir(user_id)
        ud.mkdir(parents=True, exist_ok=True)
        (ud / "USER.md").write_text(body, encoding="utf-8")

    def read_compiled_memory(self, user_id: str) -> str | None:
        p = self._user_dir(user_id) / "MEMORY.md"
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8")

    def write_compiled_memory(self, user_id: str, body: str) -> None:
        ud = self._user_dir(user_id)
        ud.mkdir(parents=True, exist_ok=True)
        (ud / "MEMORY.md").write_text(body, encoding="utf-8")

    def compiled_memory_mtime(self, user_id: str) -> float | None:
        p = self._user_dir(user_id) / "MEMORY.md"
        return p.stat().st_mtime if p.exists() else None

    def list_session_digests(self, user_id: str) -> list[str]:
        md = self._user_dir(user_id) / "memory"
        if not md.exists():
            return []
        return sorted(p.name for p in md.glob("*.md"))

    def read_session_digest(self, user_id: str, filename: str) -> str:
        return (self._user_dir(user_id) / "memory" / filename).read_text(
            encoding="utf-8", errors="replace"
        )

    def write_session_digest(self, user_id: str, filename: str, body: str) -> None:
        md = self._user_dir(user_id) / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / filename).write_text(body, encoding="utf-8")

    def session_digest_mtime(self, user_id: str, filename: str) -> float | None:
        p = self._user_dir(user_id) / "memory" / filename
        return p.stat().st_mtime if p.exists() else None

    def list_user_ids(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(
            p.name for p in self.root.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )


# ---------------------------------------------------------------------------
# S3 implementation
# ---------------------------------------------------------------------------
class S3UserStorage:
    """S3-backed UserStorage. Lazy-imports boto3 so local dev doesn't need it."""

    def __init__(self, bucket: str, prefix: str = "users", region: str = "us-east-1"):
        import boto3  # lazy — only imported when this backend is actually used
        self.bucket = bucket
        # Normalize the prefix: no leading slash, trailing slash for joining.
        self.prefix = prefix.strip("/") + "/" if prefix else ""
        self.s3 = boto3.client("s3", region_name=region)

    # ---- key helpers ------------------------------------------------------
    def _key_profile(self, user_id: str) -> str:
        return f"{self.prefix}{user_id}/USER.md"

    def _key_memory(self, user_id: str) -> str:
        return f"{self.prefix}{user_id}/MEMORY.md"

    def _key_digest(self, user_id: str, filename: str) -> str:
        return f"{self.prefix}{user_id}/memory/{filename}"

    def _key_user_prefix(self, user_id: str) -> str:
        return f"{self.prefix}{user_id}/"

    def _key_digest_prefix(self, user_id: str) -> str:
        return f"{self.prefix}{user_id}/memory/"

    # ---- low-level S3 ops -------------------------------------------------
    def _object_exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # boto3 raises ClientError for 404 + others
            return False

    def _get_text(self, key: str) -> str | None:
        try:
            res = self.s3.get_object(Bucket=self.bucket, Key=key)
            return res["Body"].read().decode("utf-8", errors="replace")
        except Exception:
            return None

    def _put_text(self, key: str, body: str) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )

    def _object_mtime(self, key: str) -> float | None:
        try:
            res = self.s3.head_object(Bucket=self.bucket, Key=key)
            lm = res.get("LastModified")
            if lm is None:
                return None
            # boto returns timezone-aware datetimes
            return lm.timestamp() if lm.tzinfo else lm.replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return None

    def _list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                keys.append(obj["Key"])
        return keys

    # ---- protocol surface -------------------------------------------------
    def user_dir_exists(self, user_id: str) -> bool:
        # Any object under the user's prefix counts.
        page = self.s3.list_objects_v2(
            Bucket=self.bucket,
            Prefix=self._key_user_prefix(user_id),
            MaxKeys=1,
        )
        return page.get("KeyCount", 0) > 0

    def profile_exists(self, user_id: str) -> bool:
        return self._object_exists(self._key_profile(user_id))

    def read_profile(self, user_id: str) -> str | None:
        return self._get_text(self._key_profile(user_id))

    def write_profile(self, user_id: str, body: str) -> None:
        self._put_text(self._key_profile(user_id), body)

    def read_compiled_memory(self, user_id: str) -> str | None:
        return self._get_text(self._key_memory(user_id))

    def write_compiled_memory(self, user_id: str, body: str) -> None:
        self._put_text(self._key_memory(user_id), body)

    def compiled_memory_mtime(self, user_id: str) -> float | None:
        return self._object_mtime(self._key_memory(user_id))

    def list_session_digests(self, user_id: str) -> list[str]:
        prefix = self._key_digest_prefix(user_id)
        keys = self._list_keys(prefix)
        # Strip the prefix and any leading slash, return just the filenames.
        names = [k[len(prefix):] for k in keys if k.endswith(".md")]
        return sorted(n for n in names if n and "/" not in n)

    def read_session_digest(self, user_id: str, filename: str) -> str:
        text = self._get_text(self._key_digest(user_id, filename))
        if text is None:
            raise FileNotFoundError(filename)
        return text

    def write_session_digest(self, user_id: str, filename: str, body: str) -> None:
        self._put_text(self._key_digest(user_id, filename), body)

    def session_digest_mtime(self, user_id: str, filename: str) -> float | None:
        return self._object_mtime(self._key_digest(user_id, filename))

    def list_user_ids(self) -> list[str]:
        # Use delimiter to get folder-level entries cheaply.
        ids: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.bucket, Prefix=self.prefix, Delimiter="/"
        ):
            for cp in page.get("CommonPrefixes", []) or []:
                p = cp.get("Prefix", "")
                # "users/<id>/" → "<id>"
                relative = p[len(self.prefix):].rstrip("/")
                if relative and not relative.startswith("."):
                    ids.append(relative)
        return sorted(ids)


# ---------------------------------------------------------------------------
# Factory + module-level singleton
# ---------------------------------------------------------------------------
def _build() -> UserStorage:
    backend = (os.getenv("USERS_STORAGE_BACKEND") or "local").strip().lower()
    if backend == "s3":
        bucket = os.getenv("USERS_S3_BUCKET")
        if not bucket:
            raise RuntimeError(
                "USERS_STORAGE_BACKEND=s3 requires USERS_S3_BUCKET to be set"
            )
        prefix = os.getenv("USERS_S3_PREFIX", "users")
        region = os.getenv("AWS_REGION", "us-east-1")
        log.info("UserStorage: S3 backend (bucket=%s prefix=%s region=%s)",
                 bucket, prefix, region)
        return S3UserStorage(bucket=bucket, prefix=prefix, region=region)
    # Local default — keeps ./run.sh working without env changes.
    default_local = Path(__file__).parent / "users"
    local_dir = Path(os.getenv("USERS_LOCAL_DIR", str(default_local)))
    log.info("UserStorage: local backend (root=%s)", local_dir)
    return LocalUserStorage(root=local_dir)


# One process-wide instance. advisor_backend.py imports this directly.
storage: UserStorage = _build()
