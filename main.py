import asyncio
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
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
from fastapi.responses import JSONResponse


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

WEBHOOK_PATH = "/webhook"
MENU_LABELS = {
    "💰 Balance",
    "🎁 Bonus",
    "💸 Withdraw",
    "👥 Referrals",
    "🏆 Leaderboard",
}


@dataclass(frozen=True)
class RequiredChannel:
    chat_id: str
    join_url: str
    title: str


def parse_force_channels(raw_value: str) -> list[RequiredChannel]:
    """
    FORCE_CHANNELS format (separate channels with semicolons):
      @publicchannel|https://t.me/publicchannel|Public channel
      -1001234567890|https://t.me/+inviteCode|Private channel

    For public channels, the short form @publicchannel is also accepted.
    The bot must be an administrator in every required channel to verify
    other users' membership reliably.
    """
    channels: list[RequiredChannel] = []

    for entry in raw_value.split(";"):
        entry = entry.strip()
        if not entry:
            continue

        parts = [part.strip() for part in entry.split("|")]
        chat_id = parts[0]
        if not chat_id:
            continue

        if len(parts) >= 2 and parts[1]:
            join_url = parts[1]
        elif chat_id.startswith("@"):
            join_url = f"https://t.me/{chat_id[1:]}"
        else:
            logger.warning(
                "Skipping channel %s: private channels need an invite URL.",
                chat_id,
            )
            continue

        title = parts[2] if len(parts) >= 3 and parts[2] else chat_id
        if not join_url.startswith(("https://t.me/", "http://t.me/")):
            logger.warning("Skipping channel %s: join URL must be a t.me link.", chat_id)
            continue

        channels.append(
            RequiredChannel(chat_id=chat_id, join_url=join_url, title=title)
        )

    return channels


REQUIRED_CHANNELS = parse_force_channels(FORCE_CHANNELS_RAW)

dp = Dispatcher()
active_bot: Optional[Bot] = None
polling_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# Keyboards and messages
# ---------------------------------------------------------------------------

def force_join_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"📢 Join channel · {channel.title}",
                url=channel.join_url,
            )
        ]
        for channel in REQUIRED_CHANNELS
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="✅ Verify",
                callback_data="verify_subscription",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💰 Balance")],
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

FORCE_JOIN_TEXT = (
    "🔒 To use this bot, you must first join our channel(s).\n\n"
    "Join all channels below, then tap ✅ Verify."
)

WELCOME_TEXT = (
    "✅ Verified! Welcome.\n\n"
    "👋 Welcome to *USDT09*! 💎\n\n"
    "Thanks for joining. The bot is currently in preview mode. "
    "All menu features are marked *Coming Soon* and are not active yet.\n\n"
    "Choose an option below to see its status."
)

COMING_SOON_TEXT = (
    "🚧 *Coming Soon*\n\n"
    "This feature is part of the USDT09 preview and is not available yet. "
    "Please check back later."
)


async def get_membership_status(bot: Bot, user_id: int) -> tuple[str, Optional[RequiredChannel]]:
    """Return ('joined', None), ('missing', channel), or ('error', channel)."""
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(
                chat_id=channel.chat_id,
                user_id=user_id,
            )
        except TelegramAPIError:
            logger.exception(
                "Could not check membership for user %s in %s",
                user_id,
                channel.chat_id,
            )
            return "error", channel

        is_member = member.status not in {
            ChatMemberStatus.LEFT,
            ChatMemberStatus.KICKED,
        }
        if member.status == ChatMemberStatus.RESTRICTED:
            is_member = bool(getattr(member, "is_member", False))

        if not is_member:
            return "missing", channel

    return "joined", None


async def show_join_prompt(message: Message) -> None:
    if not REQUIRED_CHANNELS:
        await message.answer(
            "⚠️ Channel Force is not configured yet. "
            "The administrator must add FORCE_CHANNELS in the environment settings."
        )
        return

    await message.answer(
        FORCE_JOIN_TEXT,
        reply_markup=force_join_keyboard(),
    )


async def send_welcome(message: Message) -> None:
    await message.answer(
        WELCOME_TEXT,
        reply_markup=MAIN_KEYBOARD,
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def handle_start(message: Message, bot: Bot) -> None:
    if message.from_user is None:
        return

    status, _channel = await get_membership_status(bot, message.from_user.id)

    if status == "joined":
        await send_welcome(message)
    elif status == "missing":
        await show_join_prompt(message)
    else:
        await message.answer(
            "⚠️ I couldn't verify your channel membership. "
            "Please try again later or contact the bot administrator."
        )


@dp.message(Command("help"))
async def handle_help(message: Message, bot: Bot) -> None:
    if message.from_user is None:
        return

    status, _channel = await get_membership_status(bot, message.from_user.id)
    if status == "joined":
        await message.answer(
            "Use the buttons on the keyboard to explore USDT09. "
            "All features are currently Coming Soon.",
            reply_markup=MAIN_KEYBOARD,
        )
    elif status == "missing":
        await show_join_prompt(message)
    else:
        await message.answer("⚠️ Membership verification is temporarily unavailable.")


@dp.callback_query(F.data == "verify_subscription")
async def handle_verify(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user is None:
        await callback.answer()
        return

    status, _channel = await get_membership_status(bot, callback.from_user.id)

    if status == "missing":
        await callback.answer(
            "Please join every required channel first.",
            show_alert=True,
        )
        return

    if status == "error":
        await callback.answer(
            "Verification is temporarily unavailable. Please try again later.",
            show_alert=True,
        )
        return

    await callback.answer("✅ Verified!")

    if callback.message:
        try:
            await callback.message.delete()
        except TelegramAPIError:
            logger.debug("Could not delete the old force-join message.")

        await callback.message.answer(
            WELCOME_TEXT,
            reply_markup=MAIN_KEYBOARD,
            parse_mode="Markdown",
        )


@dp.message(F.text.in_(MENU_LABELS))
async def handle_coming_soon_menu(message: Message, bot: Bot) -> None:
    if message.from_user is None:
        return

    status, _channel = await get_membership_status(bot, message.from_user.id)

    if status == "missing":
        await show_join_prompt(message)
        return
    if status == "error":
        await message.answer("⚠️ Membership verification is temporarily unavailable.")
        return

    # Intentionally a placeholder: no wallet, bonus, referral, withdrawal,
    # leaderboard, payment, or reward functionality is active in this preview.
    await message.answer(
        COMING_SOON_TEXT,
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


@dp.message()
async def handle_other_messages(message: Message, bot: Bot) -> None:
    if message.from_user is None:
        return

    status, _channel = await get_membership_status(bot, message.from_user.id)

    if status == "missing":
        await show_join_prompt(message)
    elif status == "error":
        await message.answer("⚠️ Membership verification is temporarily unavailable.")
    else:
        await message.answer(
            "Please choose an option from the menu below.",
            reply_markup=MAIN_KEYBOARD,
        )


# ---------------------------------------------------------------------------
# FastAPI: Telegram webhook + health endpoint for Render
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    global active_bot, polling_task

    # Allow Render to deploy the HTTP health service before the owner adds
    # the Telegram token and required channels. The Telegram bot stays disabled
    # until both values are configured; no menu or rewards can be used meanwhile.
    if not BOT_TOKEN or not REQUIRED_CHANNELS:
        missing = []
        if not BOT_TOKEN:
            missing.append("BOT_TOKEN")
        if not REQUIRED_CHANNELS:
            missing.append("FORCE_CHANNELS")
        logger.warning(
            "Telegram bot is disabled until these environment variables are set: %s",
            ", ".join(missing),
        )
        yield
        return

    active_bot = Bot(token=BOT_TOKEN)

    await active_bot.set_my_commands(
        [
            BotCommand(command="start", description="Start the bot"),
            BotCommand(command="help", description="How to use the bot"),
        ]
    )

    if WEBHOOK_URL:
        if not WEBHOOK_SECRET:
            raise RuntimeError(
                "WEBHOOK_SECRET is required when WEBHOOK_URL is configured."
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
            raise RuntimeError(
                "WEBHOOK_SECRET must contain only A-Z, a-z, 0-9, _ or -."
            )

        webhook_endpoint = (
            WEBHOOK_URL
            if WEBHOOK_URL.endswith(WEBHOOK_PATH)
            else f"{WEBHOOK_URL}{WEBHOOK_PATH}"
        )
        await active_bot.set_webhook(
            url=webhook_endpoint,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        logger.info("Telegram webhook configured.")
    else:
        # Polling is convenient for local development.
        await active_bot.delete_webhook(drop_pending_updates=False)
        polling_task = asyncio.create_task(
            dp.start_polling(active_bot, handle_signals=False)
        )
        logger.info("Telegram polling started (local/development mode).")

    if not REQUIRED_CHANNELS:
        logger.warning("FORCE_CHANNELS is empty; Channel Force is not enabled.")

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
    description="Telegram bot preview with required-channel verification.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    return {
        "service": "USDT09 Telegram Bot",
        "status": "online" if active_bot is not None else "waiting_for_configuration",
        "telegram_bot_enabled": active_bot is not None,
        "features": "coming_soon",
    }


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "usdt09",
        "telegram_bot_enabled": active_bot is not None,
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
        raise HTTPException(
            status_code=404,
            detail="Webhook mode is not configured.",
        )

    if not WEBHOOK_SECRET or not hmac.compare_digest(
        telegram_secret or "",
        WEBHOOK_SECRET,
    ):
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
