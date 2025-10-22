import os
import secrets
import psycopg2
import psycopg2.extras
from collections import defaultdict
from functools import wraps
from datetime import datetime, timezone
from math import log2, ceil

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
    db = get_db()
    auto_start_playoffs_if_ready(db)
    advance_playoff_winners(db)


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


def build_match_view(match_row, current_week=None):
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
    if current_week is not None:
        week_value = data.get("week")
        if data["is_bye"] or data["double_forfeit"]:
            data["can_report"] = False
        elif data.get("playoff"):
            data["can_report"] = True
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


def label_for_round(round_number):
    if round_number == 1:
        return "Quarterfinals"
    if round_number == 2:
        return "Semifinals"
    if round_number == 3:
        return "Final"
    return f"Round {round_number}"


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


def build_playoff_preview(db):
    rankings = calculate_rankings(db, include_playoffs=False)
    if len(rankings) < 2:
        return []
    names = {
        row["player_id"]: f"{row['first_name']} {row['last_name']}" for row in rankings
    }
    seeds = [row["player_id"] for row in rankings]
    bracket_size = 1 << ceil(log2(len(seeds)))
    if bracket_size < 2:
        bracket_size = 2
    seeds.extend([None] * (bracket_size - len(seeds)))
    pairings = []
    for slot in range(bracket_size // 2):
        p1 = seeds[slot]
        p2 = seeds[-(slot + 1)]
        player1_name = names.get(p1, "TBD") if p1 else "TBD"
        player2_name = names.get(p2, "Bye") if p2 else "Bye"
        match_view = {
            "id": f"preview-{slot}",
            "player1_name": player1_name,
            "player2_name": player2_name if p2 else "BYE",
            "is_bye": p2 is None,
            "reported": False,
            "score_summary": None,
            "can_report": False,
            "double_forfeit": False,
        }
        pairings.append(match_view)
    return [(label_for_round(1), pairings)]


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
        weeks=range(1, MAX_WEEKS + 1),
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
    if match["week"] and not match["playoff"]:
        if match["week"] > current_week:
            flash("This match is not yet open for reporting.", "error")
            return redirect(url_for("view_schedule", week=match["week"]))
        if match["week"] < current_week or match["double_forfeit"]:
            flash("Reporting for this match has closed.", "error")
            return redirect(url_for("view_schedule", week=match["week"]))

    if request.method == "POST":
        # Check for race condition - match might have been reported since page loaded
        current_match = db.execute(
            "SELECT reported, double_forfeit FROM matches WHERE id = %s", (match_id,)
        ).fetchone()

        if current_match["reported"] and not current_match["double_forfeit"]:
            flash("This match was already reported by someone else.", "error")
            return redirect(url_for("view_schedule", week=match.get("week", 1)))

        verified = request.form.get("verified") == "true"
        if not verified:
            flash("Both players must verify the result.", "error")
            return redirect(url_for("report_match", match_id=match_id))

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
            # Use WHERE reported = 0 to prevent duplicate reports
            cursor = db.execute(
                """
                UPDATE matches
                SET score1 = %s, score2 = %s,
                    game1_score1 = %s, game1_score2 = %s,
                    game2_score1 = %s, game2_score2 = %s,
                    game3_score1 = %s, game3_score2 = %s,
                    double_forfeit = 0,
                    reported = 1
                WHERE id = %s AND reported = 0
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

            # Check if the update actually happened
            if cursor.rowcount == 0:
                flash("Match was already reported. No changes made.", "error")
                return redirect(url_for("view_schedule", week=match.get("week", 1)))

            db.commit()
            flash("Match updated.", "success")
            advance_playoff_winners(db)
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
    grouped = []
    if rows:
        by_round = defaultdict(list)
        for match in rows:
            round_number = match["playoff_round"]
            label = label_for_round(round_number)
            by_round[label].append(build_match_view(match))
        grouped = sorted(
            by_round.items(),
            key=lambda item: item[1][0]["playoff_round"] if item[1] else 0,
        )
    preview_rounds = [] if grouped else build_playoff_preview(db)
    champion = get_playoff_champion(db)
    return render_template(
        "playoffs.html",
        rounds=grouped,
        preview_rounds=preview_rounds,
        champion=champion,
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
    matches = [build_match_view(row, current_week=current_week) for row in rows]
    return render_template(
        "admin.html",
        players=players,
        matches=matches,
        current_week=current_week,
        max_weeks=MAX_WEEKS,
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
    db.execute("DELETE FROM matches WHERE playoff = 1")
    db.commit()
    rankings = calculate_rankings(db, include_playoffs=False)
    if len(rankings) < 2:
        return
    seeds = [row["player_id"] for row in rankings]
    bracket_size = 1 << ceil(log2(len(seeds)))
    if bracket_size < 2:
        bracket_size = 2
    byes_needed = bracket_size - len(seeds)
    seeds.extend([None] * byes_needed)
    now = datetime.now(timezone.utc)
    pair_count = bracket_size // 2
    for slot in range(pair_count):
        p1 = seeds[slot]
        p2 = seeds[-(slot + 1)]
        is_bye = p2 is None
        score1 = 0 if is_bye else None
        score2 = 0 if is_bye else None
        reported_flag = 1 if is_bye else 0
        db.execute(
            """
            INSERT INTO matches (week, player1_id, player2_id, score1, score2, reported, playoff, playoff_round, created_at)
            VALUES (NULL, %s, %s, %s, %s, %s, 1, 1, %s)
            """,
            (p1, p2, score1, score2, reported_flag, now),
        )
    db.commit()
    auto_resolve_byes(db)
    advance_playoff_winners(db)


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
        winners = collect_winners(matches)

        # Check for problematic scenarios
        if len(winners) == 0:
            # All matches were double-forfeits or ties - admin needs to intervene
            continue

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
        db.execute(
            """
            INSERT INTO matches (week, player1_id, player2_id, score1, score2, reported, playoff, playoff_round, created_at)
            VALUES (NULL, %s, %s, %s, %s, %s, 1, %s, %s)
            """,
            (p1, p2, score1, score2, reported_flag, round_number, timestamp),
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
    final_match = db.execute(
        """
        SELECT m.*, p1.first_name AS p1_first, p1.last_name AS p1_last,
               p2.first_name AS p2_first, p2.last_name AS p2_last
        FROM matches m
        LEFT JOIN players p1 ON p1.id = m.player1_id
        LEFT JOIN players p2 ON p2.id = m.player2_id
        WHERE m.playoff = 1
        ORDER BY m.playoff_round DESC, m.id DESC
        LIMIT 1
        """
    ).fetchone()
    if not final_match:
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
