import asyncio
import html
import hmac
import logging
import os
import re
import secrets
import random
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, quote, urlsplit

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import storage
from admin import admin_router, is_admin


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("usdt09")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
FORCE_CHANNELS_RAW = os.getenv("FORCE_CHANNELS", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = "/webhook"
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip() or "https://usdt09.onrender.com/app"
ADSGRAM_BLOCK_ID = os.getenv("ADSGRAM_BLOCK_ID", "").strip()
MINI_APP_DIR = Path(__file__).resolve().parent / "miniapp"

active_bot: Optional[Bot] = None
polling_task: Optional[asyncio.Task] = None
bot_username = ""
telegram_log_handler = None


def _configured_audit_chat_id():
    try:
        raw_chat_id = storage.get_setting(
            "admin_audit_chat_id",
            os.getenv("ADMIN_AUDIT_CHAT_ID", "-5399347693"),
        )
        return int(raw_chat_id)
    except Exception:
        return None


class TelegramLogHandler(logging.Handler):
    """Forward important application warnings/errors to the configured Telegram group."""

    def __init__(self, bot: Bot):
        super().__init__(level=logging.WARNING)
        self.bot = bot

    def emit(self, record: logging.LogRecord):
        if not (record.name == "usdt09" or record.name.startswith("usdt09.")):
            return
        message = record.getMessage()
        # Avoid recursively reporting failures of the log delivery itself.
        if record.name == "usdt09.admin" and message.startswith((
            "Could not send admin activity notification",
            "Could not read the configured admin activity log chat ID",
        )):
            return
        try:
            asyncio.get_running_loop().create_task(self._send(record, message))
        except RuntimeError:
            return
        except Exception:
            return

    async def _send(self, record: logging.LogRecord, message: str):
        chat_id = _configured_audit_chat_id()
        if not chat_id:
            return

        if BOT_TOKEN:
            message = message.replace(BOT_TOKEN, "[hidden bot token]")
        if record.exc_info and record.exc_info[0]:
            exception_name = record.exc_info[0].__name__
            exception_text = str(record.exc_info[1] or "")[:700]
            message += f"\n{exception_name}: {exception_text}"

        timestamp = logging.Formatter().formatTime(
            record, "%Y-%m-%d %H:%M:%S"
        )
        text = (
            "⚠️ <b>USDT09 BOT LOG</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Level:</b> {html.escape(record.levelname)}\n"
            f"🧩 <b>Module:</b> <code>{html.escape(record.name)}</code>\n"
            f"🕒 <b>Time:</b> <code>{timestamp} UTC</code>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"<pre>{html.escape(message)[:3000]}</pre>"
        )
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            # Never log a log-delivery failure; that would create a notification loop.
            return


@dataclass(frozen=True)
class RequiredChannel:
    chat_id: str
    join_url: str
    title: str


class WithdrawalStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_address = State()


def parse_force_channels(raw_value: str):
    channels = []
    for entry in raw_value.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = [part.strip() for part in entry.split("|")]
        raw_id = parts[0]
        join_url = parts[1] if len(parts) > 1 else ""
        title = parts[2] if len(parts) > 2 and parts[2] else raw_id
        if raw_id.startswith("@"):
            username = raw_id[1:].strip().strip("/")
            chat_id = f"@{username}"
            join_url = join_url or f"https://t.me/{username}"
        elif raw_id.startswith(("https://t.me/", "http://t.me/")):
            parsed = urlsplit(raw_id)
            path = parsed.path.strip("/")
            if not path or path.startswith(("+", "joinchat/")):
                logger.warning("Private invite links require a numeric chat ID.")
                continue
            username = path.split("/", 1)[0]
            chat_id = f"@{username}"
            join_url = join_url or f"https://t.me/{username}"
        else:
            chat_id = raw_id
        if not join_url.startswith("https://t.me/"):
            logger.warning("Skipping channel %s: a valid https://t.me join URL is required.", chat_id)
            continue
        channels.append(RequiredChannel(chat_id, join_url, title))
    return channels


def get_required_channels():
    raw_value = storage.get_setting("force_channels", FORCE_CHANNELS_RAW)
    return parse_force_channels(raw_value or "")


dp = Dispatcher(storage=MemoryStorage())
dp.include_router(admin_router)


def format_remaining(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def main_keyboard(user_id):
    balance = storage.get_balance_units(user_id)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=f"💰 {storage.format_units(balance, 2)} USDT")],
            [KeyboardButton(text="🎁 Bonus"), KeyboardButton(text="💸 Withdraw")],
            [KeyboardButton(text="👥 Referrals"), KeyboardButton(text="🏆 Leaderboard")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Choose an option",
    )


def force_join_keyboard():
    rows = [
        [InlineKeyboardButton(text=f"📢 Join channel · {channel.title}", url=channel.join_url)]
        for channel in get_required_channels()
    ]
    rows.append([InlineKeyboardButton(text="✅ Verify", callback_data="verify_subscription")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def welcome_text(user_id):
    settings = storage.get_settings()
    balance = storage.get_balance_units(user_id)
    return (
        "✅ <b>Channel verified!</b>\n\n"
        "👋 Welcome to <b>USDT09</b> 💎\n\n"
        f"💰 Available balance: <b>{storage.format_units(balance)} USDT</b>\n"
        f"🎁 Bonus: <b>{storage.format_units(settings['bonus_units'])} USDT</b>\n\n"
        "Choose an option below."
    )


async def get_membership_status(bot, user_id):
    for channel in get_required_channels():
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


async def ensure_access(message, bot):
    user = message.from_user
    if not user:
        return False
    if storage.is_user_banned(user.id):
        await message.answer("⛔ Your account is restricted. Please contact support.")
        return False
    settings = storage.get_settings()
    if settings["maintenance"] == "ON" and not is_admin(user.id):
        await message.answer("🛠 The bot is temporarily under maintenance. Please try again later.")
        return False
    status, _ = await get_membership_status(bot, user.id)
    if status == "missing":
        await message.answer(
            "🔒 <b>Join our channel first</b>\n\nJoin every required channel, then tap ✅ Verify.",
            reply_markup=force_join_keyboard(),
            parse_mode="HTML",
        )
        return False
    if status == "error":
        await message.answer("⚠️ Membership verification is temporarily unavailable. Please try again.")
        return False
    storage.mark_verified(user.id, user.username, user.first_name or "", user.last_name or "")
    return True


def _valid_wallet(network, address):
    network = network.upper()
    address = address.strip()
    if network in {"BEP20", "ERC20"}:
        return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", address))
    if network == "TRC20":
        return bool(re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", address))
    if network == "TON":
        return bool(
            re.fullmatch(r"(?:EQ|UQ)[A-Za-z0-9_-]{46}", address)
            or re.fullmatch(r"0:[0-9a-fA-F]{64}", address)
        )
    return False


@dp.message(CommandStart())
async def handle_start(message: Message, command: CommandObject, bot: Bot, state: FSMContext):
    await state.clear()
    user = message.from_user
    if not user:
        return
    if storage.is_user_banned(user.id):
        await message.answer("⛔ Your account is restricted. Please contact support.")
        return

    if is_admin(user.id):
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Start the bot"),
                    BotCommand(command="help", description="How to use the bot"),
                    BotCommand(command="admin", description="Open admin control panel"),
                ],
                scope=BotCommandScopeChat(chat_id=user.id),
            )
        except TelegramAPIError:
            logger.exception("Could not refresh admin commands for Telegram ID %s", user.id)

    referrer_id = None
    match = re.fullmatch(r"ref_(\d{1,19})", (command.args or "").strip())
    if match:
        candidate_id = int(match.group(1))
        if 0 < candidate_id <= 9223372036854775807 and candidate_id != user.id:
            referrer_id = candidate_id

    status, _ = await get_membership_status(bot, user.id)
    storage.register_user(
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
            "🔒 <b>Join our channel first</b>\n\nJoin the channel below, then tap ✅ Verify.",
            reply_markup=force_join_keyboard(),
            parse_mode="HTML",
        )
    else:
        await message.answer("⚠️ I couldn't verify your channel membership. Please try again later.")


@dp.callback_query(F.data == "verify_subscription")
async def handle_verify(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if storage.is_user_banned(user.id):
        await callback.answer("Your account is restricted.", show_alert=True)
        return
    status, _ = await get_membership_status(bot, user.id)
    if status == "missing":
        await callback.answer("Please join every required channel first.", show_alert=True)
        return
    if status == "error":
        await callback.answer("Verification is temporarily unavailable.", show_alert=True)
        return
    storage.mark_verified(user.id, user.username, user.first_name or "", user.last_name or "")
    await callback.answer("✅ Verified!")
    if callback.message:
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass
        await callback.message.answer(
            welcome_text(user.id),
            reply_markup=main_keyboard(user.id),
            parse_mode="HTML",
        )


@dp.message(Command("myid"))
async def show_my_id(message: Message):
    user_id = message.from_user.id if message.from_user else "unknown"
    await message.answer(
        f"Your Telegram ID: <code>{user_id}</code>\n"
        f"This chat ID: <code>{message.chat.id}</code>",
        parse_mode="HTML",
    )


# Keep this diagnostic command on the root dispatcher and ignore mention mismatches.
# This lets the group owner retrieve the actual chat ID even if the bot username
# was typed with different capitalization or a similar-looking character.
@dp.message(Command("chatid", ignore_mention=True))
async def show_group_chat_id(message: Message):
    await message.answer(
        "🆔 <b>This Telegram chat ID:</b>\n"
        f"<code>{message.chat.id}</code>",
        parse_mode="HTML",
    )


@dp.message(F.text.regexp(r"^/setlogs(?:@[A-Za-z0-9_]+)?$"))
async def set_admin_logs_group(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Only a bot administrator can configure the logs group.")
        return
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer("Send /setlogs in the private group where you want the bot logs.")
        return

    storage.update_setting("admin_audit_chat_id", str(message.chat.id))
    await message.answer(
        "✅ <b>Logs group connected successfully.</b>\n"
        "Admin actions and important bot warnings/errors will be sent here.",
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def handle_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "<b>USDT09 Help</b>\n\n"
        "🎁 Claim your available bonus.\n"
        "💸 Submit a withdrawal request with your network and wallet.\n"
        "Track updates through withdrawal notifications from the bot.\n"
        "👥 Invite friends using your referral link.\n\n"
        "For support, contact the bot administrator.",
        parse_mode="HTML",
    )


@dp.message(F.text == "❌ Cancel")
async def cancel_withdrawal_button(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "✅ Withdrawal cancelled.",
        reply_markup=main_keyboard(message.from_user.id) if message.from_user else None,
    )


@dp.message(Command("cancel"))
async def handle_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Cancelled.", reply_markup=main_keyboard(message.from_user.id) if message.from_user else None)


@dp.message(F.text.startswith("💰"))
async def handle_balance(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    if not await ensure_access(message, bot):
        return
    balance = storage.get_balance_units(message.from_user.id)
    await message.answer(
        "💰 <b>Your available balance</b>\n\n"
        f"<code>{storage.format_units(balance)} USDT</code>\n\n"
        "Funds requested for withdrawal are reserved until the request is paid or rejected.",
        reply_markup=main_keyboard(message.from_user.id),
        parse_mode="HTML",
    )


@dp.message(F.text == "🎁 Bonus")
async def handle_bonus(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    if not await ensure_access(message, bot):
        return

    status, balance, remaining = storage.get_bonus_status(message.from_user.id)
    if status == "cooldown":
        await message.answer(
            f"⏳ Your next bonus will be available in <b>{format_remaining(remaining)}</b>.",
            reply_markup=main_keyboard(message.from_user.id),
            parse_mode="HTML",
        )
        return
    if status != "ready":
        await message.answer("Please complete channel verification before claiming a bonus.")
        return

    settings = storage.get_settings()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🧮 Start Math CAPTCHA",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )],
    ])
    await message.answer(
        "🎁 <b>Hourly Bonus</b>\n\n"
        "Solve two quick math questions to verify your bonus claim.\n"
        f"💰 Bonus amount: <b>{storage.format_units(settings['bonus_units'])} USDT</b>\n\n"
        "Tap below to open the Math CAPTCHA Mini App.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


def withdrawal_cancel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Cancel")]],
        resize_keyboard=True,
        input_field_placeholder="Enter withdrawal details",
    )


@dp.message(F.text == "💸 Withdraw")
async def handle_withdraw(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    if not await ensure_access(message, bot):
        return

    settings = storage.get_settings()
    balance = storage.get_balance_units(message.from_user.id)
    minimum = settings["min_withdrawal_units"]
    if balance < minimum:
        await message.answer(
            "💸 <b>USDT BEP20 Withdrawal</b>\n\n"
            "⚠️ Your balance is below the minimum withdrawal.\n"
            f"Minimum: <b>{storage.format_units(minimum)} USDT</b>\n"
            f"Your balance: <b>{storage.format_units(balance)} USDT</b>",
            reply_markup=main_keyboard(message.from_user.id),
            parse_mode="HTML",
        )
        return

    await state.set_state(WithdrawalStates.waiting_for_amount)
    await message.answer(
        "💸💎 <b>USDT BEP20 Withdrawal</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Minimum withdrawal: <b>{storage.format_units(minimum)} USDT</b>\n"
        f"Available balance: <b>{storage.format_units(balance)} USDT</b>\n\n"
        "✍️ <b>Please send the amount you want to withdraw.</b>\n"
        "Example: <code>0.10</code>",
        reply_markup=withdrawal_cancel_keyboard(),
        parse_mode="HTML",
    )


@dp.message(
    WithdrawalStates.waiting_for_amount,
    F.text,
    ~F.text.startswith("💰"),
    F.text != "🎁 Bonus",
    F.text != "💸 Withdraw",
    F.text != "👥 Referrals",
    F.text != "🏆 Leaderboard",
    F.text != "❌ Cancel",
    ~F.text.startswith("/"),
)
async def receive_withdrawal_amount(message: Message, state: FSMContext, bot: Bot):
    if not await ensure_access(message, bot):
        await state.clear()
        return

    amount_units = storage.parse_amount_units((message.text or "").strip())
    if amount_units is None:
        await message.answer(
            "⚠️ Invalid amount. Send a positive USDT amount, for example <code>0.10</code>.",
            reply_markup=withdrawal_cancel_keyboard(),
            parse_mode="HTML",
        )
        return

    settings = storage.get_settings()
    minimum = settings["min_withdrawal_units"]
    if amount_units < minimum:
        await message.answer(
            f"⚠️ Minimum withdrawal is <b>{storage.format_units(minimum)} USDT</b>. "
            "Please send a higher amount.",
            reply_markup=withdrawal_cancel_keyboard(),
            parse_mode="HTML",
        )
        return

    balance = storage.get_balance_units(message.from_user.id)
    if amount_units > balance:
        await message.answer(
            f"⚠️ Insufficient balance. You have <b>{storage.format_units(balance)} USDT</b>. "
            "Please send a smaller amount.",
            reply_markup=withdrawal_cancel_keyboard(),
            parse_mode="HTML",
        )
        return

    await state.update_data(withdrawal_amount_units=amount_units)
    await state.set_state(WithdrawalStates.waiting_for_address)
    await message.answer(
        "✅ Amount accepted.\n\n"
        f"Amount: <b>{storage.format_units(amount_units)} USDT</b>\n"
        "Network: <b>BEP20 (BNB Smart Chain)</b>\n\n"
        "📥 <b>Please send your BEP20 wallet address.</b>\n"
        "Example: <code>0x1234... (42 characters total)</code>\n\n"
        "⚠️ Make sure this is a USDT BEP20 address.",
        reply_markup=withdrawal_cancel_keyboard(),
        parse_mode="HTML",
    )


@dp.message(
    WithdrawalStates.waiting_for_address,
    F.text,
    ~F.text.startswith("💰"),
    F.text != "🎁 Bonus",
    F.text != "💸 Withdraw",
    F.text != "👥 Referrals",
    F.text != "🏆 Leaderboard",
    F.text != "❌ Cancel",
    ~F.text.startswith("/"),
)
async def receive_withdrawal_address(message: Message, state: FSMContext, bot: Bot):
    if not await ensure_access(message, bot):
        await state.clear()
        return

    wallet = (message.text or "").strip()
    if not _valid_wallet("BEP20", wallet):
        await message.answer(
            "⚠️ This is not a valid BEP20 wallet address.\n"
            "Please send a valid 0x address containing 42 characters.",
            reply_markup=withdrawal_cancel_keyboard(),
        )
        return

    data = await state.get_data()
    amount_units = int(data.get("withdrawal_amount_units") or 0)
    if amount_units <= 0:
        await state.clear()
        await message.answer(
            "⚠️ Withdrawal session expired. Please press Withdraw again.",
            reply_markup=main_keyboard(message.from_user.id),
        )
        return

    result, withdrawal_id, balance = storage.create_withdrawal(
        message.from_user.id, amount_units, "BEP20", wallet
    )
    if result == "below_minimum":
        settings = storage.get_settings()
        await state.clear()
        await message.answer(
            f"Minimum withdrawal is <b>{storage.format_units(settings['min_withdrawal_units'])} USDT</b>.",
            reply_markup=main_keyboard(message.from_user.id),
            parse_mode="HTML",
        )
        return
    if result == "insufficient":
        await state.clear()
        await message.answer(
            f"⚠️ Insufficient balance. Your current balance is <b>{storage.format_units(balance)} USDT</b>.",
            reply_markup=main_keyboard(message.from_user.id),
            parse_mode="HTML",
        )
        return
    if result != "created":
        await state.clear()
        await message.answer(
            "Please complete channel verification before submitting a withdrawal.",
            reply_markup=main_keyboard(message.from_user.id),
        )
        return

    await state.clear()
    await message.answer(
        "✅ <b>Withdrawal request submitted successfully!</b>\n\n"
        f"Request ID: <b>#{withdrawal_id}</b>\n"
        f"Amount: <b>{storage.format_units(amount_units)} USDT</b>\n"
        "Network: <b>BEP20</b>\n"
        "Status: <b>Pending</b>\n\n"
        "Your amount has been reserved. You will receive a message when the request is approved, rejected or paid.",
        reply_markup=main_keyboard(message.from_user.id),
        parse_mode="HTML",
    )


@dp.message(F.text == "👥 Referrals")
async def handle_referrals(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    if not await ensure_access(message, bot):
        return
    me = await bot.get_me()
    count = storage.get_referral_count(message.from_user.id)
    referral_reward = storage.get_settings()["referral_reward_units"]
    invite_link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    await message.answer(
        "👥 <b>Your invitation link</b>\n\n"
        "Invite friends to join USDT09.\n"
        f"💵 Reward: <b>{storage.format_units(referral_reward)} USDT</b> per verified referral.\n\n"
        f"🔗 <code>{html.escape(invite_link)}</code>\n"
        f"✅ Verified referrals: <b>{count}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📤 Share invite link",
                    url=(
                        "https://t.me/share/url?"
                        f"url={quote(invite_link, safe='')}"
                        f"&text={quote('Join USDT09 and start earning USDT rewards!', safe='')}"
                    ),
                )
            ]
        ]),
        parse_mode="HTML",
    )


@dp.message(F.text == "🏆 Leaderboard")
async def handle_leaderboard(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    if not await ensure_access(message, bot):
        return
    rows = storage.get_leaderboard(10)
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Top referrers</b>\n"]
    if not rows:
        lines.append("No verified referrals yet. Invite friends to be first!")
    else:
        for index, row in enumerate(rows, start=1):
            medal = medals[index - 1] if index <= 3 else f"{index}."
            display_name = " ".join(
                part for part in (row.get("first_name"), row.get("last_name")) if part
            ).strip()
            if not display_name:
                display_name = f"@{row['username']}" if row.get("username") else str(row["telegram_id"])
            lines.append(
                f"{medal} {html.escape(display_name)} — <b>{int(row['referrals'])}</b>"
            )
    await message.answer("\n".join(lines), reply_markup=main_keyboard(message.from_user.id), parse_mode="HTML")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global active_bot, polling_task, bot_username
    storage.init_db()

    if not BOT_TOKEN:
        logger.warning("Telegram bot disabled until BOT_TOKEN is configured.")
        yield
        return
    if not get_required_channels():
        logger.warning("No Force Join channels are configured; add channels from the admin panel before public use.")

    active_bot = Bot(token=BOT_TOKEN)
    me = await active_bot.get_me()
    bot_username = me.username or ""
    global telegram_log_handler
    if telegram_log_handler is not None:
        logger.removeHandler(telegram_log_handler)
    telegram_log_handler = TelegramLogHandler(active_bot)
    logger.addHandler(telegram_log_handler)
    logger.info("Telegram bot identity confirmed: @%s (ID %s)", bot_username or "no_username", me.id)

    commands = [
        BotCommand(command="start", description="Start the bot"),
        BotCommand(command="help", description="How to use the bot"),
    ]
    await active_bot.set_my_commands(commands)
    for admin_id in sorted(storage.get_admin_ids()):
        try:
            await active_bot.set_my_commands(
                commands + [BotCommand(command="admin", description="Open admin control panel")],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except TelegramAPIError:
            # A stale or unreachable admin chat must not prevent the bot from starting.
            logger.exception(
                "Could not set admin commands for Telegram ID %s; "
                "check ADMIN_IDS and ask that admin to send /start to the bot.",
                admin_id,
            )

    if WEBHOOK_URL:
        if not WEBHOOK_SECRET or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
            raise RuntimeError("A valid WEBHOOK_SECRET is required when WEBHOOK_URL is configured.")
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
        polling_task = asyncio.create_task(dp.start_polling(active_bot, handle_signals=False))
        logger.info("Telegram polling started.")

    yield

    if polling_task:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
        polling_task = None
    if telegram_log_handler is not None:
        logger.removeHandler(telegram_log_handler)
        telegram_log_handler = None
    if active_bot:
        await active_bot.session.close()
        active_bot = None


app = FastAPI(
    title="USDT09 Telegram Bot",
    description="USDT09 bot with a verified two-question Math CAPTCHA bonus Mini App.",
    version="3.1.0",
    lifespan=lifespan,
)
app.mount("/miniapp", StaticFiles(directory=str(MINI_APP_DIR)), name="miniapp")


def _verify_telegram_init_data(init_data: str):
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured.")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        params = dict(pairs)
        if len(params) != len(pairs):
            raise ValueError("Duplicate Telegram init data fields.")
        received_hash = params.pop("hash", "")
        if not re.fullmatch(r"[0-9a-f]{64}", received_hash):
            raise ValueError("Missing Telegram signature.")
        check_string = "\n".join(f"{key}={params[key]}" for key in sorted(params))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), "sha256").digest()
        calculated_hash = hmac.new(secret_key, check_string.encode("utf-8"), "sha256").hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            raise ValueError("Telegram signature mismatch.")
        auth_date = int(params.get("auth_date", "0"))
        now = int(__import__("time").time())
        if auth_date <= 0 or auth_date > now + 60 or now - auth_date > 86400:
            raise ValueError("Telegram session expired.")
        user = json.loads(params.get("user", "{}"))
        user_id = int(user.get("id", 0))
        if user_id <= 0:
            raise ValueError("Telegram user missing.")
        return user
    except HTTPException:
        raise
    except Exception as exc:
        logger.info("Rejected invalid Telegram Mini App session: %s", exc)
        raise HTTPException(status_code=401, detail="Telegram session is invalid. Reopen the Mini App.")


async def _verified_miniapp_user(request: Request):
    body = await request.json()
    user_data = _verify_telegram_init_data(str(body.get("initData") or ""))
    user_id = int(user_data["id"])
    user = storage.get_user(user_id)
    if not user or not user.get("verified"):
        raise HTTPException(status_code=403, detail="Start the bot and complete channel verification first.")
    if user.get("is_banned"):
        raise HTTPException(status_code=403, detail="Your account is restricted.")
    if active_bot is not None:
        membership, _ = await get_membership_status(active_bot, user_id)
        if membership != "joined":
            raise HTTPException(status_code=403, detail="Join the required channel and verify your account first.")
    return user_data, body


def _new_math_question():
    a = 2 + secrets.randbelow(17)
    b = 2 + secrets.randbelow(17)
    target = a + b
    correct_expression = f"{a} + {b}"
    choices = [correct_expression]
    sums = {target}
    while len(choices) < 3:
        left = 2 + secrets.randbelow(17)
        right = 2 + secrets.randbelow(17)
        result = left + right
        if result in sums:
            continue
        sums.add(result)
        choices.append(f"{left} + {right}")
    random.SystemRandom().shuffle(choices)
    return {
        "target": target,
        "options": choices,
        "correct_index": choices.index(correct_expression),
    }


def _question_for_client(question, number):
    return {
        "number": int(number),
        "total": 2,
        "target": int(question["target"]),
        "options": list(question["options"]),
    }


@app.get("/app")
async def math_captcha_app():
    return FileResponse(MINI_APP_DIR / "index.html")


@app.get("/api/miniapp/config")
async def mini_app_config():
    settings = storage.get_settings()
    return {
        "app": "Math CAPTCHA",
        "adsgramBlockId": settings.get("adsgram_block_id") or ADSGRAM_BLOCK_ID,
        "questionsPerRound": 2,
        "adFormat": "Rewarded",
        "adIsOptional": True,
    }


@app.post("/api/miniapp/start")
async def start_math_challenge(request: Request):
    user_data, body = await _verified_miniapp_user(request)
    user_id = int(user_data["id"])
    status, balance, remaining = storage.get_bonus_status(user_id)
    if status == "cooldown":
        return {
            "ok": True,
            "status": "cooldown",
            "remainingSeconds": int(remaining),
        }
    if status != "ready":
        raise HTTPException(status_code=403, detail="Complete channel verification before claiming a bonus.")

    questions = [_new_math_question(), _new_math_question()]
    challenge_id = storage.create_math_challenge(user_id, questions)
    return {
        "ok": True,
        "status": "question",
        "challengeId": challenge_id,
        "question": _question_for_client(questions[0], 1),
    }


@app.post("/api/miniapp/answer")
async def answer_math_challenge(request: Request):
    user_data, body = await _verified_miniapp_user(request)
    challenge_id = str(body.get("challengeId") or "")
    try:
        answer_index = int(body.get("answerIndex"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Choose answer A, B, or C.")

    result = storage.submit_math_answer(challenge_id, int(user_data["id"]), answer_index)
    status = result.get("status")
    if status == "incorrect":
        return {
            "ok": True,
            "status": "incorrect",
            "question": result["question"],
        }
    if status == "next":
        return {
            "ok": True,
            "status": "next",
            "question": result["question"],
        }
    if status == "claimed":
        return {
            "ok": True,
            "status": "verified",
            "bonusAmount": storage.format_units(result["amount_units"]),
            "balance": storage.format_units(result["balance_units"]),
        }
    if status == "cooldown":
        return {
            "ok": True,
            "status": "cooldown",
            "remainingSeconds": int(result["remaining"]),
        }
    if status == "expired":
        raise HTTPException(status_code=410, detail="This challenge expired. Close the Mini App and try again.")
    if status == "not_found":
        raise HTTPException(status_code=404, detail="Challenge not found. Start again from the Bonus button.")
    if status == "completed":
        raise HTTPException(status_code=409, detail="This challenge has already been completed.")
    if status == "not_verified":
        raise HTTPException(status_code=403, detail="Your account is not verified.")
    raise HTTPException(status_code=400, detail="Invalid challenge or answer.")


@app.get("/")
async def root():
    return {
        "service": "USDT09 Telegram Bot",
        "status": "online" if active_bot is not None else "waiting_for_configuration",
        "telegram_bot_enabled": active_bot is not None,
        "database": "postgresql",
        "mini_app": "/app",
        "withdrawals": "manual",
    }


@app.get("/health")
async def health():
    try:
        storage.get_settings()
        database_ok = True
    except Exception:
        logger.exception("Database health check failed.")
        database_ok = False
    return {
        "ok": database_ok,
        "service": "usdt09",
        "telegram_bot_enabled": active_bot is not None,
        "database": "postgresql",
    }


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
