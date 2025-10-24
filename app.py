import os
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

    # Ensure all columns exist (for schema migrations)
    ensure_match_columns(db)
    ensure_default_settings(db)

    db.commit()


@app.before_request
def ensure_db_ready():
    init_db()
    # Manual playoff progression: admin will control when playoffs start and advance
    # auto_start_playoffs_if_ready(db)
    # advance_playoff_winners(db)


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


def build_match_view(match_row, current_week=None, current_playoff_round=None):
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
    rankings = calculate_rankings(db, include_playoffs=True)
    current_week = get_current_week(db)
    return render_template(
        "index.html",
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
            "INSERT INTO players (first_name, last_name, cec_id, created_at) VALUES (%s, %s, %s, %s)",
            (first_name, last_name, cec_id, datetime.now(timezone.utc)),
        )
        db.commit()
        flash("Player registered successfully.", "success")
        return redirect(url_for("signup"))

    players = db.execute(
        "SELECT id, first_name, last_name, cec_id, created_at FROM players ORDER BY created_at ASC"
    ).fetchall()
    return render_template("signup.html", players=players)


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
    return render_template(
        "schedule.html",
        week=week,
        matches=matches,
        current_week=current_week,
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
        return redirect(url_for("index"))

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
        "SELECT id, first_name, last_name, cec_id FROM players ORDER BY created_at ASC"
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
        )
        for row in rows
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
        matches=matches,
        current_week=current_week,
        max_weeks=MAX_WEEKS,
        can_start_playoffs=can_start_playoffs,
        can_advance_round=can_advance_round,
        current_playoff_round=current_playoff_round,
        current_playoff_label=active_playoff_label,
    )


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

    generate_weekly_schedule(db)
    flash("Regular season schedule regenerated and reset to Week 1.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/force_playoffs", methods=["POST"])
@admin_required
def admin_force_playoffs():
    db = get_db()

    # Check if there are enough players
    player_count = db.execute("SELECT COUNT(*) as count FROM players").fetchone()[
        "count"
    ]
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

    # Check if this will cause mass forfeits
    if new_week > current_week:
        # Count how many matches will be auto-forfeited
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

        if matches_to_forfeit > 0:
            # Require confirmation for mass forfeits
            confirmation = request.form.get("confirm_week_jump")
            if confirmation != "yes":
                flash(
                    f"⚠️ WARNING: Advancing from Week {current_week} to Week {new_week} will AUTO-FORFEIT {matches_to_forfeit} unreported match(es). Use the confirmation checkbox to proceed.",
                    "error",
                )
                return redirect(url_for("admin_dashboard"))

    forfeited = 0
    if new_week > current_week:
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
        forfeited = cursor.rowcount
    set_current_week(db, new_week)
    db.commit()
    flash(f"Current week set to {new_week}.", "success")
    if forfeited:
        flash(f"Auto-forfeited {forfeited} unreported matches.", "error")
    return redirect(url_for("admin_dashboard"))


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
        "SELECT id, first_name, last_name FROM players ORDER BY first_name, last_name"
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
    players = db.execute("SELECT id FROM players ORDER BY created_at ASC").fetchall()
    player_ids = [player["id"] for player in players]
    db.execute("DELETE FROM matches WHERE playoff = 0")
    db.commit()
    if len(player_ids) < 2:
        return

    schedule = build_round_robin(player_ids, MAX_WEEKS)
    now = datetime.now(timezone.utc)
    for week_index, matches in enumerate(schedule, start=1):
        for p1, p2 in matches:
            is_bye = p2 is None
            score1 = 0 if is_bye else None
            score2 = 0 if is_bye else None
            reported_flag = 1 if is_bye else 0
            db.execute(
                """
                INSERT INTO matches (week, player1_id, player2_id, score1, score2, reported, playoff, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, 0, %s)
                """,
                (week_index, p1, p2, score1, score2, reported_flag, now),
            )
    set_current_week(db, 1)
    db.commit()


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
