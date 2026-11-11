import asyncio
import html
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from storage import (
    BONUS_UNITS,
    MIN_WITHDRAWAL_UNITS,
    claim_hourly_bonus,
    create_demo_withdrawal,
    get_balance_units,
    get_demo_receipt,
    get_leaderboard,
    get_referral_count,
    get_user,
    init_db,
    mark_verified,
    parse_amount_units,
    register_user,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("usdt09")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
FORCE_CHANNELS_RAW = os.getenv("FORCE_CHANNELS", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    or WEBHOOK_URL
    or "https://usdt09.onrender.com"
)
DEMO_PROOF_CHAT_ID = os.getenv("DEMO_PROOF_CHAT_ID", "").strip()

WEBHOOK_PATH = "/webhook"
BONUS_INTERVAL_SECONDS = 3600
pending_withdrawals: set[int] = set()
active_bot: Optional[Bot] = None
polling_task: Optional[asyncio.Task] = None
bot_username = ""


@dataclass(frozen=True)
class RequiredChannel:
    chat_id: str
    join_url: str
    title: str


def parse_force_channels(raw_value: str) -> list[RequiredChannel]:
    channels: list[RequiredChannel] = []

    for entry in raw_value.split(";"):
        entry = entry.strip()
        if not entry:
            continue

        parts = [part.strip() for part in entry.split("|")]
        raw_chat_id = parts[0]
        if not raw_chat_id:
            continue

        supplied_join_url = parts[1] if len(parts) >= 2 else ""
        if raw_chat_id.startswith(("@", "https://t.me/", "http://t.me/")):
            if raw_chat_id.startswith("@"):
                username = raw_chat_id[1:].strip().strip("/")
                chat_id = f"@{username}"
                join_url = supplied_join_url or f"https://t.me/{username}"
            else:
                parsed = urlsplit(raw_chat_id)
                path = parsed.path.strip("/")
                if not path or path.startswith("+") or path.startswith("joinchat/"):
                    logger.warning("Private invite links need a numeric channel ID in FORCE_CHANNELS.")
                    continue
                username = path.split("/", 1)[0]
                chat_id = f"@{username}"
                join_url = supplied_join_url or f"https://t.me/{username}"
        else:
            chat_id = raw_chat_id
            join_url = supplied_join_url
            if not join_url:
                logger.warning("Private channel IDs need an invite URL in FORCE_CHANNELS.")
                continue

        title = parts[2] if len(parts) >= 3 and parts[2] else chat_id
        if not join_url.startswith(("https://t.me/", "http://t.me/")):
            logger.warning("Skipping channel %s: join URL must be a t.me link.", chat_id)
            continue
        channels.append(RequiredChannel(chat_id, join_url, title))

    return channels


REQUIRED_CHANNELS = parse_force_channels(FORCE_CHANNELS_RAW)
dp = Dispatcher()


# ---------------------------------------------------------------------------
# Formatting and keyboard helpers
# ---------------------------------------------------------------------------

def format_units(units: int, places: int = 4) -> str:
    return f"{units / 10000:.{places}f}"


def format_remaining(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    balance = get_balance_units(user_id)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=f"💰 {balance / 10000:.2f} USDT")],
            [
                KeyboardButton(text="🎁 Bonus"),
                KeyboardButton(text="💸 Withdraw"),
            ],
            [
                KeyboardButton(text="👥 Referrals"),
                KeyboardButton(text="🏆 Leaderboard"),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Choose an option",
    )


def force_join_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"📢 Join channel · {ch.title}", url=ch.join_url)]
        for ch in REQUIRED_CHANNELS
    ]
    rows.append([InlineKeyboardButton(text="✅ Verify", callback_data="verify_subscription")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def receipt_keyboard(reference: str) -> InlineKeyboardMarkup:
    url = f"{PUBLIC_BASE_URL}/demo/receipt/{reference}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧾 View demo receipt", url=url)]
        ]
    )


def welcome_text(user_id: int) -> str:
    balance = format_units(get_balance_units(user_id))
    return (
        "✅ <b>Channel verified!</b>\n\n"
        "👋 Welcome to <b>USDT09</b> 💎\n\n"
        f"💰 Balance: <b>{balance} USDT</b>\n"
        "🎁 Hourly demo bonus: <b>0.0500 USDT</b>\n\n"
        "🧪 <b>DEMO MODE:</b> All balances and rewards are simulated. "
        "No real USDT is held or paid.\n\n"
        "Choose an option below."
    )


async def get_membership_status(bot: Bot, user_id: int):
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel.chat_id, user_id=user_id)
        except TelegramAPIError:
            logger.exception("Membership check failed for user %s in %s", user_id, channel.chat_id)
            return "error", channel

        is_member = member.status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
        if member.status == ChatMemberStatus.RESTRICTED:
            is_member = bool(getattr(member, "is_member", False))
        if not is_member:
            return "missing", channel
    return "joined", None


def register_message_user(message: Message, referrer_id: Optional[int] = None, verified: bool = False):
    user = message.from_user
    if not user:
        return
    register_user(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name or "",
        last_name=user.last_name or "",
        referrer_id=referrer_id,
        verified=verified,
    )


async def ensure_access(message: Message, bot: Bot) -> bool:
    if not message.from_user:
        return False
    status, _ = await get_membership_status(bot, message.from_user.id)
    if status == "missing":
        await message.answer(
            "🔒 <b>Join our channel first</b>\n\n"
            "Join the channel below, then tap ✅ Verify.",
            reply_markup=force_join_keyboard(),
            parse_mode="HTML",
        )
        return False
    if status == "error":
        await message.answer(
            "⚠️ Membership verification is temporarily unavailable. Please try again."
        )
        return False
    user = message.from_user
    mark_verified(user.id, user.username, user.first_name or "", user.last_name or "")
    return True


# ---------------------------------------------------------------------------
# /start and channel force
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def handle_start(message: Message, command: CommandObject, bot: Bot):
    user = message.from_user
    if not user:
        return

    referrer_id = None
    args = (command.args or "").strip()
    match = re.fullmatch(r"ref_(\d+)", args)
    if match:
        referrer_id = int(match.group(1))

    status, _channel = await get_membership_status(bot, user.id)
    register_user(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name or "",
        last_name=user.last_name or "",
        referrer_id=referrer_id,
        verified=(status == "joined"),
    )

    if status == "joined":
        await message.answer(
            welcome_text(user.id),
            reply_markup=main_keyboard(user.id),
            parse_mode="HTML",
        )
    elif status == "missing":
        await message.answer(
            "🔒 <b>Join our channel first</b>\n\n"
            "Join the channel below, then tap ✅ Verify.",
            reply_markup=force_join_keyboard(),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "⚠️ I couldn't verify your channel membership. "
            "Please try again later or contact the bot administrator."
        )


@dp.callback_query(F.data == "verify_subscription")
async def handle_verify(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        await callback.answer()
        return

    status, _channel = await get_membership_status(bot, user.id)
    if status == "missing":
        await callback.answer("Please join every required channel first.", show_alert=True)
        return
    if status == "error":
        await callback.answer("Verification is temporarily unavailable.", show_alert=True)
        return

    mark_verified(user.id, user.username, user.first_name or "", user.last_name or "")
    await callback.answer("✅ Verified!")
    if callback.message:
        try:
            await callback.message.delete()
        except TelegramAPIError:
            logger.debug("Could not delete old join prompt.")
        await callback.message.answer(
            welcome_text(user.id),
            reply_markup=main_keyboard(user.id),
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# Main menu features — simulation only, no real crypto payout
# ---------------------------------------------------------------------------

@dp.message(F.text.startswith("💰"))
async def handle_balance(message: Message, bot: Bot):
    if not message.from_user or not await ensure_access(message, bot):
        return
    balance = format_units(get_balance_units(message.from_user.id))
    await message.answer(
        "💰 <b>Your balance</b>\n\n"
        f"<code>{balance} USDT</code>\n\n"
        "🧪 This is a simulated demo balance only. It is not real USDT "
        "and cannot be transferred or redeemed.",
        reply_markup=main_keyboard(message.from_user.id),
        parse_mode="HTML",
    )


@dp.message(F.text == "🎁 Bonus")
async def handle_bonus(message: Message, bot: Bot):
    if not message.from_user or not await ensure_access(message, bot):
        return

    status, balance, remaining = claim_hourly_bonus(message.from_user.id)
    if status == "claimed":
        await message.answer(
            "🎉 <b>Hourly demo bonus claimed!</b>\n\n"
            "💰 <b>+0.0500 USDT</b> added to your demo balance.\n"
            "⏳ Next bonus available in <b>60m 00s</b>.\n\n"
            "🧪 Demo credits only — no real USDT is paid.",
            reply_markup=main_keyboard(message.from_user.id),
            parse_mode="HTML",
        )
    elif status == "cooldown":
        await message.answer(
            "⏳ <b>Bonus already claimed!</b>\n\n"
            f"Please come back in <b>{format_remaining(remaining)}</b>.\n"
            "Your next hourly demo bonus will then be available.",
            reply_markup=main_keyboard(message.from_user.id),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "Please complete Channel Force verification before claiming the bonus."
        )


@dp.message(F.text == "💸 Withdraw")
async def handle_withdraw(message: Message, bot: Bot):
    if not message.from_user or not await ensure_access(message, bot):
        return

    pending_withdrawals.add(message.from_user.id)
    balance = format_units(get_balance_units(message.from_user.id))
    await message.answer(
        "💸 <b>Demo withdrawal</b>\n\n"
        f"Balance: <b>{balance} USDT</b>\n"
        "Minimum: <b>0.05 USDT</b>\n\n"
        "Send the amount and your USDT address in one message:\n\n"
        "<code>0.05 UQYourWalletAddress...</code>\n\n"
        "🧪 <b>DEMO ONLY:</b> This records a simulated request and does not send "
        "USDT or create a blockchain transaction.\n\n"
        "Send /cancel to cancel.",
        reply_markup=main_keyboard(message.from_user.id),
        parse_mode="HTML",
    )


@dp.message(Command("cancel"))
async def handle_cancel(message: Message):
    if message.from_user:
        pending_withdrawals.discard(message.from_user.id)
    await message.answer("✅ Demo withdrawal cancelled.", reply_markup=main_keyboard(message.from_user.id) if message.from_user else None)


@dp.message(F.text == "👥 Referrals")
async def handle_referrals(message: Message, bot: Bot):
    if not message.from_user or not await ensure_access(message, bot):
        return

    me = await bot.get_me()
    count = get_referral_count(message.from_user.id)
    invite_link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    await message.answer(
        "👥 <b>Your invitation link</b>\n\n"
        "Invite friends to join USDT09 and appear on the referral leaderboard.\n\n"
        f"🔗 <code>{html.escape(invite_link)}</code>\n\n"
        f"✅ Verified referrals: <b>{count}</b>\n\n"
        "🧪 Referral statistics are for the demo preview. No cash rewards are promised.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📤 Share invite link", url=invite_link)]]
        ),
        parse_mode="HTML",
    )


@dp.message(F.text == "🏆 Leaderboard")
async def handle_leaderboard(message: Message, bot: Bot):
    if not message.from_user or not await ensure_access(message, bot):
        return

    rows = get_leaderboard(10)
    if not rows:
        text = (
            "🏆 <b>Top referrers</b>\n\n"
            "No verified referrals yet. Invite friends to be the first on the leaderboard!"
        )
    else:
        medals = ["🥇", "🥈", "🥉"]
        lines = ["🏆 <b>Top referrers</b>", ""]
        for index, row in enumerate(rows, start=1):
            name = " ".join(
                part for part in [row["first_name"], row["last_name"]] if part
            ).strip()
            if not name and row["username"]:
                name = "@" + row["username"]
            if not name:
                name = f"User {row['telegram_id']}"
            medal = medals[index - 1] if index <= 3 else f"{index}."
            lines.append(
                f"{medal} {html.escape(name)} — <b>{row['referrals']}</b>"
            )
        lines.extend(["", "🧪 Demo leaderboard based on verified invitations."])
        text = "\n".join(lines)

    await message.answer(
        text,
        reply_markup=main_keyboard(message.from_user.id),
        parse_mode="HTML",
    )


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text_fallback(message: Message, bot: Bot):
    if not message.from_user:
        return

    user_id = message.from_user.id
    if user_id in pending_withdrawals:
        if not await ensure_access(message, bot):
            return

        parts = (message.text or "").strip().split()
        if len(parts) != 2:
            await message.answer(
                "⚠️ Please send exactly two values:\n"
                "<code>0.05 UQYourWalletAddress...</code>\n"
                "Use /cancel to cancel.",
                parse_mode="HTML",
            )
            return

        amount_units = parse_amount_units(parts[0])
        wallet_address = parts[1]
        if amount_units is None:
            await message.answer("⚠️ Invalid amount. Use up to 4 decimal places, e.g. <code>0.05</code>.", parse_mode="HTML")
            return
        if not (10 <= len(wallet_address) <= 120) or any(ch.isspace() for ch in wallet_address):
            await message.answer("⚠️ Please enter a valid wallet address (10–120 characters, no spaces).")
            return

        result, reference, new_balance = create_demo_withdrawal(
            telegram_id=user_id,
            amount_units=amount_units,
            wallet_address=wallet_address,
        )
        if result == "below_minimum":
            await message.answer("⚠️ Minimum demo withdrawal is <b>0.05 USDT</b>.", parse_mode="HTML")
            return
        if result == "insufficient":
            await message.answer(
                f"⚠️ Insufficient demo balance. Available: <b>{format_units(new_balance)} USDT</b>.",
                parse_mode="HTML",
            )
            return
        if result != "created":
            await message.answer("⚠️ Please complete verification before submitting a request.")
            pending_withdrawals.discard(user_id)
            return

        pending_withdrawals.discard(user_id)
        display_name = " ".join(
            part for part in [message.from_user.first_name, message.from_user.last_name] if part
        ).strip() or (f"@{message.from_user.username}" if message.from_user.username else f"User {user_id}")
        safe_name = html.escape(display_name)
        amount_text = format_units(amount_units)
        receipt_url = f"{PUBLIC_BASE_URL}/demo/receipt/{reference}"

        group_message = (
            "🎭 <b>DEMO withdrawal request recorded</b>\n\n"
            "⚠️ <b>NO REAL USDT TRANSFER WAS MADE</b>\n\n"
            f"👤 {safe_name}\n"
            f"💰 <b>{amount_text} DEMO USDT</b>\n"
            f"🧾 Reference: <code>{reference}</code>\n\n"
            "This is a simulated preview record only."
        )
        if DEMO_PROOF_CHAT_ID:
            try:
                await bot.send_message(
                    chat_id=DEMO_PROOF_CHAT_ID,
                    text=group_message,
                    parse_mode="HTML",
                    reply_markup=receipt_keyboard(reference),
                )
            except TelegramAPIError:
                logger.exception("Could not post demo withdrawal to configured group.")

        await message.answer(
            "✅ <b>Demo request recorded!</b>\n\n"
            f"💰 Amount: <b>{amount_text} DEMO USDT</b>\n"
            f"🧾 Reference: <code>{reference}</code>\n\n"
            "🧪 No real USDT was sent. This is not a blockchain transaction.",
            reply_markup=receipt_keyboard(reference),
            parse_mode="HTML",
        )
        await message.answer(
            "Choose an option below.",
            reply_markup=main_keyboard(user_id),
        )
        return

    if not await ensure_access(message, bot):
        return
    await message.answer(
        "Please choose an option from the menu below.",
        reply_markup=main_keyboard(user_id),
    )


@dp.message(Command("help"))
async def handle_help(message: Message, bot: Bot):
    if not message.from_user or not await ensure_access(message, bot):
        return
    await message.answer(
        "USDT09 demo menu:\n"
        "🎁 Bonus — claim once per hour.\n"
        "💸 Withdraw — create a simulated withdrawal receipt.\n"
        "👥 Referrals — invite link and verified counts.\n"
        "🏆 Leaderboard — top verified inviters.\n\n"
        "No real USDT transfers or cash rewards are provided.",
        reply_markup=main_keyboard(message.from_user.id),
    )


# ---------------------------------------------------------------------------
# FastAPI receipt page and Telegram webhook
# ---------------------------------------------------------------------------

def build_receipt_html(row) -> str:
    amount = format_units(int(row["amount_units"]))
    name = " ".join(
        part for part in [row["first_name"], row["last_name"]] if part
    ).strip() or (f"@{row['username']}" if row["username"] else f"User {row['telegram_id']}")
    safe_name = html.escape(name)
    wallet = str(row["wallet_address"])
    if len(wallet) > 18:
        masked_wallet = html.escape(wallet[:8] + "…" + wallet[-6:])
    else:
        masked_wallet = html.escape(wallet)
    created = datetime.fromtimestamp(int(row["created_at"]), tz=timezone.utc)
    date_text = created.strftime("%d.%m.%Y, %H:%M:%S UTC")
    reference = html.escape(str(row["id"]))
    home_url = f"https://t.me/{bot_username}" if bot_username else "https://t.me/"
    home_url = html.escape(home_url, quote=True)

    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#101a2b">
<title>USDT09 Demo Receipt</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--card:#121f32;--line:#263852;--muted:#9aabc0;--text:#f4f7fb;--teal:#57d6c1;--gold:#efc36b}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#1c3551 0,#0b1220 48%);font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--text);min-height:100vh;padding:28px 16px}
.wrap{max-width:520px;margin:0 auto}.nav{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px}.brand{font-weight:800;letter-spacing:.4px}.brand span{color:var(--teal)}.home{color:#c9d7e8;text-decoration:none;font-size:13px}
.card{background:rgba(18,31,50,.96);border:1px solid var(--line);border-radius:22px;padding:24px;box-shadow:0 22px 70px #0006}
.demo{display:inline-flex;align-items:center;gap:7px;border:1px solid #96743c;background:#3b2f1c;color:#ffe0a0;padding:8px 12px;border-radius:999px;font-size:11px;font-weight:800;letter-spacing:.8px}
h1{font-size:24px;margin:19px 0 7px}.subtitle{color:var(--muted);font-size:13px;line-height:1.5}
.amount{text-align:center;padding:30px 8px 24px}.amount strong{font-size:43px;letter-spacing:-1.5px}.amount small{display:block;color:var(--teal);font-weight:700;margin-top:5px}
.status{border:1px solid #987d42;background:#342b1c;color:#ffdfa1;border-radius:13px;padding:14px;font-weight:800;font-size:13px;text-align:center}
.notice{font-size:12px;color:#f4d9a5;line-height:1.55;margin-top:14px}
.rows{margin-top:22px;border-top:1px solid var(--line)}.row{display:flex;justify-content:space-between;gap:20px;padding:15px 0;border-bottom:1px solid var(--line);font-size:13px}.label{color:var(--muted)}.value{text-align:right;font-weight:650;overflow-wrap:anywhere}
.ref{margin-top:20px;background:#0c1727;border:1px solid var(--line);border-radius:12px;padding:14px}.ref .label{font-size:11px}.ref code{display:block;margin-top:7px;font-size:13px;color:#b9d8e8;overflow-wrap:anywhere}
.footer{text-align:center;color:#8193a9;font-size:11px;line-height:1.6;margin:20px 8px}.homebtn{display:block;text-align:center;text-decoration:none;margin-top:18px;background:var(--teal);color:#06211f;font-weight:800;border-radius:12px;padding:14px}
</style>
</head>
<body>
<div class="wrap">
 <div class="nav"><div class="brand">USDT<span>09</span></div><a class="home" href="{{HOME}}">Home ↗</a></div>
 <main class="card">
  <div class="demo">🧪 DEMO RECEIPT · NOT ON-CHAIN</div>
  <h1>Withdrawal preview</h1>
  <div class="subtitle">A simulated record created by USDT09. This page is not a blockchain explorer.</div>
  <div class="amount"><strong>{{AMOUNT}}</strong><small>DEMO USDT</small></div>
  <div class="status">SIMULATED · NO REAL TRANSFER</div>
  <div class="notice">No USDT was sent or received. No TON transaction was created, and this receipt has no blockchain hash or on-chain value.</div>
  <div class="rows">
   <div class="row"><span class="label">Account</span><span class="value">{{NAME}}</span></div>
   <div class="row"><span class="label">To (masked)</span><span class="value">{{WALLET}}</span></div>
   <div class="row"><span class="label">Status</span><span class="value">Demo record only</span></div>
   <div class="row"><span class="label">Date</span><span class="value">{{DATE}}</span></div>
  </div>
  <div class="ref"><div class="label">Demo reference (not a transaction hash)</div><code>{{REFERENCE}}</code></div>
  <a class="homebtn" href="{{HOME}}">Return to USDT09</a>
 </main>
 <div class="footer">USDT09 · Preview interface<br>This demonstration does not represent a real payment, payout, or TON blockchain transaction.</div>
</div>
</body>
</html>"""
    replacements = {
        "{{HOME}}": home_url,
        "{{AMOUNT}}": amount,
        "{{NAME}}": safe_name,
        "{{WALLET}}": masked_wallet,
        "{{DATE}}": html.escape(date_text),
        "{{REFERENCE}}": reference,
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global active_bot, polling_task, bot_username

    init_db()

    if not BOT_TOKEN or not REQUIRED_CHANNELS:
        missing = []
        if not BOT_TOKEN:
            missing.append("BOT_TOKEN")
        if not REQUIRED_CHANNELS:
            missing.append("FORCE_CHANNELS")
        logger.warning(
            "Telegram bot disabled until configured: %s",
            ", ".join(missing),
        )
        yield
        return

    active_bot = Bot(token=BOT_TOKEN)
    me = await active_bot.get_me()
    bot_username = me.username or ""

    await active_bot.set_my_commands(
        [
            BotCommand(command="start", description="Start the bot"),
            BotCommand(command="help", description="How to use the bot"),
            BotCommand(command="cancel", description="Cancel current demo withdrawal"),
        ]
    )

    if WEBHOOK_URL:
        if not WEBHOOK_SECRET:
            raise RuntimeError("WEBHOOK_SECRET is required when WEBHOOK_URL is configured.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
            raise RuntimeError("WEBHOOK_SECRET must contain only letters, numbers, _ or -.")
        endpoint = WEBHOOK_URL if WEBHOOK_URL.endswith(WEBHOOK_PATH) else WEBHOOK_URL + WEBHOOK_PATH
        await active_bot.set_webhook(
            url=endpoint,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        logger.info("Telegram webhook configured.")
    else:
        await active_bot.delete_webhook(drop_pending_updates=False)
        polling_task = asyncio.create_task(
            dp.start_polling(active_bot, handle_signals=False)
        )
        logger.info("Telegram polling started.")

    if DEMO_PROOF_CHAT_ID:
        logger.info("Demo receipt group posting is configured.")
    else:
        logger.warning("DEMO_PROOF_CHAT_ID is empty; demo receipts will be sent only to the user.")

    yield

    if polling_task:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
        polling_task = None

    if active_bot:
        await active_bot.session.close()
        active_bot = None


app = FastAPI(
    title="USDT09 Telegram Bot",
    description="USDT09 demo rewards and referral preview.",
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    return {
        "service": "USDT09 Telegram Bot",
        "status": "online" if active_bot is not None else "waiting_for_configuration",
        "telegram_bot_enabled": active_bot is not None,
        "mode": "demo_only",
        "real_crypto_transfers": False,
    }


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "usdt09",
        "telegram_bot_enabled": active_bot is not None,
        "mode": "demo_only",
    }


@app.get("/demo/receipt/{reference}", response_class=HTMLResponse)
async def demo_receipt(reference: str):
    if not re.fullmatch(r"[A-F0-9]{12}", reference.upper()):
        raise HTTPException(status_code=404, detail="Demo receipt not found.")
    row = get_demo_receipt(reference)
    if not row:
        raise HTTPException(status_code=404, detail="Demo receipt not found.")
    return HTMLResponse(build_receipt_html(row))


@app.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    telegram_secret: Optional[str] = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
    ),
):
    if not WEBHOOK_URL:
        raise HTTPException(status_code=404, detail="Webhook mode is not configured.")
    if not WEBHOOK_SECRET or not hmac.compare_digest(telegram_secret or "", WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Invalid webhook secret.")
    if active_bot is None:
        raise HTTPException(status_code=503, detail="Bot is not ready.")

    try:
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": active_bot})
        await dp.feed_update(active_bot, update)
    except Exception:
        logger.exception("Failed to process Telegram update.")
        return JSONResponse(status_code=200, content={"ok": False})
    return {"ok": True}
