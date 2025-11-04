import os
import random
import secrets
import psycopg2
import psycopg2.extras
from collections import defaultdict
from functools import wraps
from datetime import datetime, timezone

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

# PostgreSQL connection string
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable must be set. Set it to your PostgreSQL connection string."
    )

# Fix Render's postgres:// to postgresql:// for psycopg2
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD environment variable must be set")
MAX_WEEKS = 8
MAX_GAMES = 3
ADMIN_SESSION_TIMEOUT_MINUTES = (
    30  # Admin session expires after 30 minutes of inactivity
)
SETTINGS_CURRENT_WEEK = "current_week"
SETTINGS_CURRENT_PLAYOFF_ROUND = "current_playoff_round"
WEEKDAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
SLOT_MINUTES = 30
SLOTS_PER_DAY = (24 * 60) // SLOT_MINUTES
DAY_START_HOUR = 8
DAY_END_HOUR = 18
VISIBLE_START_SLOT = (DAY_START_HOUR * 60) // SLOT_MINUTES
VISIBLE_END_SLOT = (DAY_END_HOUR * 60) // SLOT_MINUTES
PLAYER_APPROVAL_QUERY = (
    "SELECT id, first_name, last_name, approved FROM players WHERE id = %s"
)
PLAYER_APPROVAL_QUERY = (
    "SELECT id, first_name, last_name, approved FROM players WHERE id = %s"
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY environment variable must be set")

# Security settings for production
app.config["SESSION_COOKIE_SECURE"] = True  # Only send cookie over HTTPS
app.config["SESSION_COOKIE_HTTPONLY"] = (
    True  # Prevent JavaScript access to session cookie
)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # CSRF protection


class PostgreSQLWrapper:
    """Wrapper to make PostgreSQL connection behave like SQLite for easier migration"""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, params=None):
        cursor = self.conn.cursor()
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        return cursor

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def get_db():
    if "db" not in g:
        try:
            conn = psycopg2.connect(
                DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
            )
            g.db = PostgreSQLWrapper(conn)
        except psycopg2.Error as e:
            print(f"Database connection error: {e}")
            raise RuntimeError(
                f"Unable to connect to database. Please contact admin. Error: {e}"
            )
    return g.db


def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


app.teardown_appcontext(close_db)


def init_db():
    db = get_db()

    # PostgreSQL doesn't need foreign key pragma

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS players (
            id SERIAL PRIMARY KEY,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            cec_id TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """
    )
    ensure_player_columns(db)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS matches (
            id SERIAL PRIMARY KEY,
            week INTEGER,
            player1_id INTEGER,
            player2_id INTEGER,
            game1_p1 INTEGER DEFAULT 0,
            game1_p2 INTEGER DEFAULT 0,
            game2_p1 INTEGER DEFAULT 0,
            game2_p2 INTEGER DEFAULT 0,
            game3_p1 INTEGER DEFAULT 0,
            game3_p2 INTEGER DEFAULT 0,
            game1_score1 INTEGER,
            game1_score2 INTEGER,
            game2_score1 INTEGER,
            game2_score2 INTEGER,
            game3_score1 INTEGER,
            game3_score2 INTEGER,
            score1 INTEGER,
            score2 INTEGER,
            reported INTEGER DEFAULT 0,
            double_forfeit INTEGER DEFAULT 0,
            playoff INTEGER DEFAULT 0,
            playoff_round INTEGER,
            round_name TEXT,
            match_number INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (player1_id) REFERENCES players (id) ON DELETE CASCADE,
            FOREIGN KEY (player2_id) REFERENCES players (id) ON DELETE CASCADE
        )
    """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """
    )

    # Create indexes for better query performance
    db.execute("CREATE INDEX IF NOT EXISTS idx_matches_week ON matches(week)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_matches_playoff ON matches(playoff)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_matches_reported ON matches(reported)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_matches_players ON matches(player1_id, player2_id)"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS match_schedules (
            id SERIAL PRIMARY KEY,
            match_id INTEGER UNIQUE,
            week INTEGER NOT NULL,
            weekday INTEGER NOT NULL,
            slot_start INTEGER NOT NULL,
            slot_end INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (match_id) REFERENCES matches (id) ON DELETE CASCADE
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_match_schedules_week_day ON match_schedules(week, weekday, slot_start)"
    )

    # Ensure all columns exist (for schema migrations)
    ensure_match_columns(db)
    ensure_default_settings(db)
    prune_old_schedules(db)

    db.commit()


@app.before_request
def ensure_db_ready():
    init_db()
    # Manual playoff progression: admin will control when playoffs start and advance
    # auto_start_playoffs_if_ready(db)
    # advance_playoff_winners(db)


def ensure_player_columns(db):
    """Ensure player-related schema changes are applied."""
    cursor = db.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'players'
        """
    )
    existing = {row["column_name"] for row in cursor.fetchall()}

    if "approved" not in existing:
        try:
            db.execute("ALTER TABLE players ADD COLUMN approved BOOLEAN")
            db.execute("UPDATE players SET approved = TRUE")
            db.execute("ALTER TABLE players ALTER COLUMN approved SET DEFAULT FALSE")
            db.execute("ALTER TABLE players ALTER COLUMN approved SET NOT NULL")
            print("✅ Added column: approved (default FALSE)")
        except Exception as exc:
            print(f"⚠️ Could not ensure players.approved column: {exc}")

    if "approved_at" not in existing:
        try:
            db.execute("ALTER TABLE players ADD COLUMN approved_at TIMESTAMP")
            print("✅ Added column: approved_at")
        except Exception as exc:
            print(f"⚠️ Could not add players.approved_at column: {exc}")

    db.commit()


def ensure_match_columns(db):
    """Migrate old database schemas to include all necessary columns."""
    # PostgreSQL way to check existing columns
    cursor = db.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'matches'
    """)
    existing = {row["column_name"] for row in cursor.fetchall()}

    # Define all columns that might be missing from older schemas
    definitions = {
        "game1_score1": "INTEGER",
        "game1_score2": "INTEGER",
        "game2_score1": "INTEGER",
        "game2_score2": "INTEGER",
        "game3_score1": "INTEGER",
        "game3_score2": "INTEGER",
        "score1": "INTEGER",
        "score2": "INTEGER",
        "double_forfeit": "INTEGER DEFAULT 0",
        "playoff_round": "INTEGER",
    }

    for column, ddl in definitions.items():
        if column not in existing:
            try:
                db.execute(f"ALTER TABLE matches ADD COLUMN {column} {ddl}")
                print(f"✅ Added column: {column}")
            except Exception as e:
                print(f"⚠️ Could not add column {column}: {e}")

    db.commit()


def ensure_default_settings(db):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
        (SETTINGS_CURRENT_WEEK, "1"),
    )
    db.execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
        (SETTINGS_CURRENT_PLAYOFF_ROUND, "-1"),
    )


def prune_old_schedules(db):
    current_week = get_current_week(db)
    try:
        db.execute(
            "DELETE FROM match_schedules WHERE week < %s",
            (current_week,),
        )
    except Exception as exc:
        print(f"⚠️ Could not prune old schedule entries: {exc}")


def slot_to_label(slot_index):
    if slot_index < 0 or slot_index > SLOTS_PER_DAY:
        raise ValueError(f"Slot index out of range: {slot_index}")
    if slot_index == SLOTS_PER_DAY:
        return "12:00 AM"
    hour, remainder = divmod(slot_index, 2)
    minutes = 30 if remainder else 0
    hour_mod = hour % 12
    hour_display = 12 if hour_mod == 0 else hour_mod
    suffix = "AM" if hour < 12 or hour == 24 else "PM"
    return f"{hour_display}:{minutes:02d} {suffix}"


def summarize_schedule_entry(entry):
    day_label = WEEKDAY_LABELS[entry["weekday"]]
    start_label = slot_to_label(entry["slot_start"])
    end_label = slot_to_label(entry["slot_end"])
    return f"{day_label} {start_label} - {end_label}"


def fetch_week_schedule(db, week):
    rows = db.execute(
        """
        SELECT ms.match_id,
               ms.week,
               ms.weekday,
               ms.slot_start,
               ms.slot_end,
               ms.created_at,
               p1.first_name || ' ' || p1.last_name AS player1_name,
               p2.first_name || ' ' || p2.last_name AS player2_name
        FROM match_schedules ms
        JOIN matches m ON m.id = ms.match_id
        LEFT JOIN players p1 ON p1.id = m.player1_id
        LEFT JOIN players p2 ON p2.id = m.player2_id
        WHERE ms.week = %s
        ORDER BY ms.weekday ASC, ms.slot_start ASC
        """,
        (week,),
    ).fetchall()

    entries = []
    for row in rows:
        entry = dict(row)
        entry["weekday_label"] = WEEKDAY_LABELS[entry["weekday"]]
        entry["start_label"] = slot_to_label(entry["slot_start"])
        entry["end_label"] = slot_to_label(entry["slot_end"])
        entry["summary"] = summarize_schedule_entry(entry)
        entry["duration_slots"] = entry["slot_end"] - entry["slot_start"]
        entry["duration_minutes"] = entry["duration_slots"] * SLOT_MINUTES
        entries.append(entry)
    return entries


def build_calendar_grid(
    schedule_entries,
    target_match_id,
    start_slot=VISIBLE_START_SLOT,
    end_slot=VISIBLE_END_SLOT,
):
    by_day = defaultdict(list)
    for entry in schedule_entries:
        by_day[entry["weekday"]].append(entry)
    for entries in by_day.values():
        entries.sort(key=lambda item: item["slot_start"])

    grid_rows = []
    for slot_index in range(start_slot, end_slot):
        cells = []
        for weekday in range(len(WEEKDAY_LABELS)):
            occupant = next(
                (
                    entry
                    for entry in by_day.get(weekday, [])
                    if entry["slot_start"] <= slot_index < entry["slot_end"]
                ),
                None,
            )
            starts_here = bool(
                occupant is not None and slot_index == occupant["slot_start"]
            )
            cells.append(
                {
                    "weekday": weekday,
                    "occupied": occupant is not None,
                    "starts": starts_here,
                    "is_current": occupant is not None
                    and occupant["match_id"] == target_match_id,
                    "entry": occupant,
                }
            )
        grid_rows.append(
            {
                "slot": slot_index,
                "time_label": slot_to_label(slot_index),
                "end_label": slot_to_label(min(slot_index + 1, SLOTS_PER_DAY)),
                "cells": cells,
            }
        )
    return grid_rows


def player_has_week_match(db, player_id, week, exclude_match_id=None):
    if player_id is None or week is None:
        return False
    params = [week, player_id, player_id]
    query = (
        "SELECT id FROM matches "
        "WHERE playoff = 0 AND week = %s AND (player1_id = %s OR player2_id = %s)"
    )
    if exclude_match_id is not None:
        query += " AND id <> %s"
        params.append(exclude_match_id)
    conflict = db.execute(query, tuple(params)).fetchone()
    return conflict is not None


def matchup_already_exists(db, player1_id, player2_id, exclude_match_id=None):
    if not player1_id or not player2_id:
        return False
    params = [player1_id, player2_id, player2_id, player1_id]
    query = (
        "SELECT id FROM matches WHERE playoff = 0 "
        "AND ((player1_id = %s AND player2_id = %s) OR (player1_id = %s AND player2_id = %s))"
    )
    if exclude_match_id is not None:
        query += " AND id <> %s"
        params.append(exclude_match_id)
    conflict = db.execute(query, tuple(params)).fetchone()
    return conflict is not None


def format_player_name(player_row):
    if not player_row:
        return "Unknown Player"
    return f"{player_row['first_name']} {player_row['last_name']}"


def _valid_opponents_for(player, candidates, used_pairs):
    options = []
    for candidate in candidates:
        if player is None and candidate is None:
            continue
        if player is None or candidate is None:
            options.append(candidate)
            continue
        pair_key = frozenset({player, candidate})
        if pair_key not in used_pairs:
            options.append(candidate)
    return options


def _select_player_with_fewest_options(available, used_pairs):
    best_player = None
    best_options = None
    for player in available:
        remaining = [candidate for candidate in available if candidate is not player]
        options = _valid_opponents_for(player, remaining, used_pairs)
        if not options:
            return player, []
        if best_options is None or len(options) < len(best_options):
            best_player = player
            best_options = options
            if len(best_options) == 1:
                break
    return best_player, list(best_options or [])


def _build_week_matching(players, used_pairs, max_backtracks=500):
    participants = list(players)
    if len(participants) % 2 != 0:
        participants.append(None)

    attempts = 0
    while attempts < max_backtracks:
        attempts += 1
        shuffled = participants[:]
        random.shuffle(shuffled)
        result = _match_week(tuple(shuffled), used_pairs, [])
        if result is not None:
            return result
    return None


def _match_week(available, used_pairs, matches):
    if not available:
        return matches

    player, options = _select_player_with_fewest_options(list(available), used_pairs)
    if not options:
        return None

    remaining = list(available)
    remaining.remove(player)
    random.shuffle(options)

    for opponent in options:
        next_remaining = remaining[:]
        next_remaining.remove(opponent)
        updated_used = set(used_pairs)
        if player is not None and opponent is not None:
            updated_used.add(frozenset({player, opponent}))
        updated_matches = matches + [(player, opponent)]
        attempt = _match_week(tuple(next_remaining), updated_used, updated_matches)
        if attempt is not None:
            return attempt
    return None


def generate_reseeded_weeks(player_ids, existing_pairs, weeks_needed, max_attempts=200):
    if weeks_needed <= 0:
        return []
    roster = list(player_ids)
    if len(roster) < 2:
        return [[] for _ in range(weeks_needed)]

    for _ in range(max_attempts):
        used_pairs = set(existing_pairs)
        schedule = []
        success = True
        random.shuffle(roster)
        for _ in range(weeks_needed):
            week_matches = _build_week_matching(roster, used_pairs)
            if week_matches is None:
                success = False
                break
            schedule.append(week_matches)
            for player1, player2 in week_matches:
                if player1 is not None and player2 is not None:
                    used_pairs.add(frozenset({player1, player2}))
        if success:
            return schedule
    return None


def get_current_week(db):
    row = db.execute(
        "SELECT value FROM settings WHERE key = %s", (SETTINGS_CURRENT_WEEK,)
    ).fetchone()
    if not row:
        return 1
    try:
        return max(1, int(row["value"]))
    except (TypeError, ValueError):
        return 1


def set_current_week(db, week):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (SETTINGS_CURRENT_WEEK, str(week)),
    )


def get_current_playoff_round(db):
    row = db.execute(
        "SELECT value FROM settings WHERE key = %s",
        (SETTINGS_CURRENT_PLAYOFF_ROUND,),
    ).fetchone()
    if not row:
        return -1
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return -1


def set_current_playoff_round(db, round_number):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (SETTINGS_CURRENT_PLAYOFF_ROUND, str(round_number)),
    )


def get_value(mapping, key):
    if isinstance(mapping, dict):
        return mapping.get(key)
    return mapping[key]


def extract_game_scores(match_row):
    scores = []
    for idx in range(1, MAX_GAMES + 1):
        score1 = get_value(match_row, f"game{idx}_score1")
        score2 = get_value(match_row, f"game{idx}_score2")
        if score1 is None or score2 is None:
            continue
        scores.append((score1, score2))
    return scores


def aggregate_match_points(match_row):
    scores = extract_game_scores(match_row)
    if scores:
        total1 = sum(score[0] for score in scores)
        total2 = sum(score[1] for score in scores)
        return total1, total2
    score1 = get_value(match_row, "score1")
    score2 = get_value(match_row, "score2")
    return score1 or 0, score2 or 0


def calculate_game_wins(scores):
    wins1 = 0
    wins2 = 0
    for score1, score2 in scores:
        if score1 > score2:
            wins1 += 1
        elif score2 > score1:
            wins2 += 1
    return wins1, wins2


def format_match_summary(match_row):
    if get_value(match_row, "player2_id") is None:
        return "Bye"
    if get_value(match_row, "double_forfeit"):
        return "Double Forfeit"
    if not get_value(match_row, "reported"):
        return None
    scores = extract_game_scores(match_row)
    total1, total2 = aggregate_match_points(match_row)
    if not scores:
        if total1 is None or total2 is None:
            return None
        return f"{total1} - {total2}"
    wins1, wins2 = calculate_game_wins(scores)
    details = ", ".join(f"{s1}-{s2}" for s1, s2 in scores)
    return f"{wins1}-{wins2} ({details})"


def build_match_view(match_row, current_week=None, current_playoff_round=None, is_admin=False):
    if isinstance(match_row, dict):
        data = dict(match_row)
    else:
        data = {key: match_row[key] for key in match_row.keys()}
    data["is_bye"] = data.get("player2_id") is None
    data["double_forfeit"] = bool(get_value(match_row, "double_forfeit"))
    data["score_summary"] = format_match_summary(match_row)
    data["game_scores"] = extract_game_scores(match_row)
    totals = aggregate_match_points(match_row)
    data["total_score1"], data["total_score2"] = totals
    is_playoff_match = bool(data.get("playoff"))
    
    # Admin can edit any completed match (reported matches that aren't byes)
    data["admin_can_edit"] = (
        is_admin 
        and not data["is_bye"] 
        and data.get("reported", 0) == 1
        and data.get("player2_id") is not None
    )
    
    if is_playoff_match:
        if current_playoff_round is None:
            current_playoff_round = -1
        data["can_report"] = (
            current_playoff_round >= 0
            and data.get("playoff_round") == current_playoff_round
            and not data["is_bye"]
            and not data["double_forfeit"]
            and data.get("player2_id") is not None
        )
    elif current_week is not None:
        week_value = data.get("week")
        if data["is_bye"] or data["double_forfeit"]:
            data["can_report"] = False
        elif week_value is None:
            data["can_report"] = False
        else:
            data["can_report"] = week_value == current_week
    else:
        data["can_report"] = not data["is_bye"] and not data["double_forfeit"]
    return data


def normalize_pair(player_a, player_b):
    if player_a is None and player_b is None:
        return None, None
    if player_a is None:
        return player_b, None
    if player_b is None:
        return player_a, None
    return player_a, player_b


def label_for_round(round_number, num_matches_in_round=None):
    """
    Convert round number to human-readable tournament round name.

    Args:
        round_number: The round number (0 for play-ins, 1+ for main bracket)
        num_matches_in_round: Number of matches in this specific round (optional)
                             If provided, names based on teams remaining

    Returns:
        Human-readable round name (e.g., "Play-in Round", "Quarterfinals", "Finals")

    Round naming based on teams remaining (num_matches * 2):
    - 1 match (2 teams) = Finals
    - 2 matches (4 teams) = Semifinals
    - 4 matches (8 teams) = Quarterfinals
    - 8 matches (16 teams) = Round of 16
    - 16 matches (32 teams) = Round of 32
    """
    # Handle play-in round (round 0)
    if round_number == 0:
        return "Play-in Round"

    if num_matches_in_round:
        # Name based on number of teams in this round
        # Teams in round = matches * 2
        if num_matches_in_round == 1:
            return "Finals"
        elif num_matches_in_round == 2:
            return "Semifinals"
        elif num_matches_in_round == 4:
            return "Quarterfinals"
        elif num_matches_in_round == 8:
            return "Round of 16"
        elif num_matches_in_round == 16:
            return "Round of 32"
        elif num_matches_in_round == 32:
            return "Round of 64"
        else:
            # For non-standard sizes, just use "First Round", "Second Round", etc.
            if round_number == 1:
                return "First Round"
            return f"Round {round_number}"

    # Fallback for backward compatibility (static naming for ~16 player bracket)
    if round_number == 1:
        return "First Round"
    if round_number == 2:
        return "Round of 16"
    if round_number == 3:
        return "Quarterfinals"
    if round_number == 4:
        return "Semifinals"
    if round_number == 5:
        return "Finals"
    return f"Round {round_number}"


def generate_play_in_matches_metadata(
    seeds, target_bracket_size, num_byes, player_ranks
):
    """Return ordered metadata for play-in matches keyed by the seed that advances."""
    num_players = len(seeds)
    num_play_in_games = num_players - target_bracket_size
    if num_play_in_games <= 0:
        return []

    play_in_seed_numbers = list(range(num_byes + 1, target_bracket_size + 1))
    opponent_seed_numbers = list(range(target_bracket_size + 1, num_players + 1))[::-1]

    matches = []
    for seed_no, opponent_seed_no in zip(play_in_seed_numbers, opponent_seed_numbers):
        player1_id = seeds[seed_no - 1]
        player2_id = seeds[opponent_seed_no - 1]
        matches.append(
            {
                "target_seed": seed_no,
                "player1_id": player1_id,
                "player2_id": player2_id,
                "player1_rank": player_ranks.get(player1_id, seed_no),
                "player2_rank": player_ranks.get(player2_id, opponent_seed_no),
            }
        )

    matches.sort(key=lambda item: item["target_seed"], reverse=True)
    return matches


def parse_best_of_three_scores(form):
    scores = []
    for idx in range(1, MAX_GAMES + 1):
        raw1 = form.get(f"game{idx}_score1", "").strip()
        raw2 = form.get(f"game{idx}_score2", "").strip()
        if not raw1 and not raw2:
            continue
        if not raw1 or not raw2:
            raise ValueError(f"Please provide both scores for Game {idx}.")
        try:
            score1 = int(raw1)
            score2 = int(raw2)
        except ValueError:
            raise ValueError("Scores must be integers.")
        if score1 < 0 or score2 < 0:
            raise ValueError("Scores cannot be negative.")
        if score1 > 99 or score2 > 99:
            raise ValueError(
                f"Unrealistic score in Game {idx}. Scores should be under 100. If this is correct, admin can manually edit."
            )
        if score1 == score2:
            raise ValueError("Games cannot end in a tie.")
        scores.append((score1, score2))
    return scores


def validate_best_of_three(scores):
    if len(scores) < 2:
        raise ValueError("Best-of-three requires at least two completed games.")
    wins1, wins2 = calculate_game_wins(scores)
    if wins1 > 2 or wins2 > 2:
        raise ValueError("Best-of-three only allows up to two wins per player.")
    if wins1 == wins2:
        raise ValueError("Please enter a decisive match winner.")
    if wins1 < 2 and wins2 < 2:
        raise ValueError("Winner must reach two game wins in best-of-three play.")
    if wins1 >= 2 and wins2 >= 2:
        raise ValueError("Only one player can win two games in best-of-three.")
    return wins1, wins2


def _normalize_round_matches(round_info):
    matches = round_info.get("matches", [])
    round_number = round_info.get("round_number", 0)

    if round_number == 0:
        matches.sort(
            key=lambda match: (
                -(match.get("match_number") or 0),
                match.get("id"),
            )
        )
    else:
        matches.sort(key=lambda match: (match.get("match_number") or match.get("id"),))

    for idx, match in enumerate(matches, start=1):
        match["display_index"] = idx
        match.setdefault("match_number", idx)

    round_info["match_count"] = len(matches)


def _compute_next_display_index(round_info, next_round, match):
    if not next_round:
        return None

    if round_info.get("round_number") == 0:
        bracket_size = max(next_round.get("match_count", 0) * 2, 0)
        target_seed = match.get("match_number") or match.get("display_index")
        if not (bracket_size and target_seed):
            return None
        if target_seed <= bracket_size // 2:
            target_index = target_seed
        else:
            target_index = bracket_size + 1 - target_seed
        if target_index <= next_round.get("match_count", 0):
            return target_index
        return None

    target_index = (match.get("display_index", 0) + 1) // 2
    if target_index and target_index <= next_round.get("match_count", 0):
        return target_index
    return None


def _link_rounds(rounds_sorted):
    for idx, round_info in enumerate(rounds_sorted):
        next_round = rounds_sorted[idx + 1] if idx + 1 < len(rounds_sorted) else None
        for match in round_info.get("matches", []):
            match["next_round_number"] = (
                next_round.get("round_number") if next_round else None
            )
            match["next_display_index"] = _compute_next_display_index(
                round_info, next_round, match
            )


def finalize_bracket_rounds(rounds):
    """Normalize bracket rounds with consistent ordering and connector metadata."""
    if not rounds:
        return []

    rounds_sorted = sorted(
        rounds, key=lambda round_info: round_info.get("round_number", 0)
    )

    for round_info in rounds_sorted:
        _normalize_round_matches(round_info)

    _link_rounds(rounds_sorted)

    return rounds_sorted


def build_playoff_preview(db):
    """
    Build playoff preview showing ONLY the first round (play-ins + Round 1).
    Does not simulate future rounds - those will show TBD until matches are played.
    """
    rankings = calculate_rankings(db, include_playoffs=False)
    if len(rankings) < 2:
        return []

    # Create a mapping of player_id to rank
    player_ranks = {row["player_id"]: row["rank"] for row in rankings}
    names = {
        row["player_id"]: f"{row['first_name']} {row['last_name']}" for row in rankings
    }
    seeds = [row["player_id"] for row in rankings]
    num_players = len(seeds)
    round_entries = []

    if num_players & (num_players - 1) == 0:
        pair_count = num_players // 2
        matches = []

        for slot in range(pair_count):
            p1 = seeds[slot]
            p2 = seeds[-(slot + 1)]
            rank1 = player_ranks.get(p1, "?")
            rank2 = player_ranks.get(p2, "?")

            matches.append(
                {
                    "id": f"preview-r1-{slot}",
                    "player1_id": p1,
                    "player2_id": p2,
                    "player1_name": f"{rank1}. {names.get(p1, 'TBD')}",
                    "player2_name": f"{rank2}. {names.get(p2, 'TBD')}",
                    "is_bye": False,
                    "reported": False,
                    "score_summary": None,
                    "can_report": False,
                    "double_forfeit": False,
                    "total_score1": 0,
                    "total_score2": 0,
                    "playoff_round": 1,
                    "match_number": slot + 1,
                }
            )

        round_entries.append(
            {
                "round_number": 1,
                "label": label_for_round(1, pair_count),
                "matches": matches,
            }
        )

        current_round_teams = pair_count
        current_round_number = 2

        while current_round_teams > 1:
            next_round_matches = current_round_teams // 2
            placeholders = []

            for i in range(next_round_matches):
                placeholders.append(
                    {
                        "id": f"preview-r{current_round_number}-{i}",
                        "player1_id": None,
                        "player2_id": None,
                        "player1_name": "TBD",
                        "player2_name": "TBD",
                        "is_bye": False,
                        "reported": False,
                        "score_summary": None,
                        "can_report": False,
                        "double_forfeit": False,
                        "total_score1": 0,
                        "total_score2": 0,
                        "playoff_round": current_round_number,
                        "match_number": i + 1,
                    }
                )

            round_entries.append(
                {
                    "round_number": current_round_number,
                    "label": label_for_round(current_round_number, next_round_matches),
                    "matches": placeholders,
                }
            )

            current_round_teams = next_round_matches
            current_round_number += 1

    else:
        target_bracket_size = 1 << (num_players.bit_length() - 1)
        num_play_in_games = num_players - target_bracket_size
        num_byes = target_bracket_size - num_play_in_games

        play_in_matches_meta = generate_play_in_matches_metadata(
            seeds, target_bracket_size, num_byes, player_ranks
        )
        seed_to_play_in = {
            match_info["target_seed"]: match_info for match_info in play_in_matches_meta
        }

        play_in_matches = []
        for idx, match_meta in enumerate(play_in_matches_meta):
            p1 = match_meta["player1_id"]
            p2 = match_meta["player2_id"]
            rank1 = match_meta["player1_rank"]
            rank2 = match_meta["player2_rank"]

            play_in_matches.append(
                {
                    "id": f"preview-r0-{idx}",
                    "player1_id": p1,
                    "player2_id": p2,
                    "player1_name": f"{rank1}. {names.get(p1, 'TBD')}",
                    "player2_name": f"{rank2}. {names.get(p2, 'TBD')}",
                    "is_bye": False,
                    "reported": False,
                    "score_summary": None,
                    "can_report": False,
                    "double_forfeit": False,
                    "total_score1": 0,
                    "total_score2": 0,
                    "playoff_round": 0,
                    "match_number": match_meta["target_seed"],
                }
            )

        if play_in_matches:
            round_entries.append(
                {
                    "round_number": 0,
                    "label": label_for_round(0, len(play_in_matches)),
                    "matches": play_in_matches,
                }
            )

        round_one_matches = []
        main_bracket_first_round_matches = target_bracket_size // 2

        for match_index in range(1, main_bracket_first_round_matches + 1):
            seed1 = match_index
            seed2 = target_bracket_size + 1 - match_index

            match_info_1 = seed_to_play_in.get(seed1)
            match_info_2 = seed_to_play_in.get(seed2)

            player1_id = seeds[seed1 - 1] if seed1 <= num_byes else None
            player2_id = seeds[seed2 - 1] if seed2 <= num_byes else None

            if seed1 <= num_byes:
                player1_name = f"{seed1}. {names.get(player1_id, 'TBD')}"
            elif match_info_1:
                player1_name = f"Winner of Play-in ({match_info_1['player1_rank']} vs {match_info_1['player2_rank']})"
            else:
                player1_name = "TBD (Play-in Winner)"

            if seed2 <= num_byes:
                player2_name = f"{seed2}. {names.get(player2_id, 'TBD')}"
            elif match_info_2:
                player2_name = f"Winner of Play-in ({match_info_2['player1_rank']} vs {match_info_2['player2_rank']})"
            else:
                player2_name = "TBD (Play-in Winner)"

            round_one_matches.append(
                {
                    "id": f"preview-r1-{match_index - 1}",
                    "player1_id": player1_id,
                    "player2_id": player2_id,
                    "player1_name": player1_name,
                    "player2_name": player2_name,
                    "is_bye": False,
                    "reported": False,
                    "score_summary": None,
                    "can_report": False,
                    "double_forfeit": False,
                    "total_score1": 0,
                    "total_score2": 0,
                    "playoff_round": 1,
                    "match_number": match_index,
                }
            )

        round_entries.append(
            {
                "round_number": 1,
                "label": label_for_round(1, main_bracket_first_round_matches),
                "matches": round_one_matches,
            }
        )

        current_round_teams = main_bracket_first_round_matches
        current_round_number = 2

        while current_round_teams > 1:
            next_round_matches = current_round_teams // 2
            placeholders = []

            for i in range(next_round_matches):
                placeholders.append(
                    {
                        "id": f"preview-r{current_round_number}-{i}",
                        "player1_id": None,
                        "player2_id": None,
                        "player1_name": "TBD",
                        "player2_name": "TBD",
                        "is_bye": False,
                        "reported": False,
                        "score_summary": None,
                        "can_report": False,
                        "double_forfeit": False,
                        "total_score1": 0,
                        "total_score2": 0,
                        "playoff_round": current_round_number,
                        "match_number": i + 1,
                    }
                )

            round_entries.append(
                {
                    "round_number": current_round_number,
                    "label": label_for_round(current_round_number, next_round_matches),
                    "matches": placeholders,
                }
            )

            current_round_teams = next_round_matches
            current_round_number += 1

    return finalize_bracket_rounds(round_entries)


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_authenticated"):
            flash("Admin login required.", "error")
            return redirect(url_for("admin_login", next=request.path))

        # Check session timeout
        last_activity = session.get("admin_last_activity")
        if last_activity:
            try:
                last_time = datetime.fromisoformat(last_activity)
                time_diff = datetime.now(timezone.utc) - last_time
                if time_diff.total_seconds() > (ADMIN_SESSION_TIMEOUT_MINUTES * 60):
                    session.clear()
                    flash(
                        f"Admin session expired after {ADMIN_SESSION_TIMEOUT_MINUTES} minutes of inactivity. Please login again.",
                        "error",
                    )
                    return redirect(url_for("admin_login", next=request.path))
            except (ValueError, TypeError):
                pass

        # Update last activity time
        session["admin_last_activity"] = datetime.now(timezone.utc).isoformat()

        return view(*args, **kwargs)

    return wrapped_view


@app.route("/")
def index():
    db = get_db()
    current_week = get_current_week(db)

    rows = db.execute(
        """
        SELECT m.id, m.player1_id, m.player2_id, m.score1, m.score2, m.reported,
               m.game1_score1, m.game1_score2,
               m.game2_score1, m.game2_score2,
               m.game3_score1, m.game3_score2,
               m.week,
               m.double_forfeit,
               p1.first_name || ' ' || p1.last_name AS player1_name,
               p2.first_name || ' ' || p2.last_name AS player2_name
        FROM matches m
        LEFT JOIN players p1 ON p1.id = m.player1_id
        LEFT JOIN players p2 ON p2.id = m.player2_id
        WHERE m.playoff = 0 AND m.week = %s
        ORDER BY m.id ASC
        """,
        (current_week,),
    ).fetchall()

    matches = [build_match_view(row, current_week=current_week) for row in rows]
    schedule_entries = fetch_week_schedule(db, current_week)
    schedule_by_match = {entry["match_id"]: entry for entry in schedule_entries}
    for match in matches:
        entry = schedule_by_match.get(match["id"])
        if entry:
            match["schedule_summary"] = entry["summary"]
            match["schedule_weekday_label"] = entry["weekday_label"]
            match["schedule_start_label"] = entry["start_label"]
            match["schedule_end_label"] = entry["end_label"]

    active_matches = [match for match in matches if not match["is_bye"]]
    calendar_rows = build_calendar_grid(schedule_entries, target_match_id=None)

    return render_template(
        "index.html",
        current_week=current_week,
        matches=matches,
        active_matches=active_matches,
        schedule_entries=schedule_entries,
        calendar_rows=calendar_rows,
        weekday_labels=WEEKDAY_LABELS,
    )


@app.route("/standings")
def standings():
    db = get_db()
    rankings = calculate_rankings(db, include_playoffs=True)
    current_week = get_current_week(db)
    return render_template(
        "standings.html",
        rankings=rankings,
        current_week=current_week,
    )


@app.route("/signup", methods=["GET", "POST"])
def signup():
    db = get_db()
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        cec_id = request.form.get("cec_id", "").strip().upper()

        if not (first_name and last_name and cec_id):
            flash("All fields are required.", "error")
            return redirect(url_for("signup"))

        exists = db.execute(
            "SELECT 1 FROM players WHERE UPPER(cec_id) = %s", (cec_id,)
        ).fetchone()
        if exists:
            flash("CEC ID already registered.", "error")
            return redirect(url_for("signup"))

        db.execute(
            "INSERT INTO players (first_name, last_name, cec_id, created_at, approved) VALUES (%s, %s, %s, %s, %s)",
            (first_name, last_name, cec_id, datetime.now(timezone.utc), False),
        )
        db.commit()
        flash(
            "Signup received! Pending admin approval before you appear on the roster.",
            "info",
        )
        return redirect(url_for("signup"))

    players = db.execute(
        "SELECT id, first_name, last_name, cec_id, created_at FROM players WHERE approved = TRUE ORDER BY created_at ASC"
    ).fetchall()
    return render_template("signup.html", players=players)


@app.route("/admin/reseed_regular_season", methods=["POST"])
@admin_required
def admin_reseed_regular_season():
    db = get_db()

    player_ids = collect_active_player_ids(db)
    if len(player_ids) < 2:
        flash("Need at least two approved players to reseed the schedule.", "error")
        return redirect(url_for("admin_dashboard"))

    if _future_regular_weeks_have_scores(db):
        flash(
            "Cannot reseed: matches in weeks 2+ already have reported scores.", "error"
        )
        return redirect(url_for("admin_dashboard"))

    players_with_week_one, existing_pairs = _load_week_one_metadata(db)
    success, message, category = _ensure_week_one_coverage(
        db, player_ids, players_with_week_one, existing_pairs
    )
    if not success:
        flash(message, category or "error")
        return redirect(url_for("admin_dashboard"))
    if message and category:
        flash(message, category)

    current_week = get_current_week(db)
    future_start = max(2, current_week + 1)
    matches_removed, schedules_removed = _clear_future_regular_weeks(db, future_start)

    if matches_removed or schedules_removed:
        flash(
            f"Cleared future weeks starting at Week {future_start}. New matchups will go live as each week begins.",
            "success",
        )
    else:
        flash(
            "Upcoming weeks were already clear. New opponents will be generated as each week starts.",
            "info",
        )
    return redirect(url_for("admin_dashboard"))


@app.route("/schedule")
def schedule_overview():
    db = get_db()
    current_week = get_current_week(db)
    return render_template(
        "schedule_overview.html",
        weeks=range(1, MAX_WEEKS + 1),
        current_week=current_week,
    )


@app.route("/schedule/<int:week>")
def view_schedule(week):
    if week < 1 or week > MAX_WEEKS:
        abort(404)
    db = get_db()
    current_week = get_current_week(db)
    rows = db.execute(
        """
        SELECT m.id, m.player1_id, m.player2_id, m.score1, m.score2, m.reported,
               m.game1_score1, m.game1_score2,
               m.game2_score1, m.game2_score2,
               m.game3_score1, m.game3_score2,
         m.week,
         m.double_forfeit,
               p1.first_name || ' ' || p1.last_name AS player1_name,
               p2.first_name || ' ' || p2.last_name AS player2_name
        FROM matches m
        LEFT JOIN players p1 ON p1.id = m.player1_id
        LEFT JOIN players p2 ON p2.id = m.player2_id
        WHERE m.playoff = 0 AND m.week = %s
        ORDER BY m.id ASC
        """,
        (week,),
    ).fetchall()
    matches = [build_match_view(row, current_week=current_week) for row in rows]
    schedule_entries = fetch_week_schedule(db, week)
    schedule_by_match = {entry["match_id"]: entry for entry in schedule_entries}
    for match in matches:
        entry = schedule_by_match.get(match["id"])
        if entry:
            match["schedule_summary"] = entry["summary"]
            match["schedule_weekday_label"] = entry["weekday_label"]
            match["schedule_start_label"] = entry["start_label"]
            match["schedule_end_label"] = entry["end_label"]
    return render_template(
        "schedule.html",
        week=week,
        matches=matches,
        current_week=current_week,
    )


@app.route(
    "/schedule/<int:week>/match/<int:match_id>/calendar", methods=["GET", "POST"]
)
def match_calendar(week, match_id):
    if week < 1 or week > MAX_WEEKS:
        abort(404)

    db = get_db()
    match_row = db.execute(
        """
        SELECT m.id,
               m.week,
               m.player1_id,
               m.player2_id,
               p1.first_name || ' ' || p1.last_name AS player1_name,
               p2.first_name || ' ' || p2.last_name AS player2_name
        FROM matches m
        LEFT JOIN players p1 ON p1.id = m.player1_id
        LEFT JOIN players p2 ON p2.id = m.player2_id
        WHERE m.id = %s AND m.week = %s AND m.playoff = 0
        """,
        (match_id, week),
    ).fetchone()

    if not match_row:
        abort(404)

    if match_row["player2_id"] is None:
        flash("Cannot schedule a bye week matchup.", "error")
        return redirect(url_for("view_schedule", week=week))

    match_info = dict(match_row)

    if request.method == "POST":
        action = request.form.get("action", "book")

        if action == "clear":
            db.execute("DELETE FROM match_schedules WHERE match_id = %s", (match_id,))
            db.commit()
            flash("Match schedule cleared.", "success")
            return redirect(url_for("match_calendar", week=week, match_id=match_id))

        try:
            weekday = int(request.form.get("weekday", ""))
            start_slot = int(request.form.get("start_slot", ""))
        except ValueError:
            flash("Please choose a valid day and time window.", "error")
            return redirect(url_for("match_calendar", week=week, match_id=match_id))

        if weekday < 0 or weekday >= len(WEEKDAY_LABELS):
            flash("Selected day is outside the Monday–Friday window.", "error")
            return redirect(url_for("match_calendar", week=week, match_id=match_id))

        if start_slot < VISIBLE_START_SLOT or start_slot >= VISIBLE_END_SLOT:
            flash("Select a time between 6:00 AM and 8:00 PM.", "error")
            return redirect(url_for("match_calendar", week=week, match_id=match_id))

        end_slot = start_slot + 1

        conflict = db.execute(
            """
            SELECT ms.match_id,
             ms.weekday,
             ms.slot_start,
             ms.slot_end,
                   p1.first_name || ' ' || p1.last_name AS player1_name,
                   p2.first_name || ' ' || p2.last_name AS player2_name
            FROM match_schedules ms
            JOIN matches m ON m.id = ms.match_id
            LEFT JOIN players p1 ON p1.id = m.player1_id
            LEFT JOIN players p2 ON p2.id = m.player2_id
            WHERE ms.week = %s
              AND ms.weekday = %s
              AND ms.match_id <> %s
              AND NOT (ms.slot_end <= %s OR ms.slot_start >= %s)
            LIMIT 1
            """,
            (week, weekday, match_id, start_slot, end_slot),
        ).fetchone()

        if conflict:
            block_summary = summarize_schedule_entry(conflict)
            occupant_name = f"{conflict['player1_name'] or 'TBD'} vs {conflict['player2_name'] or 'BYE'}"
            flash(
                f"Room already booked ({block_summary}) by {occupant_name}. Choose another slot.",
                "error",
            )
            return redirect(url_for("match_calendar", week=week, match_id=match_id))

        try:
            db.execute(
                """
                INSERT INTO match_schedules (match_id, week, weekday, slot_start, slot_end, created_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (match_id) DO UPDATE SET
                    week = EXCLUDED.week,
                    weekday = EXCLUDED.weekday,
                    slot_start = EXCLUDED.slot_start,
                    slot_end = EXCLUDED.slot_end,
                    created_at = CURRENT_TIMESTAMP
                """,
                (match_id, week, weekday, start_slot, end_slot),
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            flash(f"Could not save schedule: {exc}", "error")
            return redirect(url_for("match_calendar", week=week, match_id=match_id))

        summary = summarize_schedule_entry(
            {"weekday": weekday, "slot_start": start_slot, "slot_end": end_slot}
        )
        flash(f"Match scheduled for {summary}.", "success")
        return redirect(url_for("match_calendar", week=week, match_id=match_id))

    schedule_entries = fetch_week_schedule(db, week)
    current_entry = next(
        (entry for entry in schedule_entries if entry["match_id"] == match_id), None
    )
    calendar_grid = build_calendar_grid(schedule_entries, match_id)

    return render_template(
        "match_calendar.html",
        week=week,
        match=match_info,
        calendar_rows=calendar_grid,
        weekday_labels=WEEKDAY_LABELS,
        current_entry=current_entry,
    )


@app.route("/match/<int:match_id>", methods=["GET", "POST"])
def report_match(match_id):
    db = get_db()
    match = db.execute(
        """
        SELECT m.*, p1.first_name || ' ' || p1.last_name AS player1_name,
               p2.first_name || ' ' || p2.last_name AS player2_name
        FROM matches m
        LEFT JOIN players p1 ON p1.id = m.player1_id
        LEFT JOIN players p2 ON p2.id = m.player2_id
        WHERE m.id = %s
        """,
        (match_id,),
    ).fetchone()
    if not match:
        abort(404)
    if match["player2_id"] is None:
        flash("Byes do not require score reports.", "error")
        target_week = match.get("week") or get_current_week(db)
        return redirect(url_for("view_schedule", week=target_week))

    current_week = get_current_week(db)
    if match["playoff"]:
        current_playoff_round = get_current_playoff_round(db)
        if current_playoff_round < 0:
            flash("Playoff reporting is currently closed.", "error")
            return redirect(url_for("playoffs"))
        if match["playoff_round"] > current_playoff_round:
            flash("This playoff round is not open for reporting yet.", "error")
            return redirect(url_for("playoffs"))
        if match["player2_id"] is None:
            flash("This matchup is waiting for an opponent.", "error")
            return redirect(url_for("playoffs"))

    if match["week"] and not match["playoff"]:
        if match["week"] > current_week:
            flash("This match is not yet open for reporting.", "error")
            return redirect(url_for("view_schedule", week=match["week"]))
        if match["week"] < current_week or match["double_forfeit"]:
            flash("Reporting for this match has closed.", "error")
            return redirect(url_for("view_schedule", week=match["week"]))

    if request.method == "POST":
        try:
            game_scores = parse_best_of_three_scores(request.form)
            validate_best_of_three(game_scores)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("report_match", match_id=match_id))

        totals = (
            sum(score[0] for score in game_scores),
            sum(score[1] for score in game_scores),
        )
        padded = game_scores + [(None, None)] * (MAX_GAMES - len(game_scores))

        try:
            # Allow updates for current week matches or playoff matches
            db.execute(
                """
                UPDATE matches
                SET score1 = %s, score2 = %s,
                    game1_score1 = %s, game1_score2 = %s,
                    game2_score1 = %s, game2_score2 = %s,
                    game3_score1 = %s, game3_score2 = %s,
                    double_forfeit = 0,
                    reported = 1
                WHERE id = %s
                """,
                (
                    totals[0],
                    totals[1],
                    padded[0][0],
                    padded[0][1],
                    padded[1][0],
                    padded[1][1],
                    padded[2][0],
                    padded[2][1],
                    match_id,
                ),
            )

            db.commit()
            flash("Match score saved successfully.", "success")
        except Exception as e:
            db.rollback()
            flash(f"Error saving match: {str(e)}", "error")
            return redirect(url_for("report_match", match_id=match_id))

        if match["playoff"]:
            return redirect(url_for("playoffs"))
        if match.get("week") and not match["playoff"]:
            return redirect(url_for("view_schedule", week=match["week"]))
        return redirect(url_for("index"))

    existing_scores = extract_game_scores(match)
    padded_scores = existing_scores + [(None, None)] * (
        MAX_GAMES - len(existing_scores)
    )
    return render_template(
        "match_report.html",
        match=match,
        game_scores=padded_scores,
        max_games=MAX_GAMES,
    )


@app.route("/playoffs")
def playoffs():
    db = get_db()
    current_playoff_round = get_current_playoff_round(db)
    rows = db.execute(
        """
        SELECT m.*, p1.first_name || ' ' || p1.last_name AS player1_name,
               p2.first_name || ' ' || p2.last_name AS player2_name
        FROM matches m
        LEFT JOIN players p1 ON p1.id = m.player1_id
        LEFT JOIN players p2 ON p2.id = m.player2_id
        WHERE m.playoff = 1
        ORDER BY m.playoff_round ASC, m.id ASC
        """
    ).fetchall()
    rounds_payload = []
    actual_rounds_map = {}
    if rows:
        rankings = calculate_rankings(db, include_playoffs=False)
        player_ranks = {row["player_id"]: row["rank"] for row in rankings}

        matches_per_round = defaultdict(int)

        for match in rows:
            matches_per_round[match["playoff_round"]] += 1

        rounds_map = {}

        for match in rows:
            round_number = match["playoff_round"]
            num_matches = matches_per_round[round_number]
            round_info = rounds_map.setdefault(
                round_number,
                {
                    "round_number": round_number,
                    "label": label_for_round(round_number, num_matches),
                    "matches": [],
                },
            )

            match_view = build_match_view(
                match, current_playoff_round=current_playoff_round
            )
            match_view["match_number"] = match.get("match_number")
            match_view["id"] = match["id"]

            if match["player1_id"]:
                rank = player_ranks.get(match["player1_id"], "?")
                match_view["player1_name"] = f"{rank}. {match['player1_name']}"
            if match["player2_id"]:
                rank = player_ranks.get(match["player2_id"], "?")
                match_view["player2_name"] = f"{rank}. {match['player2_name']}"

            round_info["matches"].append(match_view)

        rounds_payload = finalize_bracket_rounds(list(rounds_map.values()))

        for round_info in rounds_payload:
            round_number = round_info["round_number"]
            for match_view in round_info["matches"]:
                if match_view.get("double_forfeit"):
                    match_view["state"] = "forfeit"
                elif match_view.get("reported"):
                    match_view["state"] = "complete"
                else:
                    if (
                        current_playoff_round >= 0
                        and round_number > current_playoff_round
                    ):
                        match_view["state"] = "future"
                    else:
                        match_view["state"] = "pending"

                key = (
                    round_number,
                    match_view.get("match_number") or match_view.get("display_index"),
                )
                if key[1] is not None:
                    actual_rounds_map[key] = match_view

    base_rounds = build_playoff_preview(db)
    has_playoffs = bool(rows)
    default_state = "preview" if not has_playoffs else "future"

    for round_info in base_rounds:
        round_number = round_info["round_number"]
        for match_view in round_info["matches"]:
            match_view.setdefault("match_number", match_view.get("display_index"))
            match_view["id"] = None
            match_view["reported"] = False
            match_view["double_forfeit"] = False
            match_view["score_summary"] = None
            match_view["game_scores"] = []
            match_view["total_score1"] = match_view.get("total_score1", 0)
            match_view["total_score2"] = match_view.get("total_score2", 0)
            match_view["can_report"] = False
            match_view["is_placeholder"] = True
            match_view["state"] = default_state

            key = (round_number, match_view.get("match_number"))
            actual_match = actual_rounds_map.get(key)
            if actual_match:
                for field in [
                    "id",
                    "player1_id",
                    "player2_id",
                    "player1_name",
                    "player2_name",
                    "is_bye",
                    "reported",
                    "double_forfeit",
                    "score_summary",
                    "game_scores",
                    "total_score1",
                    "total_score2",
                    "can_report",
                ]:
                    if field in actual_match:
                        match_view[field] = actual_match.get(field)
                match_view["state"] = actual_match.get("state", "pending")
                match_view["is_placeholder"] = False

            if not match_view.get("reported"):
                if current_playoff_round < 0 and has_playoffs:
                    match_view["state"] = "future"
                elif (
                    current_playoff_round >= 0 and round_number > current_playoff_round
                ):
                    match_view["state"] = "future"
                    match_view["can_report"] = False
                elif (
                    current_playoff_round >= 0
                    and round_number == current_playoff_round
                    and match_view.get("player1_id")
                    and match_view.get("player2_id")
                    and not match_view.get("double_forfeit")
                ):
                    # Leave state as pending for active round matchups with opponents.
                    match_view["state"] = match_view.get("state", "pending")
                elif match_view.get("state") == "pending":
                    match_view["state"] = "future"

    rounds_payload = base_rounds
    champion = get_playoff_champion(db) if has_playoffs else None
    current_round_label = None
    if has_playoffs and current_playoff_round >= 0 and rounds_payload:
        for round_info in rounds_payload:
            if round_info["round_number"] == current_playoff_round:
                current_round_label = round_info["label"]
                break
    return render_template(
        "playoffs.html",
        rounds=rounds_payload,
        preview_mode=not has_playoffs,
        has_playoffs=has_playoffs,
        champion=champion,
        current_playoff_round=current_playoff_round,
        current_round_label=current_round_label,
    )


@app.route("/player/<int:player_id>")
def player_profile(player_id):
    db = get_db()

    # Get player info
    player = db.execute(
        "SELECT id, first_name, last_name, cec_id, created_at FROM players WHERE id = %s",
        (player_id,),
    ).fetchone()

    if not player:
        abort(404)

    # Calculate individual stats
    matches = db.execute(
        """
        SELECT m.*,
               p1.first_name || ' ' || p1.last_name AS player1_name,
               p2.first_name || ' ' || p2.last_name AS player2_name
        FROM matches m
        LEFT JOIN players p1 ON p1.id = m.player1_id
        LEFT JOIN players p2 ON p2.id = m.player2_id
        WHERE (m.player1_id = %s OR m.player2_id = %s)
          AND m.reported = 1
          AND m.player2_id IS NOT NULL
        ORDER BY m.created_at DESC
        """,
        (player_id, player_id),
    ).fetchall()

    # Calculate stats
    total_wins = 0
    total_losses = 0
    total_points_scored = 0
    total_points_against = 0
    playoff_wins = 0
    playoff_losses = 0
    match_history = []

    for match in matches:
        is_player1 = match["player1_id"] == player_id
        opponent_id = match["player2_id"] if is_player1 else match["player1_id"]
        opponent_name = match["player2_name"] if is_player1 else match["player1_name"]

        if get_value(match, "double_forfeit"):
            total_losses += 1
            if match["playoff"]:
                playoff_losses += 1
            match_history.append(
                {
                    "opponent_name": opponent_name,
                    "opponent_id": opponent_id,
                    "result": "Double Forfeit",
                    "is_win": False,
                    "week": match["week"],
                    "playoff": match["playoff"],
                    "playoff_round": match.get("playoff_round"),
                    "score_summary": "0 - 0",
                }
            )
            continue

        scores = extract_game_scores(match)
        if scores:
            wins1, wins2 = calculate_game_wins(scores)
            is_win = (is_player1 and wins1 > wins2) or (
                not is_player1 and wins2 > wins1
            )

            # Count match win/loss
            if is_win:
                total_wins += 1
                if match["playoff"]:
                    playoff_wins += 1
            else:
                total_losses += 1
                if match["playoff"]:
                    playoff_losses += 1

            # Calculate points
            for score1, score2 in scores:
                if is_player1:
                    total_points_scored += score1
                    total_points_against += score2
                else:
                    total_points_scored += score2
                    total_points_against += score1

            # Build match history entry
            score_summary = format_match_summary(match)
            match_history.append(
                {
                    "opponent_name": opponent_name,
                    "opponent_id": opponent_id,
                    "result": "Win" if is_win else "Loss",
                    "is_win": is_win,
                    "week": match["week"],
                    "playoff": match["playoff"],
                    "playoff_round": match.get("playoff_round"),
                    "score_summary": score_summary,
                }
            )

    point_diff = total_points_scored - total_points_against
    win_percentage = (
        (total_wins / (total_wins + total_losses) * 100)
        if (total_wins + total_losses) > 0
        else 0
    )

    stats = {
        "total_wins": total_wins,
        "total_losses": total_losses,
        "win_percentage": round(win_percentage, 1),
        "total_points_scored": total_points_scored,
        "total_points_against": total_points_against,
        "point_diff": point_diff,
        "playoff_wins": playoff_wins,
        "playoff_losses": playoff_losses,
        "total_matches": total_wins + total_losses,
    }

    # Get current rank
    rankings = calculate_rankings(db, include_playoffs=True)
    player_rank = next(
        (r["rank"] for r in rankings if r["player_id"] == player_id), None
    )

    return render_template(
        "player_profile.html",
        player=player,
        stats=stats,
        match_history=match_history,
        rank=player_rank,
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_authenticated"):
        return redirect(url_for("admin_dashboard"))
    next_url = request.args.get("next") or request.form.get("next")
    if request.method == "POST":
        password = request.form.get("password", "")
        # Use constant-time comparison to prevent timing attacks
        # Ensure both are strings to avoid type issues
        if (
            password
            and ADMIN_PASSWORD
            and secrets.compare_digest(password, ADMIN_PASSWORD)
        ):
            session["admin_authenticated"] = True
            session["admin_last_activity"] = datetime.now(timezone.utc).isoformat()
            flash("Logged in as admin.", "success")
            return redirect(next_url or url_for("admin_dashboard"))
        flash("Invalid password.", "error")
    return render_template("admin_login.html", next_url=next_url)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_authenticated", None)
    flash("Logged out", "success")
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    current_week = get_current_week(db)
    current_playoff_round = get_current_playoff_round(db)
    players = db.execute(
        "SELECT id, first_name, last_name, cec_id, created_at FROM players WHERE approved = TRUE ORDER BY created_at ASC"
    ).fetchall()
    pending_players = db.execute(
        "SELECT id, first_name, last_name, cec_id, created_at FROM players WHERE approved = FALSE ORDER BY created_at ASC"
    ).fetchall()
    rows = db.execute(
        """
        SELECT m.*, p1.first_name || ' ' || p1.last_name AS player1_name,
               p2.first_name || ' ' || p2.last_name AS player2_name
        FROM matches m
        LEFT JOIN players p1 ON p1.id = m.player1_id
        LEFT JOIN players p2 ON p2.id = m.player2_id
        ORDER BY m.playoff DESC, COALESCE(m.week, m.playoff_round), m.id
        """
    ).fetchall()
    matches = [
        build_match_view(
            row,
            current_week=current_week,
            current_playoff_round=current_playoff_round,
            is_admin=True,
        )
        for row in rows
    ]

    regular_match_options = [
        {
            "id": match["id"],
            "label": f"Week {match['week']}: {match.get('player1_name') or 'TBD'} vs {('BYE' if match.get('player2_id') is None else (match.get('player2_name') or 'TBD'))} (Match {match['id']})",
        }
        for match in matches
        if not match["playoff"] and match.get("week")
    ]

    # Check if we can start playoffs
    can_start_playoffs = False
    playoffs_exist = (
        db.execute("SELECT 1 FROM matches WHERE playoff = 1 LIMIT 1").fetchone()
        is not None
    )

    if not playoffs_exist:
        max_week = db.execute(
            "SELECT MAX(week) as max_week FROM matches WHERE playoff = 0"
        ).fetchone()["max_week"]

        if max_week and max_week >= MAX_WEEKS:
            pending = db.execute(
                """
                SELECT COUNT(*) as count FROM matches
                WHERE playoff = 0 AND (reported = 0 OR reported IS NULL)
                  AND player2_id IS NOT NULL
                """
            ).fetchone()["count"]
            can_start_playoffs = pending == 0

    # Check if we can advance playoff round
    can_advance_round = False
    active_playoff_label = None
    if playoffs_exist and current_playoff_round >= 0:
        matches_in_round = get_round_matches(db, current_playoff_round)
        if matches_in_round:
            active_playoff_label = label_for_round(
                current_playoff_round, len(matches_in_round)
            )
            winners = collect_winners(matches_in_round)
            if round_is_complete(matches_in_round) and winners:
                can_advance_round = True

    return render_template(
        "admin.html",
        players=players,
        pending_players=pending_players,
        matches=matches,
        regular_match_options=regular_match_options,
        current_week=current_week,
        max_weeks=MAX_WEEKS,
        can_start_playoffs=can_start_playoffs,
        can_advance_round=can_advance_round,
        current_playoff_round=current_playoff_round,
        current_playoff_label=active_playoff_label,
    )


@app.route("/admin/player/<int:player_id>/approve", methods=["POST"])
@admin_required
def admin_approve_player(player_id):
    db = get_db()
    player = db.execute(PLAYER_APPROVAL_QUERY, (player_id,)).fetchone()
    if not player:
        flash("Player not found.", "error")
        return redirect(url_for("admin_dashboard"))
    if player["approved"]:
        flash(f"{format_player_name(player)} is already approved.", "info")
        return redirect(url_for("admin_dashboard"))

    db.execute(
        "UPDATE players SET approved = TRUE, approved_at = %s WHERE id = %s",
        (datetime.now(timezone.utc), player_id),
    )
    db.commit()
    flash(f"Approved {format_player_name(player)}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/player/<int:player_id>/reject", methods=["POST"])
@admin_required
def admin_reject_player(player_id):
    db = get_db()
    player = db.execute(PLAYER_APPROVAL_QUERY, (player_id,)).fetchone()
    if not player:
        flash("Player not found.", "error")
        return redirect(url_for("admin_dashboard"))
    if player["approved"]:
        flash(
            f"{format_player_name(player)} is already approved. Use delete if removal is required.",
            "error",
        )
        return redirect(url_for("admin_dashboard"))

    db.execute("DELETE FROM players WHERE id = %s", (player_id,))
    db.commit()
    flash(f"Rejected signup for {format_player_name(player)}.", "info")
    return redirect(url_for("admin_dashboard"))


def _load_and_validate_players(db, player_ids):
    records = {}
    for pid in filter(None, player_ids):
        row = db.execute(PLAYER_APPROVAL_QUERY, (pid,)).fetchone()
        if not row:
            return {}, f"Player with id {pid} not found."
        if not row["approved"]:
            return {}, f"{format_player_name(row)} is still pending approval."
        records[pid] = row
    return records, None


def _reset_match_and_assign_players(db, match_id, player1_id, player2_id):
    db.execute(
        """
        UPDATE matches
        SET player1_id = %s,
            player2_id = %s,
            score1 = NULL,
            score2 = NULL,
            game1_score1 = NULL,
            game1_score2 = NULL,
            game2_score1 = NULL,
            game2_score2 = NULL,
            game3_score1 = NULL,
            game3_score2 = NULL,
            reported = 0,
            double_forfeit = 0
        WHERE id = %s
        """,
        (player1_id, player2_id, match_id),
    )
    db.execute("DELETE FROM match_schedules WHERE match_id = %s", (match_id,))


@app.route("/admin/match/assign", methods=["POST"])
@admin_required
def admin_assign_match_players():
    db = get_db()
    match_raw = request.form.get("match_id", "").strip()
    if not match_raw:
        flash("Select a match to update.", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        match_id = int(match_raw)
    except ValueError:
        flash("Invalid match selection.", "error")
        return redirect(url_for("admin_dashboard"))

    match = db.execute(
        "SELECT id, week, playoff FROM matches WHERE id = %s",
        (match_id,),
    ).fetchone()
    if not match:
        flash("Match not found.", "error")
        return redirect(url_for("admin_dashboard"))
    if match["playoff"]:
        flash("Playoff matches must be managed from the playoff bracket.", "error")
        return redirect(url_for("admin_dashboard"))

    player1_raw = request.form.get("player1_id", "").strip()
    player2_raw = request.form.get("player2_id", "").strip()
    if not player1_raw:
        flash("Player 1 is required.", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        player1_id = int(player1_raw)
    except ValueError:
        flash("Invalid Player 1 selection.", "error")
        return redirect(url_for("admin_dashboard"))

    player2_id = None
    if player2_raw:
        try:
            player2_id = int(player2_raw)
        except ValueError:
            flash("Invalid Player 2 selection.", "error")
            return redirect(url_for("admin_dashboard"))

    if player2_id and player1_id == player2_id:
        flash("A player cannot face themselves.", "error")
        return redirect(url_for("admin_dashboard"))

    player_records, error_message = _load_and_validate_players(
        db, [player1_id, player2_id]
    )
    if error_message:
        flash(error_message, "error")
        return redirect(url_for("admin_dashboard"))

    player1_record = player_records.get(player1_id)
    player2_record = player_records.get(player2_id) if player2_id else None
    if not player1_record:
        flash("Player 1 must be approved before assignment.", "error")
        return redirect(url_for("admin_dashboard"))
    if player2_id and not player2_record:
        flash("Player 2 must be approved before assignment.", "error")
        return redirect(url_for("admin_dashboard"))

    week = match["week"]
    if week is not None:
        if player_has_week_match(db, player1_id, week, exclude_match_id=match_id):
            flash(
                f"{format_player_name(player1_record)} already has a Week {week} match.",
                "error",
            )
            return redirect(url_for("admin_dashboard"))
        if player2_id and player_has_week_match(
            db, player2_id, week, exclude_match_id=match_id
        ):
            flash(
                f"{format_player_name(player2_record)} already has a Week {week} match.",
                "error",
            )
            return redirect(url_for("admin_dashboard"))

    if matchup_already_exists(db, player1_id, player2_id, exclude_match_id=match_id):
        flash("These players already face each other elsewhere in the season.", "error")
        return redirect(url_for("admin_dashboard"))

    try:
        _reset_match_and_assign_players(db, match_id, player1_id, player2_id)
        db.commit()
    except Exception as exc:
        db.rollback()
        flash(f"Could not update match: {exc}", "error")
        return redirect(url_for("admin_dashboard"))

    player1_name = format_player_name(player1_record)
    if player2_id:
        player2_name = format_player_name(player2_record)
        flash(
            f"Match {match_id} updated: {player1_name} vs {player2_name}.",
            "success",
        )
    else:
        flash(f"Match {match_id} updated: {player1_name} receives a bye.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/match/create", methods=["POST"])
@admin_required
def admin_create_match():
    db = get_db()
    week_raw = request.form.get("week", "").strip()
    try:
        week = int(week_raw)
    except ValueError:
        flash("Invalid week selection.", "error")
        return redirect(url_for("admin_dashboard"))

    if week < 1 or week > MAX_WEEKS:
        flash(f"Week must be between 1 and {MAX_WEEKS}.", "error")
        return redirect(url_for("admin_dashboard"))

    player1_raw = request.form.get("player1_id", "").strip()
    if not player1_raw:
        flash("Player 1 is required.", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        player1_id = int(player1_raw)
    except ValueError:
        flash("Invalid Player 1 selection.", "error")
        return redirect(url_for("admin_dashboard"))

    player2_id = None
    player2_raw = request.form.get("player2_id", "").strip()
    if player2_raw:
        try:
            player2_id = int(player2_raw)
        except ValueError:
            flash("Invalid Player 2 selection.", "error")
            return redirect(url_for("admin_dashboard"))

    if player2_id and player1_id == player2_id:
        flash("A player cannot face themselves.", "error")
        return redirect(url_for("admin_dashboard"))

    player_records, error_message = _load_and_validate_players(
        db, [player1_id, player2_id]
    )
    if error_message:
        flash(error_message, "error")
        return redirect(url_for("admin_dashboard"))

    player1_record = player_records.get(player1_id)
    player2_record = player_records.get(player2_id) if player2_id else None
    if not player1_record:
        flash("Player 1 must be approved before creating a match.", "error")
        return redirect(url_for("admin_dashboard"))
    if player2_id and not player2_record:
        flash("Player 2 must be approved before creating a match.", "error")
        return redirect(url_for("admin_dashboard"))

    if player_has_week_match(db, player1_id, week):
        flash(
            f"{format_player_name(player1_record)} already has a Week {week} match.",
            "error",
        )
        return redirect(url_for("admin_dashboard"))
    if player2_id and player_has_week_match(db, player2_id, week):
        flash(
            f"{format_player_name(player2_record)} already has a Week {week} match.",
            "error",
        )
        return redirect(url_for("admin_dashboard"))

    if matchup_already_exists(db, player1_id, player2_id):
        flash("These players already face each other elsewhere in the season.", "error")
        return redirect(url_for("admin_dashboard"))

    try:
        cursor = db.execute(
            """
            INSERT INTO matches (
                week,
                player1_id,
                player2_id,
                score1,
                score2,
                reported,
                double_forfeit,
                playoff,
                playoff_round,
                created_at
            )
            VALUES (%s, %s, %s, NULL, NULL, 0, 0, 0, NULL, %s)
            RETURNING id
            """,
            (week, player1_id, player2_id, datetime.now(timezone.utc)),
        )
        new_match_id = cursor.fetchone()["id"]
        db.commit()
    except Exception as exc:
        db.rollback()
        flash(f"Could not create match: {exc}", "error")
        return redirect(url_for("admin_dashboard"))

    player1_name = format_player_name(player1_record)
    if player2_id:
        player2_name = format_player_name(player2_record)
        flash(
            f"Created Week {week} match {new_match_id}: {player1_name} vs {player2_name}.",
            "success",
        )
    else:
        flash(
            f"Created Week {week} match {new_match_id}: {player1_name} has a bye.",
            "success",
        )
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/generate_schedule", methods=["POST"])
@admin_required
def admin_generate_schedule():
    db = get_db()

    # Check if any matches have been reported
    reported_matches = db.execute(
        "SELECT COUNT(*) as count FROM matches WHERE reported = 1 AND playoff = 0"
    ).fetchone()

    if reported_matches["count"] > 0:
        flash(
            f"⚠️ WARNING: Regenerating schedule will DELETE {reported_matches['count']} reported match(es) and reset to Week 1. All standings will be lost!",
            "error",
        )
        # Require confirmation - add a hidden form field check
        confirmation = request.form.get("confirm_regenerate")
        if confirmation != "yes":
            flash(
                "Schedule regeneration cancelled. Add %sconfirm=yes to the form to proceed.",
                "info",
            )
            return redirect(url_for("admin_dashboard"))

    success, error_message = generate_weekly_schedule(db)
    if not success:
        flash(error_message or "Unable to regenerate schedule.", "error")
        return redirect(url_for("admin_dashboard"))

    flash("Regular season schedule regenerated and reset to Week 1.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/force_playoffs", methods=["POST"])
@admin_required
def admin_force_playoffs():
    db = get_db()

    # Check if there are enough players
    player_count = db.execute(
        "SELECT COUNT(*) as count FROM players WHERE approved = TRUE"
    ).fetchone()["count"]
    if player_count < 2:
        flash("Need at least 2 players to generate playoff bracket.", "error")
        return redirect(url_for("admin_dashboard"))

    # Check if playoff bracket already exists with reported matches
    existing_playoff_matches = db.execute(
        "SELECT COUNT(*) as count FROM matches WHERE playoff = 1 AND reported = 1"
    ).fetchone()

    if existing_playoff_matches["count"] > 0:
        flash(
            f"⚠️ WARNING: Playoff bracket has {existing_playoff_matches['count']} reported match(es). Regenerating will DELETE all playoff results!",
            "error",
        )
        confirmation = request.form.get("confirm_regenerate")
        if confirmation != "yes":
            flash(
                "Playoff regeneration cancelled. Add confirmation to proceed.", "info"
            )
            return redirect(url_for("admin_dashboard"))

    create_playoff_bracket(db)
    flash("Playoff bracket generated from current standings.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/start_playoffs", methods=["POST"])
@admin_required
def admin_start_playoffs():
    """Manually start playoffs once regular season is complete."""
    db = get_db()

    # Check if playoffs already exist
    existing = db.execute("SELECT 1 FROM matches WHERE playoff = 1 LIMIT 1").fetchone()
    if existing:
        flash("Playoffs have already been created.", "error")
        return redirect(url_for("admin_dashboard"))

    # Check if regular season is complete
    max_week = db.execute(
        "SELECT MAX(week) as max_week FROM matches WHERE playoff = 0"
    ).fetchone()["max_week"]

    if not max_week or max_week < MAX_WEEKS:
        flash(f"Cannot start playoffs until week {MAX_WEEKS} is generated.", "error")
        return redirect(url_for("admin_dashboard"))

    # Check for unreported matches
    pending = db.execute(
        """
        SELECT COUNT(*) as count FROM matches
        WHERE playoff = 0 AND (reported = 0 OR reported IS NULL)
          AND player2_id IS NOT NULL
        """
    ).fetchone()["count"]

    if pending > 0:
        flash(
            f"Cannot start playoffs: {pending} regular season match(es) still unreported.",
            "error",
        )
        return redirect(url_for("admin_dashboard"))

    create_playoff_bracket(db)
    flash("Playoff bracket created! First round is ready.", "success")
    return redirect(url_for("playoffs"))


@app.route("/admin/advance_playoff_round", methods=["POST"])
@admin_required
def admin_advance_playoff_round():
    """Manually advance to the next playoff round."""
    db = get_db()
    current_round = get_current_playoff_round(db)
    if current_round < 0:
        flash("Playoffs are not currently active.", "error")
        return redirect(url_for("admin_dashboard"))

    matches = get_round_matches(db, current_round)
    if not matches:
        flash("No matches found for the active playoff round.", "error")
        return redirect(url_for("admin_dashboard"))

    if not round_is_complete(matches):
        unreported = sum(
            1 for m in matches if not m["reported"] and m["player2_id"] is not None
        )
        flash(
            f"Cannot advance: {unreported} match(es) in Round {current_round} are still unreported.",
            "error",
        )
        return redirect(url_for("admin_dashboard"))

    winners = collect_winners(matches)
    if not winners:
        flash("Cannot advance: No winners recorded in the current round.", "error")
        return redirect(url_for("admin_dashboard"))

    if len(winners) == 1:
        champion_id = winners[0]
        champion = db.execute(
            "SELECT first_name, last_name FROM players WHERE id = %s",
            (champion_id,),
        ).fetchone()
        set_current_playoff_round(db, -1)
        db.commit()
        if champion:
            flash(
                f"Tournament complete! Champion: {champion['first_name']} {champion['last_name']}.",
                "success",
            )
        else:
            flash("Tournament complete! Champion has been crowned.", "success")
        return redirect(url_for("playoffs"))

    if current_round == 0:
        seed_to_winner = {}
        for match in matches:
            winner = determine_winner(match)
            if not winner:
                continue
            target_seed = match.get("match_number")
            if target_seed is None:
                continue
            seed_to_winner[target_seed] = winner

        round_1_matches = db.execute(
            """
            SELECT * FROM matches
            WHERE playoff = 1 AND playoff_round = 1
            ORDER BY id
            """
        ).fetchall()

        if not round_1_matches:
            flash("Round 1 bracket is missing.", "error")
            return redirect(url_for("admin_dashboard"))

        pair_count = len(round_1_matches)
        target_bracket_size = pair_count * 2 if pair_count else 0
        updated = False

        for match in round_1_matches:
            match_index = match.get("match_number")
            if not match_index:
                continue
            seed1 = match_index
            seed2 = (
                target_bracket_size + 1 - match_index if target_bracket_size else None
            )

            if match["player1_id"] is None and seed1 in seed_to_winner:
                db.execute(
                    "UPDATE matches SET player1_id = %s WHERE id = %s",
                    (seed_to_winner[seed1], match["id"]),
                )
                updated = True

            if match["player2_id"] is None and seed2 in seed_to_winner:
                db.execute(
                    "UPDATE matches SET player2_id = %s WHERE id = %s",
                    (seed_to_winner[seed2], match["id"]),
                )
                updated = True

        if not updated:
            flash(
                "No available slots to fill with play-in winners. Verify bracket integrity.",
                "error",
            )
            db.rollback()
            return redirect(url_for("admin_dashboard"))

        set_current_playoff_round(db, 1)
        db.commit()
        flash("Play-in winners advanced to Round 1!", "success")
        return redirect(url_for("playoffs"))

    next_round = current_round + 1
    if not next_round_exists(db, next_round):
        create_next_round(db, winners, next_round)
    else:
        next_matches = get_round_matches(db, next_round)
        if not next_matches:
            flash("Next round bracket exists but contains no matches.", "error")
            return redirect(url_for("admin_dashboard"))

        winner_iter = iter(winners)
        updated = False
        for match in next_matches:
            if match["player1_id"] is None:
                try:
                    player = next(winner_iter)
                except StopIteration:
                    break
                db.execute(
                    "UPDATE matches SET player1_id = %s WHERE id = %s",
                    (player, match["id"]),
                )
                updated = True
            if match["player2_id"] is None:
                try:
                    player = next(winner_iter)
                except StopIteration:
                    break
                db.execute(
                    "UPDATE matches SET player2_id = %s WHERE id = %s",
                    (player, match["id"]),
                )
                updated = True

        if updated:
            db.commit()

    set_current_playoff_round(db, next_round)
    db.commit()

    next_round_count = db.execute(
        "SELECT COUNT(*) as count FROM matches WHERE playoff = 1 AND playoff_round = %s",
        (next_round,),
    ).fetchone()["count"]

    round_label = label_for_round(next_round, next_round_count)
    flash(f"Advanced to {round_label}!", "success")
    return redirect(url_for("playoffs"))


@app.route("/admin/week", methods=["POST"])
@admin_required
def admin_update_week():
    db = get_db()
    current_week = get_current_week(db)
    raw_value = request.form.get("current_week", "").strip()
    try:
        new_week = int(raw_value)
    except ValueError:
        flash("Week must be an integer.", "error")
        return redirect(url_for("admin_dashboard"))
    if new_week < 1 or new_week > MAX_WEEKS:
        flash(f"Week must be between 1 and {MAX_WEEKS}.", "error")
        return redirect(url_for("admin_dashboard"))

    matches_to_forfeit = 0
    if new_week > current_week:
        matches_to_forfeit = db.execute(
            """
            SELECT COUNT(*) as count FROM matches
            WHERE playoff = 0
              AND week IS NOT NULL
              AND week >= %s
              AND week < %s
              AND player2_id IS NOT NULL
              AND (reported = 0 OR reported IS NULL)
            """,
            (current_week, new_week),
        ).fetchone()["count"]
        if matches_to_forfeit > 0 and request.form.get("confirm_week_jump") != "yes":
            flash(
                f"⚠️ WARNING: Advancing from Week {current_week} to Week {new_week} will AUTO-FORFEIT {matches_to_forfeit} unreported match(es). Use the confirmation checkbox to proceed.",
                "error",
            )
            return redirect(url_for("admin_dashboard"))

    forfeited = 0
    generated_weeks = []
    if new_week > current_week:
        success, forfeited, generated_weeks, generation_error = (
            _advance_regular_season_weeks(db, current_week, new_week)
        )
        if not success:
            db.rollback()
            flash(generation_error, "error")
            return redirect(url_for("admin_dashboard"))
    set_current_week(db, new_week)
    db.commit()
    flash(f"Current week set to {new_week}.", "success")
    if forfeited:
        flash(f"Auto-forfeited {forfeited} unreported matches.", "error")
    if generated_weeks:
        if len(generated_weeks) == 1:
            flash(
                f"Week {generated_weeks[0]} matchups generated and published.",
                "info",
            )
        else:
            week_labels = ", ".join(str(week) for week in generated_weeks)
            flash(f"Weeks {week_labels} matchups generated and published.", "info")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/match/<int:match_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_match(match_id):
    db = get_db()
    match = db.execute(
        """
        SELECT m.*, p1.first_name || ' ' || p1.last_name AS player1_name,
               p2.first_name || ' ' || p2.last_name AS player2_name
        FROM matches m
        LEFT JOIN players p1 ON p1.id = m.player1_id
        LEFT JOIN players p2 ON p2.id = m.player2_id
        WHERE m.id = %s
        """,
        (match_id,),
    ).fetchone()
    
    if not match:
        flash("Match not found.", "error")
        return redirect(url_for("admin_dashboard"))
    
    if match["player2_id"] is None:
        flash("Cannot edit scores for bye matches.", "error")
        return redirect(url_for("admin_dashboard"))
    
    if not match["reported"]:
        flash("Cannot edit unreported matches. Use regular match reporting instead.", "error")
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        try:
            game_scores = parse_best_of_three_scores(request.form)
            validate_best_of_three(game_scores)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin_edit_match", match_id=match_id))

        totals = (
            sum(score[0] for score in game_scores),
            sum(score[1] for score in game_scores),
        )
        padded = game_scores + [(None, None)] * (MAX_GAMES - len(game_scores))

        try:
            db.execute(
                """
                UPDATE matches SET
                    game1_score1 = %s, game1_score2 = %s,
                    game2_score1 = %s, game2_score2 = %s,
                    game3_score1 = %s, game3_score2 = %s,
                    score1 = %s, score2 = %s,
                    reported = 1,
                    double_forfeit = 0
                WHERE id = %s
                """,
                (
                    padded[0][0], padded[0][1],
                    padded[1][0], padded[1][1],
                    padded[2][0], padded[2][1],
                    totals[0], totals[1],
                    match_id,
                ),
            )
            db.commit()
            flash(f"Match scores updated successfully for {match['player1_name']} vs {match['player2_name']}.", "success")
            return redirect(url_for("admin_dashboard"))
        except Exception as exc:
            flash(f"Database error: {exc}", "error")
            return redirect(url_for("admin_edit_match", match_id=match_id))

    existing_scores = extract_game_scores(match)
    padded_scores = existing_scores + [(None, None)] * (
        MAX_GAMES - len(existing_scores)
    )
    return render_template(
        "admin_edit_match.html",
        match=match,
        game_scores=padded_scores,
        max_games=MAX_GAMES,
    )


@app.route("/admin/player/<int:player_id>/delete", methods=["POST"])
@admin_required
def admin_delete_player(player_id):
    db = get_db()

    # Check if player has any reported matches
    reported_matches = db.execute(
        """
        SELECT COUNT(*) as count FROM matches
        WHERE (player1_id = %s OR player2_id = %s)
        AND reported = 1
        AND player2_id IS NOT NULL
        """,
        (player_id, player_id),
    ).fetchone()

    if reported_matches["count"] > 0:
        flash(
            "Cannot delete player with reported match history. This would corrupt rankings and standings.",
            "error",
        )
        return redirect(url_for("admin_dashboard"))

    # Check if player is in playoff bracket
    playoff_matches = db.execute(
        "SELECT COUNT(*) as count FROM matches WHERE (player1_id = %s OR player2_id = %s) AND playoff = 1",
        (player_id, player_id),
    ).fetchone()

    if playoff_matches["count"] > 0:
        flash(
            "Cannot delete player in playoff bracket. Reset the league or wait until next season.",
            "error",
        )
        return redirect(url_for("admin_dashboard"))

    # Safe to delete - only unreported regular season matches
    db.execute(
        "DELETE FROM matches WHERE player1_id = %s OR player2_id = %s",
        (player_id, player_id),
    )
    db.execute("DELETE FROM players WHERE id = %s", (player_id,))
    db.commit()
    flash("Player removed. Note: Schedule may need regeneration.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/match/<int:match_id>/delete", methods=["POST"])
@admin_required
def admin_delete_match(match_id):
    db = get_db()
    db.execute("DELETE FROM matches WHERE id = %s", (match_id,))
    db.commit()
    flash("Match deleted.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/reset", methods=["POST"])
@admin_required
def admin_reset_data():
    db = get_db()
    db.execute("DELETE FROM matches")
    db.execute("DELETE FROM players")
    # PostgreSQL SERIAL sequences are automatically managed, no need to reset like sqlite_sequence
    db.execute("ALTER SEQUENCE players_id_seq RESTART WITH 1")
    db.execute("ALTER SEQUENCE matches_id_seq RESTART WITH 1")
    set_current_week(db, 1)
    set_current_playoff_round(db, -1)
    db.commit()
    flash("All league data cleared.", "success")
    return redirect(url_for("admin_dashboard"))


def apply_match_to_stats(stats, match):
    p1 = stats.get(match["player1_id"])
    p2 = stats.get(match["player2_id"])
    if not p1 or not p2:
        return
    if get_value(match, "double_forfeit"):
        p1["losses"] += 1
        p2["losses"] += 1
        return
    scores = extract_game_scores(match)
    total1, total2 = aggregate_match_points(match)
    p1["points_scored"] += total1
    p2["points_scored"] += total2
    p1["point_diff"] += total1 - total2
    p2["point_diff"] += total2 - total1
    if scores:
        wins1, wins2 = calculate_game_wins(scores)
        if wins1 > wins2:
            p1["wins"] += 1
            p2["losses"] += 1
        else:
            p2["wins"] += 1
            p1["losses"] += 1
    elif total1 != total2:
        if total1 > total2:
            p1["wins"] += 1
            p2["losses"] += 1
        else:
            p2["wins"] += 1
            p1["losses"] += 1


def fetch_ranked_matches(db, include_playoffs):
    clause = "" if include_playoffs else "AND playoff = 0"
    return db.execute(
        f"""
        SELECT * FROM matches
        WHERE reported = 1 {clause}
          AND player1_id IS NOT NULL
          AND player2_id IS NOT NULL
        """
    ).fetchall()


def calculate_rankings(db, include_playoffs=True):
    players = db.execute(
        "SELECT id, first_name, last_name FROM players WHERE approved = TRUE ORDER BY first_name, last_name"
    ).fetchall()
    if not players:
        return []
    stats = {
        player["id"]: {
            "player_id": player["id"],
            "first_name": player["first_name"],
            "last_name": player["last_name"],
            "wins": 0,
            "losses": 0,
            "point_diff": 0,
            "points_scored": 0,
        }
        for player in players
    }

    for match in fetch_ranked_matches(db, include_playoffs):
        apply_match_to_stats(stats, match)

    ordered = sorted(
        stats.values(),
        key=lambda row: (
            -row["wins"],
            -row["point_diff"],
            -row["points_scored"],
            row["last_name"],
            row["first_name"],
        ),
    )
    for idx, row in enumerate(ordered, start=1):
        row["rank"] = idx
    return ordered


def generate_weekly_schedule(db):
    player_ids = collect_active_player_ids(db)
    db.execute("DELETE FROM match_schedules WHERE week IS NOT NULL")
    db.execute("DELETE FROM matches WHERE playoff = 0")
    set_current_week(db, 1)

    success, _, error_message = ensure_regular_week_generated(
        db, 1, player_ids=player_ids
    )

    if not success:
        db.rollback()
        return False, error_message

    db.commit()
    return True, None


def collect_active_player_ids(db):
    rows = db.execute(
        "SELECT id FROM players WHERE approved = TRUE ORDER BY created_at ASC"
    ).fetchall()
    return [row["id"] for row in rows]


def _regular_week_exists(db, target_week):
    return (
        db.execute(
            "SELECT 1 FROM matches WHERE playoff = 0 AND week = %s LIMIT 1",
            (target_week,),
        ).fetchone()
        is not None
    )


def _collect_existing_pairs(db, roster_set):
    rows = db.execute(
        """
        SELECT player1_id, player2_id FROM matches
        WHERE playoff = 0
          AND player1_id IS NOT NULL
          AND player2_id IS NOT NULL
        """
    ).fetchall()
    return {
        frozenset({row["player1_id"], row["player2_id"]})
        for row in rows
        if row["player1_id"] in roster_set and row["player2_id"] in roster_set
    }


def _persist_generated_week(db, target_week, matches):
    now = datetime.now(timezone.utc)
    inserted = 0
    for player1_id, player2_id in matches:
        if player1_id is None and player2_id is None:
            continue

        is_bye = player2_id is None
        db.execute(
            """
            INSERT INTO matches (
                week,
                player1_id,
                player2_id,
                score1,
                score2,
                reported,
                double_forfeit,
                playoff,
                playoff_round,
                created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, 0, 0, NULL, %s)
            """,
            (
                target_week,
                player1_id,
                player2_id,
                0 if is_bye else None,
                0 if is_bye else None,
                1 if is_bye else 0,
                now,
            ),
        )
        inserted += 1
    return inserted


def ensure_regular_week_generated(db, target_week, player_ids=None):
    if target_week < 1 or target_week > MAX_WEEKS:
        return False, False, f"Week must be between 1 and {MAX_WEEKS}."

    if _regular_week_exists(db, target_week):
        return True, False, None

    if player_ids is None:
        player_ids = collect_active_player_ids(db)

    if len(player_ids) < 2:
        return (
            False,
            False,
            "Need at least two approved players to generate regular season matches.",
        )

    roster_set = set(player_ids)
    existing_pairs = _collect_existing_pairs(db, roster_set)

    generated = generate_reseeded_weeks(player_ids, existing_pairs, weeks_needed=1)
    if not generated:
        return (
            False,
            False,
            "Unable to generate a conflict-free schedule for the next week.",
        )

    inserted = _persist_generated_week(db, target_week, generated[0])
    if inserted == 0:
        return False, False, "Generated week contained no matches."

    return True, True, None


def ensure_weeks_generated(db, week_numbers, player_ids=None):
    created = []
    for week in week_numbers:
        success, inserted, error_message = ensure_regular_week_generated(
            db, week, player_ids=player_ids
        )
        if not success:
            return False, created, error_message
        if inserted:
            created.append(week)
    return True, created, None


def _advance_regular_season_weeks(db, current_week, new_week):
    cursor = db.execute(
        """
        UPDATE matches
        SET reported = 1,
            double_forfeit = 1,
            score1 = 0,
            score2 = 0,
            game1_score1 = NULL,
            game1_score2 = NULL,
            game2_score1 = NULL,
            game2_score2 = NULL,
            game3_score1 = NULL,
            game3_score2 = NULL
        WHERE playoff = 0
          AND week IS NOT NULL
          AND week >= %s
          AND week < %s
          AND player2_id IS NOT NULL
          AND (reported = 0 OR reported IS NULL)
        """,
        (current_week, new_week),
    )

    player_ids = collect_active_player_ids(db)
    success, generated_weeks, error_message = ensure_weeks_generated(
        db, range(current_week + 1, new_week + 1), player_ids=player_ids
    )
    if not success:
        return False, cursor.rowcount, [], error_message

    return True, cursor.rowcount, generated_weeks, None


def _future_regular_weeks_have_scores(db):
    count = db.execute(
        "SELECT COUNT(*) as count FROM matches WHERE playoff = 0 AND week > 1 AND reported = 1"
    ).fetchone()["count"]
    return count > 0


def _load_week_one_metadata(db):
    rows = db.execute(
        "SELECT id, player1_id, player2_id FROM matches WHERE playoff = 0 AND week = 1"
    ).fetchall()
    players_with_week_one = set()
    existing_pairs = set()
    for row in rows:
        if row["player1_id"]:
            players_with_week_one.add(row["player1_id"])
        if row["player2_id"]:
            players_with_week_one.add(row["player2_id"])
        if row["player1_id"] and row["player2_id"]:
            existing_pairs.add(frozenset({row["player1_id"], row["player2_id"]}))
    return players_with_week_one, existing_pairs


def _ensure_week_one_coverage(db, player_ids, players_with_week_one, existing_pairs):
    missing_players = [pid for pid in player_ids if pid not in players_with_week_one]
    if not missing_players:
        return True, None, None

    if len(missing_players) != 2:
        return (
            False,
            "Unexpected number of players missing Week 1 matches. Please resolve manually before reseeding.",
            "error",
        )

    new_pair = frozenset(missing_players)
    if new_pair in existing_pairs:
        return (
            False,
            "Week 1 already contains this pairing. Assign the new players manually before reseeding.",
            "error",
        )

    try:
        db.execute(
            """
            INSERT INTO matches (
                week,
                player1_id,
                player2_id,
                score1,
                score2,
                reported,
                double_forfeit,
                playoff,
                playoff_round,
                created_at
            )
            VALUES (1, %s, %s, NULL, NULL, 0, 0, 0, NULL, %s)
            """,
            (
                missing_players[0],
                missing_players[1],
                datetime.now(timezone.utc),
            ),
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        return False, f"Could not create Week 1 match for new players: {exc}", "error"

    existing_pairs.add(new_pair)
    return (
        True,
        "Added Week 1 match for newly approved players before reseeding.",
        "info",
    )


def _clear_future_regular_weeks(db, start_week):
    schedules_removed = db.execute(
        "DELETE FROM match_schedules WHERE week >= %s", (start_week,)
    ).rowcount
    matches_removed = db.execute(
        "DELETE FROM matches WHERE playoff = 0 AND week >= %s", (start_week,)
    ).rowcount
    db.commit()
    return matches_removed, schedules_removed


def build_round_robin(player_ids, limit_weeks):
    rotation = prepare_rotation(player_ids)
    rounds = []
    current = rotation[:]
    total_rounds = max(len(rotation) - 1, limit_weeks)
    for _ in range(total_rounds):
        rounds.append(capture_pairs(current))
        current = rotate_players(current)
    trimmed = rounds[:limit_weeks]
    return [clean_pairs(pairs) for pairs in trimmed]


def prepare_rotation(player_ids):
    roster = list(player_ids)
    if len(roster) % 2 != 0:
        roster.append(None)
    return roster


def capture_pairs(rotation):
    half = len(rotation) // 2
    return [
        normalize_pair(rotation[i], rotation[-(i + 1)])
        for i in range(half)
        if not (rotation[i] is None and rotation[-(i + 1)] is None)
    ]


def rotate_players(rotation):
    if len(rotation) <= 2:
        return rotation[:]
    return [rotation[0], rotation[-1], *rotation[1:-1]]


def clean_pairs(pairs):
    return [pair for pair in pairs if pair[0] is not None or pair[1] is not None]


def auto_start_playoffs_if_ready(db):
    existing = db.execute("SELECT 1 FROM matches WHERE playoff = 1 LIMIT 1").fetchone()
    if existing:
        return
    total_regular = db.execute(
        "SELECT COUNT(*) as count FROM matches WHERE playoff = 0"
    ).fetchone()["count"]
    if total_regular == 0:
        return
    max_week = db.execute(
        "SELECT MAX(week) as max_week FROM matches WHERE playoff = 0"
    ).fetchone()["max_week"]
    if not max_week or max_week < MAX_WEEKS:
        return
    pending = db.execute(
        """
        SELECT COUNT(*) as count FROM matches
        WHERE playoff = 0 AND (reported = 0 OR reported IS NULL)
          AND player2_id IS NOT NULL
        """
    ).fetchone()["count"]
    if pending == 0:
        create_playoff_bracket(db)


def create_playoff_bracket(db):
    """
    Create playoff bracket with proper play-in games.

    Strategy for non-power-of-2 player counts:
    - Reduce to nearest LOWER power of 2 for main bracket
    - Example: 19 players → 16 main bracket
    - Play-in games = num_players - target_bracket_size (19 - 16 = 3 games)
    - Play-in participants = play_in_games * 2 (3 * 2 = 6 players)
    - Byes = target_bracket_size - play_in_games (16 - 3 = 13 players)
    - Top 13 seeds get byes, seeds 14-19 play in 3 play-in games
    - 3 winners join 13 bye players to form 16-player main bracket
    """
    db.execute("DELETE FROM matches WHERE playoff = 1")
    db.commit()
    rankings = calculate_rankings(db, include_playoffs=False)
    if len(rankings) < 2:
        set_current_playoff_round(db, -1)
        db.commit()
        return

    player_ranks = {row["player_id"]: row["rank"] for row in rankings}
    seeds = [row["player_id"] for row in rankings]
    num_players = len(seeds)

    # Check if already a power of 2
    if num_players & (num_players - 1) == 0:
        # Perfect power of 2 - no play-ins needed
        now = datetime.now(timezone.utc)
        create_first_round_matches(db, seeds, playoff_round=1, created_at=now)
        set_current_playoff_round(db, 1)
        db.commit()
        return

    # Need play-in games to reduce to lower power of 2
    # Find nearest lower power of 2
    target_bracket_size = 1 << (num_players.bit_length() - 1)

    # Calculate play-ins needed
    num_play_in_games = num_players - target_bracket_size
    num_byes = target_bracket_size - num_play_in_games

    now = datetime.now(timezone.utc)
    play_in_matches_meta = generate_play_in_matches_metadata(
        seeds, target_bracket_size, num_byes, player_ranks
    )

    # Create play-in games (Round 0)
    for match_meta in play_in_matches_meta:
        db.execute(
            """
            INSERT INTO matches (
                week,
                player1_id,
                player2_id,
                score1,
                score2,
                reported,
                playoff,
                playoff_round,
                match_number,
                created_at
            )
            VALUES (NULL, %s, %s, NULL, NULL, 0, 1, 0, %s, %s)
            """,
            (
                match_meta["player1_id"],
                match_meta["player2_id"],
                match_meta["target_seed"],
                now,
            ),
        )

    # Create Round 1 (main bracket first round)
    main_bracket_first_round_matches = target_bracket_size // 2

    for match_index in range(1, main_bracket_first_round_matches + 1):
        seed1 = match_index
        seed2 = target_bracket_size + 1 - match_index

        player1_id = seeds[seed1 - 1] if seed1 <= num_byes else None
        player2_id = seeds[seed2 - 1] if seed2 <= num_byes else None

        db.execute(
            """
            INSERT INTO matches (
                week,
                player1_id,
                player2_id,
                score1,
                score2,
                reported,
                playoff,
                playoff_round,
                match_number,
                created_at
            )
            VALUES (NULL, %s, %s, NULL, NULL, 0, 1, 1, %s, %s)
            """,
            (player1_id, player2_id, match_index, now),
        )

    set_current_playoff_round(db, 0)
    db.commit()


def create_first_round_matches(db, seeds, playoff_round, created_at):
    """Helper to create first round matches when no play-ins are needed."""
    pair_count = len(seeds) // 2
    for slot in range(pair_count):
        p1 = seeds[slot]
        p2 = seeds[-(slot + 1)]
        db.execute(
            """
            INSERT INTO matches (
                week,
                player1_id,
                player2_id,
                score1,
                score2,
                reported,
                playoff,
                playoff_round,
                match_number,
                created_at
            )
            VALUES (NULL, %s, %s, NULL, NULL, 0, 1, %s, %s, %s)
            """,
            (p1, p2, playoff_round, slot + 1, created_at),
        )


def auto_resolve_byes(db):
    byes = db.execute(
        """
        SELECT id FROM matches
        WHERE playoff = 1 AND player2_id IS NULL AND reported = 1
          AND (score1 IS NULL OR score2 IS NULL)
        """
    ).fetchall()
    for match in byes:
        db.execute(
            "UPDATE matches SET score1 = 0, score2 = 0 WHERE id = %s",
            (match["id"],),
        )
    db.commit()


def advance_playoff_winners(db, _recursion_depth=0):
    """
    Advance playoff winners to the next round.
    Special handling for play-in games (round 0) - winners fill into Round 1 matches.
    """
    # Prevent infinite recursion
    MAX_RECURSION_DEPTH = 10
    if _recursion_depth >= MAX_RECURSION_DEPTH:
        return

    round_numbers = list_round_numbers(db)
    created_next_round = False

    for round_number in round_numbers:
        matches = get_round_matches(db, round_number)
        if not matches or not round_is_complete(matches):
            continue

        # Special case: Play-in games (round 0) advance to existing Round 1 matches
        winners = collect_winners(matches)

        # Check for problematic scenarios
        if len(winners) == 0:
            # All matches were double-forfeits or ties - admin needs to intervene
            continue

        if round_number == 0:
            seed_to_winner = {}
            for match in matches:
                winner = determine_winner(match)
                if not winner:
                    continue
                target_seed = match.get("match_number")
                if target_seed is None:
                    continue
                seed_to_winner[target_seed] = winner

            if not seed_to_winner:
                # Fallback for legacy brackets without match metadata
                round_1_matches = db.execute(
                    """
                    SELECT * FROM matches
                    WHERE playoff = 1 AND playoff_round = 1
                    ORDER BY id
                    """
                ).fetchall()

                if not round_1_matches:
                    continue

                winner_index = 0
                for match in round_1_matches:
                    if match["player1_id"] is None and winner_index < len(winners):
                        db.execute(
                            "UPDATE matches SET player1_id = %s WHERE id = %s",
                            (winners[winner_index], match["id"]),
                        )
                        winner_index += 1

                    if match["player2_id"] is None and winner_index < len(winners):
                        db.execute(
                            "UPDATE matches SET player2_id = %s WHERE id = %s",
                            (winners[winner_index], match["id"]),
                        )
                        winner_index += 1

                if winner_index > 0:
                    db.commit()
                    created_next_round = True
                continue

            # Get Round 1 matches in order
            round_1_matches = db.execute(
                """
                SELECT * FROM matches
                WHERE playoff = 1 AND playoff_round = 1
                ORDER BY id
                """
            ).fetchall()

            if not round_1_matches:
                continue

            pair_count = len(round_1_matches)
            target_bracket_size = pair_count * 2
            updated = False

            for match in round_1_matches:
                match_index = match.get("match_number")
                if not match_index:
                    continue
                seed1 = match_index
                seed2 = target_bracket_size + 1 - match_index

                if match["player1_id"] is None:
                    winner_seed1 = seed_to_winner.get(seed1)
                    if winner_seed1:
                        db.execute(
                            "UPDATE matches SET player1_id = %s WHERE id = %s",
                            (winner_seed1, match["id"]),
                        )
                        updated = True

                if match["player2_id"] is None:
                    winner_seed2 = seed_to_winner.get(seed2)
                    if winner_seed2:
                        db.execute(
                            "UPDATE matches SET player2_id = %s WHERE id = %s",
                            (winner_seed2, match["id"]),
                        )
                        updated = True

            if updated:
                db.commit()
                created_next_round = True
            continue

        # Normal case: create next round if winners exist and next round doesn't
        next_round = round_number + 1
        if len(winners) <= 1 or next_round_exists(db, next_round):
            continue
        create_next_round(db, winners, next_round)
        created_next_round = True

    if created_next_round:
        advance_playoff_winners(db, _recursion_depth + 1)


def list_round_numbers(db):
    rows = db.execute(
        "SELECT DISTINCT playoff_round FROM matches WHERE playoff = 1 ORDER BY playoff_round"
    ).fetchall()
    return [row["playoff_round"] for row in rows]


def get_round_matches(db, round_number):
    return db.execute(
        "SELECT * FROM matches WHERE playoff = 1 AND playoff_round = %s ORDER BY id",
        (round_number,),
    ).fetchall()


def round_is_complete(matches):
    for match in matches:
        if match["player2_id"] is None:
            continue
        if match["reported"] == 0:
            return False
    return True


def collect_winners(matches):
    winners = []
    for match in matches:
        winner = determine_winner(match)
        if winner:
            winners.append(winner)
    return winners


def next_round_exists(db, round_number):
    return (
        db.execute(
            "SELECT 1 FROM matches WHERE playoff = 1 AND playoff_round = %s",
            (round_number,),
        ).fetchone()
        is not None
    )


def create_next_round(db, winners, round_number):
    timestamp = datetime.now(timezone.utc)
    for idx in range(0, len(winners), 2):
        p1 = winners[idx]
        p2 = winners[idx + 1] if idx + 1 < len(winners) else None
        is_bye = p2 is None
        score1 = 0 if is_bye else None
        score2 = 0 if is_bye else None
        reported_flag = 1 if is_bye else 0
        match_number = (idx // 2) + 1
        db.execute(
            """
            INSERT INTO matches (
                week,
                player1_id,
                player2_id,
                score1,
                score2,
                reported,
                playoff,
                playoff_round,
                match_number,
                created_at
            )
            VALUES (NULL, %s, %s, %s, %s, %s, 1, %s, %s, %s)
            """,
            (
                p1,
                p2,
                score1,
                score2,
                reported_flag,
                round_number,
                match_number,
                timestamp,
            ),
        )
    db.commit()
    auto_resolve_byes(db)


def determine_winner(match):
    if match["player2_id"] is None:
        return match["player1_id"]
    if match["reported"] == 0:
        return None
    if get_value(match, "double_forfeit"):
        # For double forfeits in playoffs, return None so no one advances
        # Admin will need to manually fix or delete the match
        return None
    scores = extract_game_scores(match)
    if scores:
        wins1, wins2 = calculate_game_wins(scores)
        if wins1 == wins2:
            return None
        return match["player1_id"] if wins1 > wins2 else match["player2_id"]
    score1 = match["score1"]
    score2 = match["score2"]
    if score1 is None or score2 is None or score1 == score2:
        return None
    return match["player1_id"] if score1 > score2 else match["player2_id"]


def get_playoff_champion(db):
    # First, check if there's a Finals match (the round with only 1 match)
    # Get the match count per round
    round_counts = db.execute(
        """
        SELECT playoff_round, COUNT(*) as count
        FROM matches
        WHERE playoff = 1
        GROUP BY playoff_round
        ORDER BY playoff_round DESC
        """
    ).fetchall()

    if not round_counts:
        return None

    # Find the round with exactly 1 match (the Finals)
    finals_round = None
    for row in round_counts:
        if row["count"] == 1:
            finals_round = row["playoff_round"]
            break

    if finals_round is None:
        # No finals yet (no round has exactly 1 match)
        return None

    # Get the finals match
    final_match = db.execute(
        """
        SELECT m.*, p1.first_name AS p1_first, p1.last_name AS p1_last,
               p2.first_name AS p2_first, p2.last_name AS p2_last
        FROM matches m
        LEFT JOIN players p1 ON p1.id = m.player1_id
        LEFT JOIN players p2 ON p2.id = m.player2_id
        WHERE m.playoff = 1 AND m.playoff_round = %s
        LIMIT 1
        """,
        (finals_round,),
    ).fetchone()

    if not final_match:
        return None

    # Check if the finals match is complete and has a winner
    if not final_match["reported"] or final_match["player2_id"] is None:
        return None

    winner_id = determine_winner(final_match)
    if not winner_id:
        return None

    return db.execute(
        "SELECT id, first_name, last_name FROM players WHERE id = %s",
        (winner_id,),
    ).fetchone()


if __name__ == "__main__":
    app.run(debug=True)
