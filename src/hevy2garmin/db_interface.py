"""Abstract database interface for hevy2garmin."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class NoWritableDatabaseError(RuntimeError):
    """No Postgres URL is set and the local SQLite fallback cannot be written.

    Happens on serverless deploys (Vercel/Lambda) where the home filesystem is
    read-only and no database has been attached. Handlers catch this to show an
    actionable "add a database" message instead of a raw 500 (#145, #142).
    """


class Database(ABC):
    """Abstract base class for workout sync storage."""

    @abstractmethod
    def is_synced(self, hevy_id: str) -> bool:
        """Check if a Hevy workout has already been synced."""

    @abstractmethod
    def get_garmin_id(self, hevy_id: str) -> str | None:
        """Get the Garmin activity ID for a synced workout."""

    @abstractmethod
    def mark_synced(
        self,
        hevy_id: str,
        garmin_activity_id: str | None = None,
        title: str = "",
        calories: int | None = None,
        avg_hr: int | None = None,
        hevy_updated_at: str | None = None,
        sync_method: str = "upload",
    ) -> None:
        """Record a successfully synced workout."""

    @abstractmethod
    def get_stale_synced(self, workouts: list[dict]) -> list[str]:
        """Return hevy_ids of synced workouts edited on Hevy since sync."""

    @abstractmethod
    def get_synced_count(self) -> int:
        """Get total number of synced workouts."""

    @abstractmethod
    def get_recent_synced(self, limit: int = 10) -> list[dict]:
        """Get recently synced workouts."""

    @abstractmethod
    def record_sync_log(
        self,
        synced: int = 0,
        skipped: int = 0,
        failed: int = 0,
        trigger: str = "manual",
    ) -> None:
        """Persist a sync run result."""

    @abstractmethod
    def get_sync_log(self, limit: int = 20) -> list[dict]:
        """Get recent sync log entries."""

    @abstractmethod
    def get_cached_hr(self, hevy_id: str) -> dict | None:
        """Get cached HR data for a workout. Returns None if not cached."""

    @abstractmethod
    def cache_hr(self, hevy_id: str, data: dict) -> None:
        """Cache HR data for a workout."""

    @abstractmethod
    def unsync(self, hevy_id: str) -> bool:
        """Remove a sync record. Returns True if a record was deleted."""

    @abstractmethod
    def unsync_all(self) -> int:
        """Remove all sync records. Returns count of deleted records."""

    @abstractmethod
    def get_app_config(self, key: str) -> dict | None:
        """Get a JSON value from the generic key-value app cache."""

    @abstractmethod
    def set_app_config(self, key: str, value: dict) -> None:
        """Store a JSON value in the generic key-value app cache."""

    @abstractmethod
    def claim_pending(self, hevy_id: str, payload: dict[str, Any]) -> bool:
        """Atomically claim submission authority for a workout."""

    @abstractmethod
    def get_pending(self, hevy_id: str) -> dict | None:
        """Return one unfinished upload operation."""

    @abstractmethod
    def list_pending(self) -> list[dict]:
        """Return unfinished upload operations, newest first."""

    @abstractmethod
    def update_pending(self, hevy_id: str, **changes: Any) -> None:
        """Persist selected pending-operation fields."""

    @abstractmethod
    def delete_pending(self, hevy_id: str) -> bool:
        """Abandon an unfinished operation."""

    @abstractmethod
    def complete_pending(self, hevy_id: str, terminal: dict[str, Any]) -> None:
        """Atomically upsert terminal state and remove pending state."""

    @abstractmethod
    def resolve_terminal(
        self,
        hevy_id: str,
        *,
        status: str,
        garmin_activity_id: str | None = None,
        reason: str | None = None,
        source: str | None = None,
    ) -> None:
        """Atomically create a manual/skipped terminal state and clear pending."""

    @abstractmethod
    def get_workout_states(self, hevy_ids: list[str]) -> dict[str, dict]:
        """Batch-fetch terminal and pending state with terminal precedence."""

    @abstractmethod
    def get_terminal_counts(self) -> dict[str, int]:
        """Return uploaded, manual, skipped, and total terminal counts."""

    # ── Routine → Garmin planned-workout tracking ───────────────────────────
    @abstractmethod
    def get_synced_routine(self, hevy_routine_id: str) -> dict | None:
        """Return the sync record for a Hevy routine, or None if never synced."""

    @abstractmethod
    def is_routine_synced(self, hevy_routine_id: str, hevy_updated_at: str | None = None) -> bool:
        """True if the routine was synced and (if given) not edited on Hevy since."""

    @abstractmethod
    def mark_routine_synced(
        self,
        hevy_routine_id: str,
        garmin_workout_id: str | None = None,
        title: str = "",
        hevy_updated_at: str | None = None,
        scheduled_date: str | None = None,
        content_hash: str | None = None,
    ) -> None:
        """Record a routine synced to a Garmin planned workout."""

    @abstractmethod
    def delete_synced_routine(self, hevy_routine_id: str) -> bool:
        """Remove a routine sync record. Returns True if a record was deleted."""

    @abstractmethod
    def get_routine_stats(self) -> dict:
        """Return routine sync counts: ``{"synced": int, "scheduled": int}``."""

    @abstractmethod
    def get_recent_synced_routines(self, limit: int = 5) -> list[dict]:
        """Return recently synced routines, newest first."""
