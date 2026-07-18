"""Build a Garmin Connect planned-workout payload from a Hevy routine.

A Hevy *routine* is a template (a plan of exercises/sets/reps), not a logged
session, so it maps to a Garmin **planned workout** in the Training/Workouts
library — not to an uploaded activity. This module converts a routine dict from
``GET /v1/routines`` into the JSON body accepted by Garmin's
``POST /workout-service/workout`` endpoint.

The payload shape is reverse-engineered (Garmin does not publish this API). The
numeric IDs below (``sportType``, ``stepType``, ``endCondition``, ``weightUnit``)
were **validated on 2026-07-16** by diffing a workout created by this builder
against a strength workout hand-authored in Garmin Connect (via
``client.get_workout_by_id``): Garmin accepted and stored every field verbatim,
and ``weightValue`` is in **kilograms** (not grams). Garmin fills in
``weightUnit.factor`` and ``targetType`` itself, so we omit them. The exercise
``category``/``exerciseName`` strings come from :func:`mapper.fit_exercise_strings`,
which derives them from the FIT SDK enums fit-tool ships.

Note: Garmin's own UI collapses identical consecutive sets into a
``RepeatGroupDTO`` with a rest step; this builder emits one flat ``interval``
step per set instead, which is equally valid and preserves per-set weights.
"""

from __future__ import annotations

import hashlib
import json
import logging

from hevy2garmin.mapper import fit_exercise_strings, lookup_exercise

logger = logging.getLogger("hevy2garmin")


def workout_content_hash(payload: dict) -> str:
    """Stable SHA-256 of a Garmin workout payload, for change detection.

    Hashing the *generated payload* (not the raw Hevy routine) means the hash
    changes both when the routine's exercises/sets change AND when this builder
    changes how it emits steps (e.g. adding rest steps). That lets ``sync_routines``
    re-create a workout whenever its content would differ, without relying on
    Hevy's ``updated_at`` or a manual ``--force``.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

# --- Garmin workout-service enums (validated against a real export 2026-07-16) #
SPORT_TYPE_STRENGTH = {"sportTypeId": 5, "sportTypeKey": "strength_training"}

# stepType: warmup sets vs. working sets vs. rest between sets.
_STEP_TYPE_WARMUP = {"stepTypeId": 1, "stepTypeKey": "warmup"}
_STEP_TYPE_INTERVAL = {"stepTypeId": 3, "stepTypeKey": "interval"}
_STEP_TYPE_REST = {"stepTypeId": 5, "stepTypeKey": "rest"}

# endCondition: how a step ends.
_END_REPS = {"conditionTypeId": 10, "conditionTypeKey": "reps"}
_END_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time"}
_END_LAP_BUTTON = {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}

_WEIGHT_UNIT_KG = {"unitId": 8, "unitKey": "kilogram"}
_WEIGHT_UNIT_LB = {"unitId": 7, "unitKey": "pound"}
_KG_TO_LB = 2.2046226218


def _weight_fields(set_data: dict, weight_unit: str) -> dict:
    """Weight fields for a step, or empty when the set has no weight."""
    weight_kg = set_data.get("weight_kg")
    if weight_kg is None:
        return {}
    if weight_unit == "pound":
        return {"weightValue": round(weight_kg * _KG_TO_LB, 2), "weightUnit": _WEIGHT_UNIT_LB}
    return {"weightValue": float(weight_kg), "weightUnit": _WEIGHT_UNIT_KG}


def _build_step(
    order: int,
    set_data: dict,
    exercise_title: str,
    category_str: str | None,
    exercise_name_str: str | None,
    weight_unit: str,
) -> dict:
    """Build one ``ExecutableStepDTO`` for a single Hevy set."""
    is_warmup = (set_data.get("type") or "").lower() == "warmup"
    step: dict = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": _STEP_TYPE_WARMUP if is_warmup else _STEP_TYPE_INTERVAL,
    }

    reps = set_data.get("reps")
    duration = set_data.get("duration_seconds")
    if reps is not None:
        step["endCondition"] = _END_REPS
        step["endConditionValue"] = float(reps)
    elif duration is not None:
        step["endCondition"] = _END_TIME
        step["endConditionValue"] = float(duration)
    else:
        step["endCondition"] = _END_LAP_BUTTON

    # Garmin identifies the strength exercise by string enums. When we can't map
    # it, fall back to a named step so the user still sees the exercise.
    if category_str is not None:
        step["category"] = category_str
    if exercise_name_str is not None:
        step["exerciseName"] = exercise_name_str
    if exercise_name_str is None:
        step["stepName"] = exercise_title

    step.update(_weight_fields(set_data, weight_unit))
    return step


def _rest_step(order: int, rest_seconds: int) -> dict:
    """Build a timed rest step (stepType ``rest``, ends after ``rest_seconds``)."""
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": _STEP_TYPE_REST,
        "endCondition": _END_TIME,
        "endConditionValue": float(rest_seconds),
    }


def routine_to_garmin_workout(
    routine: dict,
    *,
    weight_unit: str = "kilogram",
    default_rest_seconds: int | None = None,
) -> dict:
    """Convert a Hevy routine into a Garmin ``/workout-service/workout`` body.

    ``weight_unit`` is ``"kilogram"`` (default) or ``"pound"``; Hevy always
    stores ``weight_kg`` so pounds are converted. Exercises that don't map to a
    known FIT category become generic named steps (logged via ``UNKNOWN`` count).

    A timed ``rest`` step is inserted *between* consecutive sets of an exercise
    (never after its last set). The duration is the exercise's Hevy
    ``rest_seconds``; when Hevy omits it, ``default_rest_seconds`` is used, and
    if that is also falsy no rest steps are added.
    """
    exercises = routine.get("exercises") or []
    steps: list[dict] = []
    unknown = 0
    order = 1
    for exercise in exercises:
        title = exercise.get("title") or exercise.get("name") or "Exercise"
        template_id = exercise.get("exercise_template_id")
        category, subcategory, _ = lookup_exercise(title, template_id)
        category_str, exercise_name_str = fit_exercise_strings(category, subcategory)
        if category_str is None:
            unknown += 1

        rest_seconds = exercise.get("rest_seconds")
        if rest_seconds is None:
            rest_seconds = default_rest_seconds

        sets = exercise.get("sets") or []
        for i, set_data in enumerate(sets):
            steps.append(
                _build_step(order, set_data, title, category_str, exercise_name_str, weight_unit)
            )
            order += 1
            # Rest goes between sets of the same exercise, not after the last one.
            if rest_seconds and i < len(sets) - 1:
                steps.append(_rest_step(order, rest_seconds))
                order += 1

    name = routine.get("title") or routine.get("name") or "Hevy Routine"
    if unknown:
        logger.info("  Routine '%s': %d exercise(s) had no Garmin mapping", name, unknown)

    return {
        "workoutName": name,
        "description": (routine.get("notes") or "Synced from Hevy").strip()[:1024],
        "sportType": SPORT_TYPE_STRENGTH,
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": SPORT_TYPE_STRENGTH,
                "workoutSteps": steps,
            }
        ],
    }
