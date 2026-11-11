import asyncio
import html
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

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
)
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

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

active_bot: Optional[Bot] = None
polling_task: Optional[asyncio.Task] = None
bot_username = ""


@dataclass(frozen=True)
class RequiredChannel:
    chat_id: str
    join_url: str
    title: str


class WithdrawalStates(StatesGroup):
    waiting_for_details = State()


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


REQUIRED_CHANNELS = parse_force_channels(FORCE_CHANNELS_RAW)
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
            [KeyboardButton(text="📄 My Withdrawals")],
            [KeyboardButton(text="👥 Referrals"), KeyboardButton(text="🏆 Leaderboard")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Choose an option",
    )


def force_join_keyboard():
    rows = [
        [InlineKeyboardButton(text=f"📢 Join channel · {channel.title}", url=channel.join_url)]
        for channel in REQUIRED_CHANNELS
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
async def handle_start(message: Message, command: CommandObject, bot: Bot):
    user = message.from_user
    if not user:
        return
    if storage.is_user_banned(user.id):
        await message.answer("⛔ Your account is restricted. Please contact support.")
        return

    referrer_id = None
    match = re.fullmatch(r"ref_(\d+)", (command.args or "").strip())
    if match:
        referrer_id = int(match.group(1))

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
    if message.from_user:
        await message.answer(f"Your Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@dp.message(Command("help"))
async def handle_help(message: Message):
    await message.answer(
        "<b>USDT09 Help</b>\n\n"
        "🎁 Claim your available bonus.\n"
        "💸 Submit a withdrawal request with your network and wallet.\n"
        "📄 Check withdrawal status and TXID.\n"
        "👥 Invite friends using your referral link.\n\n"
        "For support, contact the bot administrator.",
        parse_mode="HTML",
    )


@dp.message(Command("cancel"))
async def handle_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Cancelled.", reply_markup=main_keyboard(message.from_user.id) if message.from_user else None)


@dp.message(F.text.startswith("💰"))
async def handle_balance(message: Message, bot: Bot):
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
async def handle_bonus(message: Message, bot: Bot):
    if not await ensure_access(message, bot):
        return
    status, balance, remaining = storage.claim_hourly_bonus(message.from_user.id)
    if status == "claimed":
        settings = storage.get_settings()
        amount = settings["bonus_units"]
        await message.answer(
            "🎉 <b>Bonus claimed successfully!</b>\n\n"
            f"💰 <b>+{storage.format_units(amount)} USDT</b> added to your balance.\n"
            f"💳 New balance: <b>{storage.format_units(balance)} USDT</b>.",
            reply_markup=main_keyboard(message.from_user.id),
            parse_mode="HTML",
        )
    elif status == "cooldown":
        await message.answer(
            f"⏳ Your next bonus will be available in <b>{format_remaining(remaining)}</b>.",
            reply_markup=main_keyboard(message.from_user.id),
            parse_mode="HTML",
        )
    else:
        await message.answer("Please complete channel verification before claiming a bonus.")


@dp.message(F.text == "💸 Withdraw")
async def handle_withdraw(message: Message, bot: Bot, state: FSMContext):
    if not await ensure_access(message, bot):
        return
    settings = storage.get_settings()
    balance = storage.get_balance_units(message.from_user.id)
    await state.set_state(WithdrawalStates.waiting_for_details)
    await message.answer(
        "💸 <b>Withdrawal request</b>\n\n"
        f"Available balance: <b>{storage.format_units(balance)} USDT</b>\n"
        f"Minimum withdrawal: <b>{storage.format_units(settings['min_withdrawal_units'])} USDT</b>\n\n"
        "Send the amount, network and wallet address in one message:\n"
        "<code>0.50 BEP20 0xYour40CharacterWalletAddress</code>\n\n"
        "Supported networks: BEP20, TRC20, TON, ERC20.\n"
        "Check the selected network and wallet carefully. Your amount will be reserved while the request is pending.\n"
        "Send /cancel to cancel before submitting.",
        parse_mode="HTML",
    )


@dp.message(WithdrawalStates.waiting_for_details)
async def receive_withdrawal(message: Message, state: FSMContext, bot: Bot):
    if not await ensure_access(message, bot):
        return
    parts = (message.text or "").strip().split()
    if len(parts) != 3:
        await message.answer(
            "Send exactly: <code>amount NETWORK wallet_address</code>\n"
            "Example: <code>0.50 BEP20 0xYour40CharacterWalletAddress</code>",
            parse_mode="HTML",
        )
        return
    amount_units = storage.parse_amount_units(parts[0])
    network = parts[1].upper()
    wallet = parts[2]
    if amount_units is None:
        await message.answer("Invalid amount. Use up to 4 decimal places, for example <code>0.50</code>.", parse_mode="HTML")
        return
    if not _valid_wallet(network, wallet):
        await message.answer(
            "⚠️ The wallet address format does not match the selected network.\n"
            "Please check the network/address pair and try again.",
        )
        return
    result, withdrawal_id, balance = storage.create_withdrawal(
        message.from_user.id, amount_units, network, wallet
    )
    if result == "below_minimum":
        settings = storage.get_settings()
        await message.answer(f"Minimum withdrawal is <b>{storage.format_units(settings['min_withdrawal_units'])} USDT</b>.", parse_mode="HTML")
        return
    if result == "insufficient":
        await message.answer(f"Insufficient balance. Available: <b>{storage.format_units(balance)} USDT</b>.", parse_mode="HTML")
        return
    if result != "created":
        await state.clear()
        await message.answer("Please complete channel verification before submitting a withdrawal.")
        return
    await state.clear()
    await message.answer(
        f"✅ <b>Withdrawal request #{withdrawal_id} submitted.</b>\n\n"
        f"Amount: <b>{storage.format_units(amount_units)} USDT</b>\n"
        f"Network: <b>{html.escape(network)}</b>\n"
        "Status: <b>Pending</b>\n\n"
        "The amount is reserved from your available balance. You will be notified when the request is approved, rejected or paid.",
        reply_markup=main_keyboard(message.from_user.id),
        parse_mode="HTML",
    )


@dp.message(F.text == "📄 My Withdrawals")
async def handle_my_withdrawals(message: Message, bot: Bot):
    if not await ensure_access(message, bot):
        return
    rows = storage.get_user_withdrawals(message.from_user.id, 10)
    if not rows:
        await message.answer("You have no withdrawal requests yet.", reply_markup=main_keyboard(message.from_user.id))
        return
    labels = {
        "pending": "🟠 Pending",
        "approved": "🔵 Approved — awaiting manual transfer",
        "paid": "🟢 Paid",
        "rejected": "🔴 Rejected — funds returned",
    }
    lines = ["📄 <b>Your recent withdrawals</b>\n"]
    for row in rows:
        lines.append(
            f"#{row['id']} · <b>{storage.format_units(row['amount_units'])} USDT</b>\n"
            f"{html.escape(row['network'])} · {labels.get(row['status'], row['status'])}\n"
            f"TXID: <code>{html.escape(row.get('txid') or 'Not available')}</code>"
        )
    await message.answer("\n\n".join(lines), reply_markup=main_keyboard(message.from_user.id), parse_mode="HTML")


@dp.message(F.text == "👥 Referrals")
async def handle_referrals(message: Message, bot: Bot):
    if not await ensure_access(message, bot):
        return
    me = await bot.get_me()
    count = storage.get_referral_count(message.from_user.id)
    invite_link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    await message.answer(
        "👥 <b>Your invitation link</b>\n\n"
        "Invite friends to join USDT09.\n\n"
        f"🔗 <code>{html.escape(invite_link)}</code>\n"
        f"✅ Verified referrals: <b>{count}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Share invite link", url=invite_link)]
        ]),
        parse_mode="HTML",
    )


@dp.message(F.text == "🏆 Leaderboard")
async def handle_leaderboard(message: Message, bot: Bot):
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
            lines.append(f"{medal} {html.escape(row.get('first_name') or row.get('username') or str(row['telegram_id']))} — <b>{row['referrals']}</b>")
    await message.answer("\n".join(lines), reply_markup=main_keyboard(message.from_user.id), parse_mode="HTML")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global active_bot, polling_task, bot_username
    storage.init_db()

    if not BOT_TOKEN:
        logger.warning("Telegram bot disabled until BOT_TOKEN is configured.")
        yield
        return
    if not REQUIRED_CHANNELS:
        logger.warning("FORCE_CHANNELS is empty; Channel Force must be configured before public use.")

    active_bot = Bot(token=BOT_TOKEN)
    me = await active_bot.get_me()
    bot_username = me.username or ""

    commands = [
        BotCommand(command="start", description="Start the bot"),
        BotCommand(command="help", description="How to use the bot"),
        BotCommand(command="myid", description="Show your Telegram ID"),
        BotCommand(command="cancel", description="Cancel current action"),
    ]
    await active_bot.set_my_commands(commands)
    for admin_id in sorted({int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}):
        await active_bot.set_my_commands(
            commands + [BotCommand(command="admin", description="Open admin control panel")],
            scope=BotCommandScopeChat(chat_id=admin_id),
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
    if active_bot:
        await active_bot.session.close()
        active_bot = None


app = FastAPI(
    title="USDT09 Telegram Bot",
    description="USDT09 bot with PostgreSQL-backed user balances and manual withdrawals.",
    version="3.0.0",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    return {
        "service": "USDT09 Telegram Bot",
        "status": "online" if active_bot is not None else "waiting_for_configuration",
        "telegram_bot_enabled": active_bot is not None,
        "database": "postgresql",
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
