"""
Database adapter to support both SQLite (local dev) and PostgreSQL (Render production)
"""

import os

# Check if we should use PostgreSQL (Render provides DATABASE_URL)
DATABASE_URL = os.getenv("DATABASE_URL")
USE_POSTGRES = DATABASE_URL is not None and DATABASE_URL.strip() != ""

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

    # Render provides DATABASE_URL in postgres:// format, psycopg2 needs postgresql://
    if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


def get_db_connection():
    """Get a database connection (SQLite or PostgreSQL)"""
    if USE_POSTGRES:
        conn = psycopg2.connect(
            DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
        )
        return conn
    else:
        import sqlite3

        BASE_DIR = os.path.abspath(os.path.dirname(__file__))
        DATABASE = os.path.join(BASE_DIR, "league.db")
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        return conn


def init_schema(conn):
    """Initialize database schema (works for both SQLite and PostgreSQL)"""
    cursor = conn.cursor()

    # Players table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS players (
            id SERIAL PRIMARY KEY,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            cec_id TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """
        if USE_POSTGRES
        else """
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            cec_id TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """
    )

    # Matches table
    cursor.execute(
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
        if USE_POSTGRES
        else """
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    # Settings table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # Create indexes
    if USE_POSTGRES:
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_matches_week ON matches(week)")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_matches_playoff ON matches(playoff)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_matches_reported ON matches(reported)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_matches_players ON matches(player1_id, player2_id)"
        )
    else:
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_matches_week ON matches(week)")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_matches_playoff ON matches(playoff)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_matches_reported ON matches(reported)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_matches_players ON matches(player1_id, player2_id)"
        )

    conn.commit()


class DictRow:
    """Wrapper to make PostgreSQL rows behave like SQLite Row objects"""

    def __init__(self, row_dict):
        self._data = row_dict

    def __getitem__(self, key):
        return self._data[key]

    def __contains__(self, key):
        return key in self._data

    def keys(self):
        return self._data.keys()

    def get(self, key, default=None):
        return self._data.get(key, default)


def dict_factory(cursor, row):
    """Convert row to dict for consistent access pattern"""
    if USE_POSTGRES:
        return DictRow(dict(row))
    else:
        return {k[0]: row[i] for i, k in enumerate(cursor.description)}
