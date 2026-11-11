import os
import sqlite3
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Optional

DB_PATH = os.getenv("DATABASE_PATH", "usdt09.sqlite3")
BONUS_UNITS = 500       # 0.0500 USDT, demo credits only (1 unit = 0.0001)
MIN_WITHDRAWAL_UNITS = 500


def _connect():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 20000")
    return conn


def init_db():
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                referrer_id INTEGER,
                verified INTEGER NOT NULL DEFAULT 0,
                balance_units INTEGER NOT NULL DEFAULT 0,
                last_bonus_at INTEGER,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS demo_withdrawals (
                id TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL,
                amount_units INTEGER NOT NULL,
                wallet_address TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'demo_recorded',
                created_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_users_referrer_verified
                ON users(referrer_id, verified);
            CREATE INDEX IF NOT EXISTS idx_withdrawals_user
                ON demo_withdrawals(telegram_id, created_at);
            """
        )


def register_user(
    telegram_id: int,
    username: Optional[str],
    first_name: str,
    last_name: str,
    referrer_id: Optional[int] = None,
    verified: bool = False,
):
    """Create a user once; only accept an existing, verified referrer."""
    now = int(time.time())
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT telegram_id FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if exists:
            conn.execute(
                """
                UPDATE users
                SET username = ?, first_name = ?, last_name = ?,
                    verified = CASE WHEN ? = 1 THEN 1 ELSE verified END
                WHERE telegram_id = ?
                """,
                (username, first_name or "", last_name or "", int(verified), telegram_id),
            )
            conn.commit()
            return False

        safe_referrer = None
        if referrer_id and referrer_id != telegram_id:
            ref = conn.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = ? AND verified = 1",
                (referrer_id,),
            ).fetchone()
            if ref:
                safe_referrer = referrer_id

        conn.execute(
            """
            INSERT INTO users (
                telegram_id, username, first_name, last_name,
                referrer_id, verified, balance_units, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                telegram_id, username, first_name or "", last_name or "",
                safe_referrer, int(verified), now,
            ),
        )
        conn.commit()
        return True


def mark_verified(telegram_id: int, username: Optional[str], first_name: str, last_name: str):
    register_user(
        telegram_id=telegram_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        verified=True,
    )
    with _connect() as conn:
        conn.execute("UPDATE users SET verified = 1 WHERE telegram_id = ?", (telegram_id,))
        conn.commit()


def get_user(telegram_id: int):
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()


def get_balance_units(telegram_id: int) -> int:
    user = get_user(telegram_id)
    return int(user["balance_units"]) if user else 0


def claim_hourly_bonus(telegram_id: int, now: Optional[int] = None):
    """Return (status, balance_units, seconds_left)."""
    now = int(now or time.time())
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute(
            "SELECT balance_units, last_bonus_at, verified FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if not user or not user["verified"]:
            conn.rollback()
            return "not_verified", 0, 0

        last = user["last_bonus_at"]
        if last is not None:
            remaining = 3600 - (now - int(last))
            if remaining > 0:
                conn.rollback()
                return "cooldown", int(user["balance_units"]), remaining

        new_balance = int(user["balance_units"]) + BONUS_UNITS
        conn.execute(
            """
            UPDATE users
            SET balance_units = ?, last_bonus_at = ?
            WHERE telegram_id = ?
            """,
            (new_balance, now, telegram_id),
        )
        conn.commit()
        return "claimed", new_balance, 0


def get_referral_count(telegram_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total FROM users
            WHERE referrer_id = ? AND verified = 1
            """,
            (telegram_id,),
        ).fetchone()
        return int(row["total"])


def get_leaderboard(limit: int = 10):
    with _connect() as conn:
        return conn.execute(
            """
            SELECT u.telegram_id, u.username, u.first_name, u.last_name,
                   COUNT(r.telegram_id) AS referrals
            FROM users AS u
            JOIN users AS r ON r.referrer_id = u.telegram_id AND r.verified = 1
            WHERE u.verified = 1
            GROUP BY u.telegram_id
            HAVING COUNT(r.telegram_id) > 0
            ORDER BY referrals DESC, u.created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def parse_amount_units(raw_amount: str):
    try:
        amount = Decimal(raw_amount)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    scaled = amount * Decimal(10000)
    if scaled != scaled.to_integral_value():
        return None
    return int(scaled)


def create_demo_withdrawal(telegram_id: int, amount_units: int, wallet_address: str):
    """Deduct demo credits and create a clearly simulated receipt; never sends crypto."""
    now = int(time.time())
    reference = uuid.uuid4().hex[:12].upper()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute(
            "SELECT balance_units, verified FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if not user or not user["verified"]:
            conn.rollback()
            return "not_verified", None, 0
        if amount_units < MIN_WITHDRAWAL_UNITS:
            conn.rollback()
            return "below_minimum", None, int(user["balance_units"])
        if amount_units > int(user["balance_units"]):
            conn.rollback()
            return "insufficient", None, int(user["balance_units"])

        new_balance = int(user["balance_units"]) - amount_units
        conn.execute(
            "UPDATE users SET balance_units = ? WHERE telegram_id = ?",
            (new_balance, telegram_id),
        )
        conn.execute(
            """
            INSERT INTO demo_withdrawals
                (id, telegram_id, amount_units, wallet_address, status, created_at)
            VALUES (?, ?, ?, ?, 'demo_recorded', ?)
            """,
            (reference, telegram_id, amount_units, wallet_address, now),
        )
        conn.commit()
        return "created", reference, new_balance


def get_demo_receipt(reference: str):
    with _connect() as conn:
        return conn.execute(
            """
            SELECT w.id, w.telegram_id, w.amount_units, w.wallet_address,
                   w.status, w.created_at,
                   u.username, u.first_name, u.last_name
            FROM demo_withdrawals AS w
            JOIN users AS u ON u.telegram_id = w.telegram_id
            WHERE w.id = ?
            """,
            (reference.upper(),),
        ).fetchone()
