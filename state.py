"""SQLite state tracking for sync operations."""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from models import SyncState

logger = logging.getLogger(__name__)


class StateStore:
    """SQLite-based state tracking for photo sync operations."""

    def __init__(self, db_path: str = "data/sync_state.db"):
        """
        Initialize the state store.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database schema."""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    immich_id TEXT PRIMARY KEY,
                    skylight_id TEXT NOT NULL,
                    synced_at TEXT NOT NULL,
                    checksum TEXT
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_skylight_id
                ON sync_state(skylight_id)
            """)

            # Track sync runs for debugging
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    photos_added INTEGER DEFAULT 0,
                    photos_removed INTEGER DEFAULT 0,
                    errors INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'running'
                )
            """)

            conn.commit()

    @contextmanager
    def _connect(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def get_sync_state(self, immich_id: str) -> Optional[SyncState]:
        """
        Get the sync state for an Immich asset.

        Args:
            immich_id: Immich asset ID

        Returns:
            SyncState if found, None otherwise
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_state WHERE immich_id = ?",
                (immich_id,),
            ).fetchone()

            if row:
                return SyncState(
                    immich_id=row["immich_id"],
                    skylight_id=row["skylight_id"],
                    synced_at=datetime.fromisoformat(row["synced_at"]),
                    checksum=row["checksum"],
                )
            return None

    def get_by_skylight_id(self, skylight_id: str) -> Optional[SyncState]:
        """
        Get the sync state by Skylight photo ID.

        Args:
            skylight_id: Skylight photo ID

        Returns:
            SyncState if found, None otherwise
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_state WHERE skylight_id = ?",
                (skylight_id,),
            ).fetchone()

            if row:
                return SyncState(
                    immich_id=row["immich_id"],
                    skylight_id=row["skylight_id"],
                    synced_at=datetime.fromisoformat(row["synced_at"]),
                    checksum=row["checksum"],
                )
            return None

    def get_all_states(self) -> list[SyncState]:
        """Get all sync states."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM sync_state").fetchall()
            return [
                SyncState(
                    immich_id=row["immich_id"],
                    skylight_id=row["skylight_id"],
                    synced_at=datetime.fromisoformat(row["synced_at"]),
                    checksum=row["checksum"],
                )
                for row in rows
            ]

    def save_sync_state(
        self,
        immich_id: str,
        skylight_id: str,
        checksum: Optional[str] = None,
    ) -> None:
        """
        Save or update a sync state.

        Args:
            immich_id: Immich asset ID
            skylight_id: Skylight photo ID
            checksum: Optional checksum for change detection
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sync_state
                (immich_id, skylight_id, synced_at, checksum)
                VALUES (?, ?, ?, ?)
                """,
                (immich_id, skylight_id, datetime.now().isoformat(), checksum),
            )
            conn.commit()

    def remove_sync_state(self, immich_id: str) -> bool:
        """
        Remove a sync state by Immich ID.

        Args:
            immich_id: Immich asset ID

        Returns:
            True if a row was deleted
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM sync_state WHERE immich_id = ?",
                (immich_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def remove_by_skylight_id(self, skylight_id: str) -> bool:
        """
        Remove a sync state by Skylight ID.

        Args:
            skylight_id: Skylight photo ID

        Returns:
            True if a row was deleted
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM sync_state WHERE skylight_id = ?",
                (skylight_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_synced_immich_ids(self) -> set[str]:
        """Get set of all synced Immich IDs."""
        with self._connect() as conn:
            rows = conn.execute("SELECT immich_id FROM sync_state").fetchall()
            return {row["immich_id"] for row in rows}

    def get_synced_skylight_ids(self) -> set[str]:
        """Get set of all synced Skylight IDs."""
        with self._connect() as conn:
            rows = conn.execute("SELECT skylight_id FROM sync_state").fetchall()
            return {row["skylight_id"] for row in rows}

    # Sync run tracking

    def start_sync_run(self) -> int:
        """
        Record the start of a sync run.

        Returns:
            Run ID for later updates
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO sync_runs (started_at) VALUES (?)",
                (datetime.now().isoformat(),),
            )
            conn.commit()
            return cursor.lastrowid

    def complete_sync_run(
        self,
        run_id: int,
        photos_added: int = 0,
        photos_removed: int = 0,
        errors: int = 0,
        status: str = "completed",
    ) -> None:
        """
        Record the completion of a sync run.

        Args:
            run_id: Run ID from start_sync_run
            photos_added: Number of photos uploaded
            photos_removed: Number of photos deleted
            errors: Number of errors encountered
            status: Final status (completed, failed, partial)
        """
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sync_runs SET
                    completed_at = ?,
                    photos_added = ?,
                    photos_removed = ?,
                    errors = ?,
                    status = ?
                WHERE id = ?
                """,
                (
                    datetime.now().isoformat(),
                    photos_added,
                    photos_removed,
                    errors,
                    status,
                    run_id,
                ),
            )
            conn.commit()

    def get_last_sync_run(self) -> Optional[dict]:
        """Get information about the last sync run."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()

            if row:
                return dict(row)
            return None
