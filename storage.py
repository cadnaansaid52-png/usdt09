import os
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

import psycopg2
from psycopg2.extras import RealDictCursor

UNITS_PER_USDT = 10000
DEFAULT_BONUS_UNITS = 500
DEFAULT_BONUS_INTERVAL = 3600
DEFAULT_MIN_WITHDRAWAL_UNITS = 500


def _connect():
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required. Connect USDT09 to its Render PostgreSQL database.")
    return psycopg2.connect(database_url, connect_timeout=10)


@contextmanager
def db_session():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _dict_row(cursor):
    row = cursor.fetchone()
    return dict(row) if row else None


def init_db():
    """Create the PostgreSQL schema and apply additive, non-destructive migrations."""
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    referrer_id BIGINT,
                    verified BOOLEAN NOT NULL DEFAULT FALSE,
                    balance_units BIGINT NOT NULL DEFAULT 0 CHECK (balance_units >= 0),
                    last_bonus_at BIGINT,
                    is_banned BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            # Additive migrations also make upgrades safe for an earlier PostgreSQL schema.
            migrations = [
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_id BIGINT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS balance_units BIGINT NOT NULL DEFAULT 0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_bonus_at BIGINT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            ]
            for statement in migrations:
                cur.execute(statement)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                    amount_units BIGINT NOT NULL CHECK (amount_units > 0),
                    network TEXT NOT NULL,
                    wallet_address TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    txid TEXT,
                    admin_id BIGINT,
                    admin_note TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS balance_ledger (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                    amount_units BIGINT NOT NULL,
                    reason TEXT NOT NULL,
                    admin_id BIGINT,
                    reference TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_audit (
                    id BIGSERIAL PRIMARY KEY,
                    admin_id BIGINT NOT NULL,
                    action TEXT NOT NULL,
                    target_user_id BIGINT,
                    details TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_referrer_verified ON users(referrer_id, verified)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_withdrawals_user_created ON withdrawals(telegram_id, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_withdrawals_status_created ON withdrawals(status, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ledger_user_created ON balance_ledger(telegram_id, created_at DESC)")
            defaults = {
                "bonus_units": str(DEFAULT_BONUS_UNITS),
                "bonus_interval_seconds": str(DEFAULT_BONUS_INTERVAL),
                "min_withdrawal_units": str(DEFAULT_MIN_WITHDRAWAL_UNITS),
                "maintenance": "OFF",
            }
            for key, value in defaults.items():
                cur.execute(
                    "INSERT INTO settings (setting_key, setting_value) VALUES (%s, %s) ON CONFLICT (setting_key) DO NOTHING",
                    (key, value),
                )


def get_setting(key, default=None):
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT setting_value FROM settings WHERE setting_key = %s", (str(key),))
            row = cur.fetchone()
            return row[0] if row else default


def update_setting(key, value):
    allowed = {"bonus_units", "bonus_interval_seconds", "min_withdrawal_units", "maintenance"}
    if key not in allowed:
        raise ValueError("Unknown setting.")
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO settings (setting_key, setting_value) VALUES (%s, %s)
                ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value
            """, (str(key), str(value)))


def get_settings():
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT setting_key, setting_value FROM settings")
            values = {str(key): value for key, value in cur.fetchall()}
    return {
        "bonus_units": int(values.get("bonus_units", DEFAULT_BONUS_UNITS)),
        "bonus_interval_seconds": int(values.get("bonus_interval_seconds", DEFAULT_BONUS_INTERVAL)),
        "min_withdrawal_units": int(values.get("min_withdrawal_units", DEFAULT_MIN_WITHDRAWAL_UNITS)),
        "maintenance": values.get("maintenance", "OFF"),
    }


def register_user(telegram_id, username, first_name, last_name, referrer_id=None, verified=False):
    now = int(time.time())
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_id FROM users WHERE telegram_id = %s", (int(telegram_id),))
            exists = cur.fetchone()
            if exists:
                cur.execute("""
                    UPDATE users
                    SET username = %s, first_name = %s, last_name = %s,
                        verified = CASE WHEN %s THEN TRUE ELSE verified END
                    WHERE telegram_id = %s
                """, (username, first_name or "", last_name or "", bool(verified), int(telegram_id)))
                return False

            safe_referrer = None
            if referrer_id and int(referrer_id) != int(telegram_id):
                cur.execute("SELECT telegram_id FROM users WHERE telegram_id = %s AND verified = TRUE AND is_banned = FALSE", (int(referrer_id),))
                if cur.fetchone():
                    safe_referrer = int(referrer_id)

            cur.execute("""
                INSERT INTO users (
                    telegram_id, username, first_name, last_name, referrer_id,
                    verified, balance_units, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 0, TO_TIMESTAMP(%s))
            """, (int(telegram_id), username, first_name or "", last_name or "", safe_referrer, bool(verified), now))
            return True


def mark_verified(telegram_id, username, first_name, last_name):
    register_user(telegram_id, username, first_name, last_name, verified=True)
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET verified = TRUE WHERE telegram_id = %s", (int(telegram_id),))


def get_user(telegram_id):
    with db_session() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE telegram_id = %s", (int(telegram_id),))
            return _dict_row(cur)


def is_user_banned(telegram_id):
    user = get_user(telegram_id)
    return bool(user and user.get("is_banned"))


def get_balance_units(telegram_id):
    user = get_user(telegram_id)
    return int(user["balance_units"]) if user else 0


def parse_amount_units(raw_amount):
    try:
        amount = Decimal(str(raw_amount))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    scaled = amount * Decimal(UNITS_PER_USDT)
    if scaled != scaled.to_integral_value():
        return None
    return int(scaled)


def format_units(units, places=4):
    return f"{int(units) / UNITS_PER_USDT:.{places}f}"


def claim_hourly_bonus(telegram_id, now=None):
    now = int(now or time.time())
    settings = get_settings()
    with db_session() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE telegram_id = %s FOR UPDATE", (int(telegram_id),))
            user = _dict_row(cur)
            if not user or not user["verified"] or user["is_banned"]:
                return "not_verified", 0, 0
            last = user["last_bonus_at"]
            interval = max(60, settings["bonus_interval_seconds"])
            if last is not None:
                remaining = interval - (now - int(last))
                if remaining > 0:
                    return "cooldown", int(user["balance_units"]), remaining

            amount = settings["bonus_units"]
            new_balance = int(user["balance_units"]) + amount
            cur.execute(
                "UPDATE users SET balance_units = %s, last_bonus_at = %s WHERE telegram_id = %s",
                (new_balance, now, int(telegram_id)),
            )
            cur.execute("""
                INSERT INTO balance_ledger (telegram_id, amount_units, reason, reference)
                VALUES (%s, %s, 'hourly_bonus', %s)
            """, (int(telegram_id), amount, f"bonus-{now}"))
            return "claimed", new_balance, 0


def get_referral_count(telegram_id):
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM users WHERE referrer_id = %s AND verified = TRUE AND is_banned = FALSE",
                (int(telegram_id),),
            )
            return int(cur.fetchone()[0])


def get_leaderboard(limit=10):
    limit = max(1, min(int(limit), 50))
    with db_session() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT u.telegram_id, u.username, u.first_name, u.last_name,
                       COUNT(r.telegram_id) AS referrals
                FROM users AS u
                JOIN users AS r ON r.referrer_id = u.telegram_id
                WHERE u.verified = TRUE AND u.is_banned = FALSE
                  AND r.verified = TRUE AND r.is_banned = FALSE
                GROUP BY u.telegram_id
                HAVING COUNT(r.telegram_id) > 0
                ORDER BY referrals DESC, u.created_at ASC
                LIMIT %s
            """, (limit,))
            return [dict(row) for row in cur.fetchall()]


def create_withdrawal(telegram_id, amount_units, network, wallet_address):
    reference = uuid.uuid4().hex[:12].upper()
    settings = get_settings()
    with db_session() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE telegram_id = %s FOR UPDATE", (int(telegram_id),))
            user = _dict_row(cur)
            if not user or not user["verified"] or user["is_banned"]:
                return "not_verified", None, 0
            if amount_units < settings["min_withdrawal_units"]:
                return "below_minimum", None, int(user["balance_units"])
            if amount_units > int(user["balance_units"]):
                return "insufficient", None, int(user["balance_units"])

            new_balance = int(user["balance_units"]) - int(amount_units)
            cur.execute("UPDATE users SET balance_units = %s WHERE telegram_id = %s", (new_balance, int(telegram_id)))
            cur.execute("""
                INSERT INTO withdrawals (telegram_id, amount_units, network, wallet_address, status)
                VALUES (%s, %s, %s, %s, 'pending') RETURNING id
            """, (int(telegram_id), int(amount_units), str(network).upper(), str(wallet_address)))
            withdrawal_id = int(cur.fetchone()["id"])
            cur.execute("""
                INSERT INTO balance_ledger (telegram_id, amount_units, reason, reference)
                VALUES (%s, %s, 'withdrawal_reserved', %s)
            """, (int(telegram_id), -int(amount_units), str(withdrawal_id)))
            return "created", withdrawal_id, new_balance


def get_user_withdrawals(telegram_id, limit=10):
    limit = max(1, min(int(limit), 30))
    with db_session() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, amount_units, network, wallet_address, status, txid,
                       admin_note, created_at, updated_at
                FROM withdrawals WHERE telegram_id = %s
                ORDER BY created_at DESC LIMIT %s
            """, (int(telegram_id), limit))
            return [dict(row) for row in cur.fetchall()]


def get_withdrawals(status="pending", limit=15):
    allowed = {"pending", "approved", "rejected", "paid", "all"}
    if status not in allowed:
        raise ValueError("Invalid withdrawal status.")
    limit = max(1, min(int(limit), 50))
    with db_session() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            where = "" if status == "all" else "WHERE w.status = %s"
            params = (limit,) if status == "all" else (status, limit)
            cur.execute(f"""
                SELECT w.*, u.username, u.first_name, u.last_name
                FROM withdrawals w
                JOIN users u ON u.telegram_id = w.telegram_id
                {where}
                ORDER BY w.created_at DESC LIMIT %s
            """, params)
            return [dict(row) for row in cur.fetchall()]


def get_withdrawal(withdrawal_id):
    with db_session() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT w.*, u.username, u.first_name, u.last_name, u.balance_units
                FROM withdrawals w
                JOIN users u ON u.telegram_id = w.telegram_id
                WHERE w.id = %s
            """, (int(withdrawal_id),))
            return _dict_row(cur)


def set_withdrawal_status(withdrawal_id, new_status, admin_id, note="", txid=None):
    if new_status not in {"approved", "rejected", "paid"}:
        return "invalid"
    with db_session() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM withdrawals WHERE id = %s FOR UPDATE", (int(withdrawal_id),))
            withdrawal = _dict_row(cur)
            if not withdrawal:
                return "not_found"
            current = withdrawal["status"]
            allowed = {
                "approved": {"pending"},
                "rejected": {"pending", "approved"},
                "paid": {"approved"},
            }
            if current not in allowed[new_status]:
                return "invalid_transition"
            if new_status == "paid" and not (txid and 10 <= len(txid.strip()) <= 200):
                return "txid_required"

            if new_status == "rejected":
                cur.execute("SELECT balance_units FROM users WHERE telegram_id = %s FOR UPDATE", (int(withdrawal["telegram_id"]),))
                row = cur.fetchone()
                refunded = int(row["balance_units"]) + int(withdrawal["amount_units"])
                cur.execute("UPDATE users SET balance_units = %s WHERE telegram_id = %s", (refunded, int(withdrawal["telegram_id"])))
                cur.execute("""
                    INSERT INTO balance_ledger (telegram_id, amount_units, reason, admin_id, reference)
                    VALUES (%s, %s, 'withdrawal_refund', %s, %s)
                """, (int(withdrawal["telegram_id"]), int(withdrawal["amount_units"]), int(admin_id), str(withdrawal_id)))

            cur.execute("""
                UPDATE withdrawals
                SET status = %s, admin_id = %s, admin_note = %s,
                    txid = COALESCE(%s, txid), updated_at = NOW()
                WHERE id = %s
            """, (new_status, int(admin_id), str(note or ""), txid.strip() if txid else None, int(withdrawal_id)))
            cur.execute("""
                INSERT INTO admin_audit (admin_id, action, target_user_id, details)
                VALUES (%s, %s, %s, %s)
            """, (int(admin_id), f"withdrawal_{new_status}", int(withdrawal["telegram_id"]), f"id={withdrawal_id}; amount={withdrawal['amount_units']}"))
            return "ok"


def get_user_list(limit=15):
    limit = max(1, min(int(limit), 50))
    with db_session() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT telegram_id, username, first_name, last_name, balance_units,
                       verified, is_banned, created_at
                FROM users ORDER BY created_at DESC LIMIT %s
            """, (limit,))
            return [dict(row) for row in cur.fetchall()]


def search_users(query, limit=15):
    query = str(query).strip()
    limit = max(1, min(int(limit), 50))
    with db_session() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if query.isdigit():
                cur.execute("""
                    SELECT telegram_id, username, first_name, last_name, balance_units,
                           verified, is_banned, created_at
                    FROM users WHERE telegram_id = %s LIMIT %s
                """, (int(query), limit))
            else:
                cur.execute("""
                    SELECT telegram_id, username, first_name, last_name, balance_units,
                           verified, is_banned, created_at
                    FROM users WHERE username ILIKE %s
                    ORDER BY created_at DESC LIMIT %s
                """, ("%" + query.lstrip("@") + "%", limit))
            return [dict(row) for row in cur.fetchall()]


def set_user_banned(telegram_id, banned, admin_id):
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_banned = %s WHERE telegram_id = %s", (bool(banned), int(telegram_id)))
            changed = cur.rowcount > 0
            if changed:
                cur.execute("""
                    INSERT INTO admin_audit (admin_id, action, target_user_id, details)
                    VALUES (%s, %s, %s, %s)
                """, (int(admin_id), "user_ban" if banned else "user_unban", int(telegram_id), ""))
            return changed


def adjust_balance(telegram_id, amount_units, reason, admin_id):
    if amount_units == 0:
        return "invalid", 0
    with db_session() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT balance_units FROM users WHERE telegram_id = %s FOR UPDATE", (int(telegram_id),))
            user = _dict_row(cur)
            if not user:
                return "not_found", 0
            new_balance = int(user["balance_units"]) + int(amount_units)
            if new_balance < 0:
                return "insufficient", int(user["balance_units"])
            cur.execute("UPDATE users SET balance_units = %s WHERE telegram_id = %s", (new_balance, int(telegram_id)))
            cur.execute("""
                INSERT INTO balance_ledger (telegram_id, amount_units, reason, admin_id, reference)
                VALUES (%s, %s, %s, %s, NULL)
            """, (int(telegram_id), int(amount_units), str(reason), int(admin_id)))
            cur.execute("""
                INSERT INTO admin_audit (admin_id, action, target_user_id, details)
                VALUES (%s, %s, %s, %s)
            """, (int(admin_id), "balance_adjustment", int(telegram_id), f"delta={amount_units}; reason={reason}"))
            return "ok", new_balance


def get_dashboard_stats():
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE created_at::date = CURRENT_DATE),
                       COUNT(*) FILTER (WHERE is_banned = TRUE),
                       COALESCE(SUM(balance_units), 0)
                FROM users
            """)
            users = cur.fetchone()
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'pending'),
                    COALESCE(SUM(amount_units) FILTER (WHERE status = 'pending'), 0),
                    COUNT(*) FILTER (WHERE status = 'approved'),
                    COALESCE(SUM(amount_units) FILTER (WHERE status = 'approved'), 0),
                    COUNT(*) FILTER (WHERE status = 'paid'),
                    COALESCE(SUM(amount_units) FILTER (WHERE status = 'paid'), 0),
                    COUNT(*) FILTER (WHERE status = 'rejected'),
                    COALESCE(SUM(amount_units) FILTER (WHERE status = 'rejected'), 0)
                FROM withdrawals
            """)
            wd = cur.fetchone()
    return {
        "total_users": int(users[0] or 0),
        "joined_today": int(users[1] or 0),
        "banned_users": int(users[2] or 0),
        "available_balance_units": int(users[3] or 0),
        "pending_count": int(wd[0] or 0),
        "pending_units": int(wd[1] or 0),
        "approved_count": int(wd[2] or 0),
        "approved_units": int(wd[3] or 0),
        "paid_count": int(wd[4] or 0),
        "paid_units": int(wd[5] or 0),
        "rejected_count": int(wd[6] or 0),
        "rejected_units": int(wd[7] or 0),
    }
