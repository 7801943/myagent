"""Per-user StateStore registry for multi-user web sessions."""

from __future__ import annotations

import re
from pathlib import Path

from myagent.context.state import SQLiteStateStore
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

_SAFE_USERNAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


def safe_username(raw: str) -> str:
    """Return a filesystem-safe username component."""
    value = _SAFE_USERNAME_RE.sub("_", str(raw or "").strip()).strip("._-")
    if not value or value in {".", ".."}:
        raise ValueError("Invalid username for per-user state path")
    return value


class UserStateStoreRegistry:
    """Lazily creates one SQLiteStateStore per user."""

    def __init__(self, base_dir: str | Path = "data/state/users"):
        self._base_dir = Path(base_dir).expanduser()
        self._stores: dict[str, SQLiteStateStore] = {}

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def db_path_for_user(self, username: str) -> Path:
        return self._base_dir / safe_username(username) / "myagent_state.db"

    async def get_store(self, username: str) -> SQLiteStateStore:
        key = safe_username(username)
        existing = self._stores.get(key)
        if existing is not None:
            return existing

        db_path = self.db_path_for_user(key)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = SQLiteStateStore(db_path)
        await store.initialize()
        self._stores[key] = store
        logger.info("Initialized per-user state DB: user=%s path=%s", key, db_path)
        return store

    async def close_all(self) -> None:
        for username, store in list(self._stores.items()):
            try:
                await store.close()
            except Exception as exc:
                logger.warning("Failed to close state DB for user %s: %s", username, exc)
        self._stores.clear()

