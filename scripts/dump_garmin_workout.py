"""Dump a Garmin planned-workout as JSON to reverse-engineer /workout-service.

The Hevy→Garmin *routine* sync (``hevy2garmin.routine``) builds the workout
payload from IDs discovered by reverse engineering — sportType, stepType,
endCondition, weightUnit, and whether ``weightValue`` is kg or grams. This
read-only script fetches a REAL workout from your Garmin account so those
constants can be confirmed against ground truth.

Recommended: create one strength workout by hand in Garmin Connect (a warmup set
plus two working sets with reps and weight), then dump it and compare its steps
to the constants in ``src/hevy2garmin/routine.py``.

Credentials come from a ``.env`` file (GARMIN_EMAIL / GARMIN_PASSWORD, and
optionally DATABASE_URL to reuse the same Postgres token store as your
deployment). See ``.env.example``. Real environment variables take precedence
over the file.

Usage:

    PYTHONPATH=src python scripts/dump_garmin_workout.py --list
    PYTHONPATH=src python scripts/dump_garmin_workout.py --id 123456789
    PYTHONPATH=src python scripts/dump_garmin_workout.py --id 123456789 -o out.json
    PYTHONPATH=src python scripts/dump_garmin_workout.py --env path/to/.env --list

Nothing is created, scheduled, or deleted — only GET requests are made.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from hevy2garmin.config import load_config
from hevy2garmin.garmin import get_client

# Repo root = parent of this script's directory, so `.env` resolves whether the
# script is run from the repo root or elsewhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: str | None) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    A tiny parser so the script needs no python-dotenv dependency. Skips blank
    lines and comments, strips optional surrounding quotes, and never overrides
    a variable already set in the real environment.
    """
    candidates = [Path(path)] if path else [Path.cwd() / ".env", _REPO_ROOT / ".env"]
    env_file = next((p for p in candidates if p.is_file()), None)
    if env_file is None:
        if path:
            sys.exit(f"env file not found: {path}")
        return
    print(f"Loading env from {env_file}", file=sys.stderr)
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _client():
    cfg = load_config()
    return get_client(
        cfg.get("garmin_email"),
        cfg.get("garmin_password", ""),
        cfg.get("garmin_token_dir", "~/.garminconnect"),
    )


def cmd_list(client) -> None:
    workouts = client.get_workouts() or []
    if not workouts:
        print("No saved workouts found in this Garmin account.")
        return
    print(f"{'workoutId':>14}  name")
    print("-" * 40)
    for w in workouts:
        sport = (w.get("sportType") or {}).get("sportTypeKey", "?")
        print(f"{str(w.get('workoutId', '?')):>14}  {w.get('workoutName', '?')}  [{sport}]")


def cmd_dump(client, workout_id: str, out_path: str | None) -> None:
    data = client.get_workout_by_id(workout_id)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Wrote {len(text)} bytes to {out_path}")
    print(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List saved workouts (id + name)")
    parser.add_argument("--id", help="workoutId to dump as JSON")
    parser.add_argument("-o", "--out", help="Also write the JSON to this file")
    parser.add_argument("--env", help="Path to a .env file (default: ./.env or repo root)")
    args = parser.parse_args()

    if not args.list and not args.id:
        parser.error("pass --list or --id <workoutId>")

    _load_dotenv(args.env)
    print("Authenticating with Garmin Connect...", file=sys.stderr)
    client = _client()

    if args.list:
        cmd_list(client)
    if args.id:
        cmd_dump(client, args.id, args.out)


if __name__ == "__main__":
    main()
