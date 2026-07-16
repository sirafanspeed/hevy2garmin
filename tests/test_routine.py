"""Tests for Hevy routine → Garmin planned-workout conversion and ops."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hevy2garmin import sync as sync_module
from hevy2garmin.sync import routine_schedule_dates
from hevy2garmin.db_sqlite import SQLiteDatabase
from hevy2garmin.garmin import create_workout, delete_workout, schedule_workout
from hevy2garmin.mapper import fit_exercise_strings
from hevy2garmin.routine import routine_to_garmin_workout, workout_content_hash


class TestFitExerciseStrings:
    def test_known_category_and_exercise(self) -> None:
        # Bench Press (Barbell) maps to FIT (0, 1).
        assert fit_exercise_strings(0, 1) == ("BENCH_PRESS", "BARBELL_BENCH_PRESS")

    def test_unknown_category_is_none(self) -> None:
        assert fit_exercise_strings(65534, 0) == (None, None)

    def test_bad_subcategory_keeps_category(self) -> None:
        cat, name = fit_exercise_strings(0, 99999)
        assert cat == "BENCH_PRESS"
        assert name is None


class TestRoutineToGarminWorkout:
    def _routine(self) -> dict:
        return {
            "id": "r1",
            "title": "Push Day",
            "notes": "chest/shoulders",
            "exercises": [
                {
                    "title": "Bench Press (Barbell)",
                    "sets": [
                        {"type": "warmup", "reps": 10, "weight_kg": 40},
                        {"type": "normal", "reps": 8, "weight_kg": 60},
                    ],
                },
                {
                    "title": "Totally Made Up Exercise",
                    "sets": [{"type": "normal", "reps": 12, "weight_kg": None}],
                },
            ],
        }

    def test_top_level_shape(self) -> None:
        payload = routine_to_garmin_workout(self._routine())
        assert payload["workoutName"] == "Push Day"
        assert payload["description"] == "chest/shoulders"
        assert payload["sportType"]["sportTypeKey"] == "strength_training"
        assert len(payload["workoutSegments"]) == 1

    def test_steps_and_order(self) -> None:
        steps = routine_to_garmin_workout(self._routine())["workoutSegments"][0]["workoutSteps"]
        assert [s["stepOrder"] for s in steps] == [1, 2, 3]

    def test_warmup_vs_working_step_type(self) -> None:
        steps = routine_to_garmin_workout(self._routine())["workoutSegments"][0]["workoutSteps"]
        assert steps[0]["stepType"]["stepTypeKey"] == "warmup"
        assert steps[1]["stepType"]["stepTypeKey"] == "interval"

    def test_reps_and_weight_encoding(self) -> None:
        steps = routine_to_garmin_workout(self._routine())["workoutSegments"][0]["workoutSteps"]
        assert steps[1]["endCondition"]["conditionTypeKey"] == "reps"
        assert steps[1]["endConditionValue"] == 8.0
        assert steps[1]["weightValue"] == 60.0
        assert steps[1]["weightUnit"]["unitKey"] == "kilogram"

    def test_mapped_exercise_carries_garmin_strings(self) -> None:
        steps = routine_to_garmin_workout(self._routine())["workoutSegments"][0]["workoutSteps"]
        assert steps[0]["category"] == "BENCH_PRESS"
        assert steps[0]["exerciseName"] == "BARBELL_BENCH_PRESS"

    def test_unmapped_exercise_falls_back_to_named_step(self) -> None:
        steps = routine_to_garmin_workout(self._routine())["workoutSegments"][0]["workoutSteps"]
        unknown = steps[2]
        assert "category" not in unknown
        assert "exerciseName" not in unknown
        assert unknown["stepName"] == "Totally Made Up Exercise"

    def test_no_weight_omits_weight_fields(self) -> None:
        steps = routine_to_garmin_workout(self._routine())["workoutSegments"][0]["workoutSteps"]
        assert "weightValue" not in steps[2]

    def test_pound_conversion(self) -> None:
        payload = routine_to_garmin_workout(self._routine(), weight_unit="pound")
        step = payload["workoutSegments"][0]["workoutSteps"][1]  # 60 kg working set
        assert step["weightUnit"]["unitKey"] == "pound"
        assert step["weightValue"] == round(60 * 2.2046226218, 2)

    def test_duration_based_step(self) -> None:
        routine = {"title": "Core", "exercises": [
            {"title": "Plank", "sets": [{"type": "normal", "duration_seconds": 60}]},
        ]}
        step = routine_to_garmin_workout(routine)["workoutSegments"][0]["workoutSteps"][0]
        assert step["endCondition"]["conditionTypeKey"] == "time"
        assert step["endConditionValue"] == 60.0

    def test_empty_routine(self) -> None:
        payload = routine_to_garmin_workout({"title": "Empty", "exercises": []})
        assert payload["workoutSegments"][0]["workoutSteps"] == []

    def test_no_rest_by_default(self) -> None:
        # _routine() has no rest_seconds and no default → no rest steps.
        steps = routine_to_garmin_workout(self._routine())["workoutSegments"][0]["workoutSteps"]
        assert all(s["stepType"]["stepTypeKey"] != "rest" for s in steps)


class TestRestSteps:
    def _routine(self, rest=None) -> dict:
        ex: dict = {"title": "Bench Press (Barbell)", "sets": [
            {"type": "normal", "reps": 8, "weight_kg": 60},
            {"type": "normal", "reps": 8, "weight_kg": 60},
            {"type": "normal", "reps": 8, "weight_kg": 60},
        ]}
        if rest is not None:
            ex["rest_seconds"] = rest
        return {"title": "Push", "exercises": [ex]}

    def test_rest_between_sets_from_hevy(self) -> None:
        steps = routine_to_garmin_workout(self._routine(rest=90))["workoutSegments"][0]["workoutSteps"]
        # 3 sets + 2 rests between them.
        assert [s["stepType"]["stepTypeKey"] for s in steps] == [
            "interval", "rest", "interval", "rest", "interval"]
        assert [s["stepOrder"] for s in steps] == [1, 2, 3, 4, 5]

    def test_rest_step_shape(self) -> None:
        steps = routine_to_garmin_workout(self._routine(rest=90))["workoutSegments"][0]["workoutSteps"]
        rest = steps[1]
        assert rest["stepType"] == {"stepTypeId": 5, "stepTypeKey": "rest"}
        assert rest["endCondition"]["conditionTypeKey"] == "time"
        assert rest["endConditionValue"] == 90.0
        assert "category" not in rest and "weightValue" not in rest

    def test_no_rest_after_last_set(self) -> None:
        steps = routine_to_garmin_workout(self._routine(rest=90))["workoutSegments"][0]["workoutSteps"]
        assert steps[-1]["stepType"]["stepTypeKey"] == "interval"

    def test_hevy_rest_overrides_default(self) -> None:
        steps = routine_to_garmin_workout(
            self._routine(rest=30), default_rest_seconds=120
        )["workoutSegments"][0]["workoutSteps"]
        assert steps[1]["endConditionValue"] == 30.0

    def test_default_used_when_hevy_omits(self) -> None:
        steps = routine_to_garmin_workout(
            self._routine(), default_rest_seconds=75
        )["workoutSegments"][0]["workoutSteps"]
        rests = [s for s in steps if s["stepType"]["stepTypeKey"] == "rest"]
        assert len(rests) == 2
        assert all(s["endConditionValue"] == 75.0 for s in rests)

    def test_zero_rest_adds_no_steps(self) -> None:
        steps = routine_to_garmin_workout(self._routine(rest=0))["workoutSegments"][0]["workoutSteps"]
        assert all(s["stepType"]["stepTypeKey"] != "rest" for s in steps)

    def test_rest_not_added_across_exercises(self) -> None:
        routine = {"title": "Full", "exercises": [
            {"title": "Bench Press (Barbell)", "rest_seconds": 60,
             "sets": [{"type": "normal", "reps": 5, "weight_kg": 60}]},
            {"title": "Bench Press (Barbell)", "rest_seconds": 60,
             "sets": [{"type": "normal", "reps": 5, "weight_kg": 60}]},
        ]}
        steps = routine_to_garmin_workout(routine)["workoutSegments"][0]["workoutSteps"]
        # One set each, so no intra-exercise rest and none between exercises.
        assert [s["stepType"]["stepTypeKey"] for s in steps] == ["interval", "interval"]


class TestGarminWorkoutOps:
    def test_create_workout_posts_and_returns_id(self) -> None:
        client = MagicMock()
        client.client.request.return_value.json.return_value = {"workoutId": 999}
        wid = create_workout(client, {"workoutName": "Push"})
        assert wid == 999
        method, service, path = client.client.request.call_args[0][:3]
        assert method == "POST"
        assert path == "/workout-service/workout"
        assert client.client.request.call_args[1]["json"] == {"workoutName": "Push"}

    def test_create_workout_missing_id_returns_none(self) -> None:
        client = MagicMock()
        client.client.request.return_value.json.return_value = {}
        assert create_workout(client, {"workoutName": "Push"}) is None

    def test_delete_workout_hits_delete_endpoint(self) -> None:
        client = MagicMock()
        delete_workout(client, 42)
        method, service, path = client.client.request.call_args[0][:3]
        assert method == "DELETE"
        assert path == "/workout-service/workout/42"

    def test_schedule_workout_posts_date(self) -> None:
        client = MagicMock()
        schedule_workout(client, 42, "2026-08-01")
        method, service, path = client.client.request.call_args[0][:3]
        assert method == "POST"
        assert path == "/workout-service/schedule/42"
        assert client.client.request.call_args[1]["json"] == {"date": "2026-08-01"}


class TestSyncRoutines:
    def _patched(self, tmp_path: Path, routines: list[dict]):
        """Patch sync_routines' collaborators; return (db, create_mock, schedule_mock)."""
        store = SQLiteDatabase(tmp_path / "routines.db")
        hevy = MagicMock()
        hevy.get_routines.return_value = {"routines": routines, "page_count": 1}
        create_mock = MagicMock(return_value=777)
        schedule_mock = MagicMock()
        patches = [
            patch.object(sync_module, "load_config", return_value={
                "hevy_api_key": "k", "garmin_email": "e", "garmin_password": "p"}),
            patch.object(sync_module.db, "get_db", return_value=store),
            patch.object(sync_module, "HevyClient", return_value=hevy),
            patch.object(sync_module, "get_client", return_value=MagicMock()),
            patch.object(sync_module, "create_workout", create_mock),
            patch.object(sync_module, "delete_workout", MagicMock()),
            patch.object(sync_module, "schedule_workout", schedule_mock),
        ]
        return store, create_mock, schedule_mock, patches

    def test_creates_and_tracks(self, tmp_path: Path) -> None:
        routines = [{"id": "r1", "title": "Push", "updated_at": "2026-01-01T00:00:00Z",
                     "exercises": [{"title": "Bench Press (Barbell)",
                                    "sets": [{"type": "normal", "reps": 5, "weight_kg": 60}]}]}]
        store, create_mock, _, patches = self._patched(tmp_path, routines)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            result = sync_module.sync_routines()
        assert result == {"created": 1, "updated": 0, "skipped": 0, "failed": 0,
                          "scheduled": 0, "total": 1}
        create_mock.assert_called_once()
        assert store.get_synced_routine("r1")["garmin_workout_id"] == "777"

    def _hash_for(self, routine: dict) -> str:
        # Mirror how sync_routines builds the payload (no timing config → 75s default).
        payload = routine_to_garmin_workout(routine, weight_unit="kilogram", default_rest_seconds=75)
        return workout_content_hash(payload)

    def test_skips_when_hash_unchanged(self, tmp_path: Path) -> None:
        routines = [{"id": "r1", "title": "Push", "updated_at": "2026-01-01T00:00:00Z", "exercises": []}]
        store, create_mock, _, patches = self._patched(tmp_path, routines)
        store.mark_routine_synced("r1", garmin_workout_id="777",
                                  content_hash=self._hash_for(routines[0]))
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            result = sync_module.sync_routines()
        assert result["skipped"] == 1
        assert result["created"] == 0
        create_mock.assert_not_called()

    def test_resyncs_when_hash_changed(self, tmp_path: Path) -> None:
        routines = [{"id": "r1", "title": "Push", "updated_at": "2026-01-01T00:00:00Z", "exercises": []}]
        store, create_mock, _, patches = self._patched(tmp_path, routines)
        delete_mock = MagicMock()
        # Stored under a stale hash → payload differs → recreate.
        store.mark_routine_synced("r1", garmin_workout_id="555", content_hash="stale-hash")
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(sync_module, "delete_workout", delete_mock), patches[6]:
            result = sync_module.sync_routines()
        assert result["updated"] == 1
        assert result["created"] == 0
        assert result["skipped"] == 0
        create_mock.assert_called_once()
        delete_mock.assert_called_once_with(delete_mock.call_args[0][0], "555")
        assert store.get_synced_routine("r1")["content_hash"] == self._hash_for(routines[0])

    def test_schedule_when_date_given(self, tmp_path: Path) -> None:
        routines = [{"id": "r1", "title": "Push", "updated_at": "2026-01-01T00:00:00Z", "exercises": []}]
        store, _, schedule_mock, patches = self._patched(tmp_path, routines)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            result = sync_module.sync_routines(schedule_date="2026-08-01")
        assert result["scheduled"] == 1
        assert schedule_mock.call_count == 1
        assert schedule_mock.call_args[0][1:] == (777, "2026-08-01")
        assert store.get_synced_routine("r1")["scheduled_date"] == "2026-08-01"

    def test_force_recreates_already_synced(self, tmp_path: Path) -> None:
        routines = [{"id": "r1", "title": "Push", "updated_at": "2026-01-01T00:00:00Z",
                     "exercises": [{"title": "Bench Press (Barbell)",
                                    "sets": [{"type": "normal", "reps": 5, "weight_kg": 60}]}]}]
        store, create_mock, _, patches = self._patched(tmp_path, routines)
        store.mark_routine_synced("r1", garmin_workout_id="555",
                                  hevy_updated_at="2026-01-01T00:00:00Z")
        delete_mock = MagicMock()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(sync_module, "delete_workout", delete_mock), patches[6]:
            result = sync_module.sync_routines(force=True)
        # Forcing a routine that was already synced counts as an update.
        assert result["updated"] == 1
        assert result["created"] == 0
        assert result["skipped"] == 0
        create_mock.assert_called_once()
        delete_mock.assert_called_once_with(delete_mock.call_args[0][0], "555")
        assert store.get_synced_routine("r1")["garmin_workout_id"] == "777"

    def test_dry_run_does_not_call_garmin(self, tmp_path: Path) -> None:
        routines = [{"id": "r1", "title": "Push", "updated_at": "2026-01-01T00:00:00Z", "exercises": []}]
        store, create_mock, _, patches = self._patched(tmp_path, routines)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            result = sync_module.sync_routines(dry_run=True)
        assert result["created"] == 1
        create_mock.assert_not_called()
        assert store.get_synced_routine("r1") is None

    def test_resync_preserves_prior_schedule(self, tmp_path: Path) -> None:
        # A routine synced AND scheduled, then re-synced because its content changed:
        # recreating the Garmin workout drops its calendar entry, so the stored date
        # must be re-applied to the new workout (a restore, not a new booking).
        routines = [{"id": "r1", "title": "Push", "updated_at": "2026-01-01T00:00:00Z", "exercises": []}]
        store, create_mock, schedule_mock, patches = self._patched(tmp_path, routines)
        store.mark_routine_synced("r1", garmin_workout_id="555",
                                  scheduled_date="2026-08-01", content_hash="stale-hash")
        delete_mock = MagicMock()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(sync_module, "delete_workout", delete_mock), patches[6]:
            result = sync_module.sync_routines()
        assert result["updated"] == 1
        # Restoring the old date is not counted as a new schedule.
        assert result["scheduled"] == 0
        # The new workout (777) is re-scheduled on the stored date...
        schedule_mock.assert_called_once()
        assert schedule_mock.call_args[0][1:] == (777, "2026-08-01")
        # ...and the date is kept on the record instead of being wiped to None.
        assert store.get_synced_routine("r1")["scheduled_date"] == "2026-08-01"


class TestRoutineScheduleDates:
    def test_once_returns_single_date(self) -> None:
        assert routine_schedule_dates("once", date="2026-07-20") == ["2026-07-20"]

    def test_once_requires_date(self) -> None:
        with pytest.raises(ValueError):
            routine_schedule_dates("once")

    def test_once_rejects_bad_date(self) -> None:
        with pytest.raises(ValueError):
            routine_schedule_dates("once", date="not-a-date")

    def test_recurring_weekly(self) -> None:
        # 2026-07-15 is a Wednesday; first Monday on/after is 2026-07-20.
        dates = routine_schedule_dates("recurring", weekday=0, start_date="2026-07-15", weeks=5)
        assert dates == ["2026-07-20", "2026-07-27", "2026-08-03", "2026-08-10", "2026-08-17"]

    def test_recurring_start_on_weekday_includes_start(self) -> None:
        # 2026-07-20 is a Monday → the start date itself is the first occurrence.
        dates = routine_schedule_dates("recurring", weekday=0, start_date="2026-07-20", weeks=2)
        assert dates == ["2026-07-20", "2026-07-27"]

    def test_recurring_accepts_string_inputs(self) -> None:
        dates = routine_schedule_dates("recurring", weekday="2", start_date="2026-07-15", weeks="1")
        assert dates == ["2026-07-15"]  # 2026-07-15 is a Wednesday (weekday 2)

    def test_recurring_requires_all_fields(self) -> None:
        with pytest.raises(ValueError):
            routine_schedule_dates("recurring", weekday=0, weeks=3)

    def test_recurring_rejects_bad_weekday(self) -> None:
        with pytest.raises(ValueError):
            routine_schedule_dates("recurring", weekday=9, start_date="2026-07-15", weeks=2)

    def test_recurring_capped(self) -> None:
        dates = routine_schedule_dates("recurring", weekday=0, start_date="2026-01-05", weeks=999)
        assert len(dates) == sync_module.MAX_SCHEDULE_OCCURRENCES

    def test_unknown_mode(self) -> None:
        with pytest.raises(ValueError):
            routine_schedule_dates("bogus")


class TestScheduleRoutine:
    def _patched(self, tmp_path: Path):
        store = SQLiteDatabase(tmp_path / "sched.db")
        schedule_mock = MagicMock()
        client = MagicMock()
        patches = [
            patch.object(sync_module, "load_config", return_value={
                "garmin_email": "e", "garmin_password": "p"}),
            patch.object(sync_module.db, "get_db", return_value=store),
            patch.object(sync_module, "get_client", return_value=client),
            patch.object(sync_module, "schedule_workout", schedule_mock),
        ]
        return store, schedule_mock, patches

    def test_schedules_each_date(self, tmp_path: Path) -> None:
        store, schedule_mock, patches = self._patched(tmp_path)
        store.mark_routine_synced("r1", garmin_workout_id="900", title="Push")
        dates = ["2026-07-20", "2026-07-27"]
        with patches[0], patches[1], patches[2], patches[3]:
            result = sync_module.schedule_routine("r1", dates)
        assert result == {"scheduled": 2, "workout_id": "900", "dates": dates}
        assert schedule_mock.call_count == 2
        assert [c.args[1:] for c in schedule_mock.call_args_list] == [
            ("900", "2026-07-20"), ("900", "2026-07-27")]
        # Earliest date persisted for display.
        assert store.get_synced_routine("r1")["scheduled_date"] == "2026-07-20"

    def test_raises_when_not_synced(self, tmp_path: Path) -> None:
        store, schedule_mock, patches = self._patched(tmp_path)
        with patches[0], patches[1], patches[2], patches[3]:
            with pytest.raises(ValueError, match="not synced"):
                sync_module.schedule_routine("missing", ["2026-07-20"])
        schedule_mock.assert_not_called()

    def test_raises_on_empty_dates(self, tmp_path: Path) -> None:
        store, _, patches = self._patched(tmp_path)
        store.mark_routine_synced("r1", garmin_workout_id="900")
        with patches[0], patches[1], patches[2], patches[3]:
            with pytest.raises(ValueError):
                sync_module.schedule_routine("r1", [])
