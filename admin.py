import html
import logging
import re
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import storage

logger = logging.getLogger("usdt09.admin")
admin_router = Router()

# Private group used for detailed administrator activity notifications.
ADMIN_AUDIT_CHAT_ID = int(__import__("os").getenv("ADMIN_AUDIT_CHAT_ID", "-5399347693"))


def _get_admin_audit_chat_id():
    try:
        configured = storage.get_setting("admin_audit_chat_id", str(ADMIN_AUDIT_CHAT_ID))
        chat_id = int(configured)
        return chat_id if chat_id != 0 else ADMIN_AUDIT_CHAT_ID
    except (TypeError, ValueError, Exception):
        logger.exception("Could not read the configured admin activity log chat ID")
        return ADMIN_AUDIT_CHAT_ID


async def _notify_admin_change(bot: Bot, actor, action: str, details: str, target_user_id=None):
    actor_id = int(getattr(actor, "id", 0) or 0)
    actor_name = " ".join(
        part for part in (
            getattr(actor, "first_name", "") or "",
            getattr(actor, "last_name", "") or "",
        ) if part
    ).strip() or "Unknown admin"
    actor_username = getattr(actor, "username", None)
    actor_label = actor_name
    if actor_username:
        actor_label += f" (@{actor_username})"

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    detail_text = html.escape(str(details or "—"))[:2600]
    message_text = (
        "🔔 <b>USDT09 ADMIN ACTIVITY</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👮 <b>Admin:</b> {html.escape(actor_label)}\n"
        f"🆔 <b>Admin ID:</b> <code>{actor_id}</code>\n"
        f"🛠 <b>Action:</b> {html.escape(action)}\n"
        + (f"🎯 <b>Target ID:</b> <code>{int(target_user_id)}</code>\n" if target_user_id else "")
        + f"🕒 <b>Time:</b> <code>{timestamp}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>Details</b>\n{detail_text}"
    )
    chat_id = _get_admin_audit_chat_id()
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=message_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        # Do not undo a successful admin change if the log group is unavailable.
        logger.exception(
            "Could not send admin activity notification to chat %s",
            chat_id,
        )


def _admin_ids():
    return storage.get_admin_ids()


def is_admin(user_id):
    return storage.is_admin(user_id)


def _owner_id():
    return storage.get_owner_id()


class AdminStates(StatesGroup):
    waiting_search = State()
    waiting_balance = State()
    waiting_setting = State()
    waiting_txid = State()
    waiting_reject_note = State()
    waiting_channel = State()
    waiting_admin_id = State()


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📊 Dashboard", "adm:dashboard"), _btn("👥 Users", "adm:users")],
        [_btn("🔎 Search user", "adm:search"), _btn("💸 Withdrawals", "adm:withdrawals")],
        [_btn("📢 Force Join channels", "adm:channels")],
        [_btn("👮 Admins", "adm:admins")],
        [_btn("⚙️ Settings", "adm:settings"), _btn("📈 Ledger summary", "adm:ledger")],
    ])


def back_dashboard():
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Admin dashboard", "adm:dashboard")]])


def _fmt(units):
    return storage.format_units(int(units or 0), 4)


def _name(row):
    full = " ".join(part for part in [row.get("first_name") or "", row.get("last_name") or ""] if part).strip()
    if full:
        return full
    if row.get("username"):
        return "@" + str(row["username"])
    return f"User {row.get('telegram_id', 'unknown')}"


async def _edit(target, text, keyboard):
    if isinstance(target, Message):
        await target.answer(text, reply_markup=keyboard, parse_mode="HTML")
    elif target.message:
        try:
            await target.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            await target.message.answer(text, reply_markup=keyboard, parse_mode="HTML")


async def show_dashboard(target):
    stats = storage.get_dashboard_stats()
    settings = storage.get_settings()
    text = (
        "💎 <b>USDT09 CONTROL CENTER</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total users: <b>{stats['total_users']:,}</b>\n"
        f"🆕 Joined today: <b>{stats['joined_today']:,}</b>\n"
        f"🚫 Banned users: <b>{stats['banned_users']:,}</b>\n"
        f"💰 Available user balances: <b>{_fmt(stats['available_balance_units'])} USDT</b>\n\n"
        "💸 <b>WITHDRAWALS</b>\n"
        f"🟠 Pending: <b>{stats['pending_count']}</b> · {_fmt(stats['pending_units'])} USDT\n"
        f"🔵 Approved: <b>{stats['approved_count']}</b> · {_fmt(stats['approved_units'])} USDT\n"
        f"🟢 Paid: <b>{stats['paid_count']}</b> · {_fmt(stats['paid_units'])} USDT\n"
        f"🔴 Rejected: <b>{stats['rejected_count']}</b> · {_fmt(stats['rejected_units'])} USDT\n\n"
        "⚙️ <b>ACTIVE SETTINGS</b>\n"
        f"🎁 Bonus: <b>{_fmt(settings['bonus_units'])} USDT</b>\n"
        f"⏱ Bonus interval: <b>{settings['bonus_interval_seconds']} seconds</b>\n"
        f"⬇️ Minimum withdrawal: <b>{_fmt(settings['min_withdrawal_units'])} USDT</b>\n"
        "━━━━━━━━━━━━━━━━━━\nChoose an admin section."
    )
    await _edit(target, text, admin_menu())


@admin_router.message(Command("chatid"))
async def show_chat_id(message: Message):
    if message.chat.type == "private":
        await message.answer("Open the private group where this bot is a member and send /chatid there.")
        return
    await message.answer(
        "🆔 <b>Telegram group chat ID:</b>\n"
        f"<code>{message.chat.id}</code>\n\n"
        "Send this ID to the bot owner so the private admin activity log destination can be corrected.",
        parse_mode="HTML",
    )


@admin_router.message(Command("admin"))
async def admin_entry(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ This control panel is private.")
        return
    if message.chat.type != "private":
        await message.answer("Please open the bot privately and send /admin.")
        return
    await state.clear()
    await show_dashboard(message)


async def show_users(target, query=None):
    rows = storage.search_users(query, 15) if query is not None else storage.get_user_list(15)
    if not rows:
        await _edit(target, "No users found.", back_dashboard())
        return
    lines = ["👥 <b>USER MANAGEMENT</b>\n"]
    buttons = []
    for row in rows:
        uid = int(row["telegram_id"])
        status = "🚫" if row["is_banned"] else "✅"
        username = ("@" + row["username"]) if row.get("username") else str(uid)
        lines.append(f"{status} {html.escape(username)} · <code>{uid}</code> · {_fmt(row['balance_units'])} USDT")
        buttons.append([_btn(f"{status} {username[:24]} · {uid}", f"adm:user:{uid}")])
    buttons.append([_btn("⬅️ Dashboard", "adm:dashboard")])
    await _edit(target, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))


async def show_user(target, user_id):
    user = storage.get_user(user_id)
    if not user:
        await _edit(target, "User not found.", back_dashboard())
        return
    username = "@" + user["username"] if user.get("username") else "—"
    status = "BANNED" if user["is_banned"] else "ACTIVE"
    referral_count = storage.get_referral_count(user_id)
    text = (
        "👤 <b>USER DETAILS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Name: <b>{html.escape(_name(user))}</b>\n"
        f"Username: <b>{html.escape(username)}</b>\n"
        f"Telegram ID: <code>{int(user['telegram_id'])}</code>\n"
        f"Balance: <b>{_fmt(user['balance_units'])} USDT</b>\n"
        f"Verified: <b>{'YES' if user['verified'] else 'NO'}</b>\n"
        f"Status: <b>{status}</b>\n"
        f"Referrals: <b>{referral_count}</b>\n"
        f"Joined: <code>{user['created_at'].strftime('%Y-%m-%d %H:%M UTC') if user.get('created_at') else '—'}</code>\n"
    )
    ban_label = "✅ Unban user" if user["is_banned"] else "🚫 Ban user"
    ban_action = "unban" if user["is_banned"] else "ban"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("➕ Add balance", f"adm:add:{user_id}"), _btn("➖ Deduct balance", f"adm:deduct:{user_id}")],
        [_btn(ban_label, f"adm:{ban_action}:{user_id}")],
        [_btn("⬅️ Users", "adm:users"), _btn("🏠 Dashboard", "adm:dashboard")],
    ])
    await _edit(target, text, keyboard)


async def show_withdrawals(target, status="pending"):
    rows = storage.get_withdrawals(status, 15)
    title = status.upper()
    lines = [f"💸 <b>{html.escape(title)} WITHDRAWALS</b>\n"]
    buttons = []
    if not rows:
        lines.append("No requests in this section.")
    for row in rows:
        uid = int(row["telegram_id"])
        wid = int(row["id"])
        lines.append(
            f"#{wid} · <b>{_fmt(row['amount_units'])} USDT</b> · "
            f"{html.escape(row['network'])} · {html.escape(_name(row))} · <code>{uid}</code>"
        )
        buttons.append([_btn(f"#{wid} · {_fmt(row['amount_units'])} USDT · {row['network']}", f"adm:withdrawal:{wid}")])
    buttons.extend([
        [_btn("🟠 Pending", "adm:wlist:pending"), _btn("🔵 Approved", "adm:wlist:approved")],
        [_btn("🟢 Paid", "adm:wlist:paid"), _btn("🔴 Rejected", "adm:wlist:rejected")],
        [_btn("📚 All requests", "adm:wlist:all")],
        [_btn("⬅️ Dashboard", "adm:dashboard")],
    ])
    await _edit(target, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))


async def show_withdrawal(target, withdrawal_id):
    row = storage.get_withdrawal(withdrawal_id)
    if not row:
        await _edit(target, "Withdrawal not found.", back_dashboard())
        return
    status = str(row["status"]).upper()
    text = (
        f"💸 <b>WITHDRAWAL #{int(row['id'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"User: <b>{html.escape(_name(row))}</b>\n"
        f"Telegram ID: <code>{int(row['telegram_id'])}</code>\n"
        f"Amount: <b>{_fmt(row['amount_units'])} USDT</b>\n"
        f"Network: <b>{html.escape(row['network'])}</b>\n"
        f"Wallet: <code>{html.escape(row['wallet_address'])}</code>\n"
        f"Status: <b>{status}</b>\n"
        f"TXID: <code>{html.escape(row.get('txid') or 'Not added')}</code>\n"
        f"Note: {html.escape(row.get('admin_note') or '—')}\n"
        f"Created: <code>{row['created_at'].strftime('%Y-%m-%d %H:%M UTC')}</code>"
    )
    buttons = []
    if row["status"] == "pending":
        buttons.append([_btn("✅ Approve", f"adm:approve:{withdrawal_id}"), _btn("❌ Reject", f"adm:reject:{withdrawal_id}")])
    elif row["status"] == "approved":
        buttons.append([_btn("🟢 Mark PAID + TXID", f"adm:paid:{withdrawal_id}"), _btn("❌ Reject + refund", f"adm:reject:{withdrawal_id}")])
    buttons.extend([
        [_btn("⬅️ Pending", "adm:wlist:pending"), _btn("💸 Withdrawals", "adm:withdrawals")],
        [_btn("🏠 Dashboard", "adm:dashboard")],
    ])
    await _edit(target, text, InlineKeyboardMarkup(inline_keyboard=buttons))


async def show_settings(target):
    settings = storage.get_settings()
    text = (
        "⚙️ <b>BOT SETTINGS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎁 Bonus amount: <b>{_fmt(settings['bonus_units'])} USDT</b>\n"
        f"⏱ Bonus interval: <b>{settings['bonus_interval_seconds']} seconds</b>\n"
        f"⬇️ Minimum withdrawal: <b>{_fmt(settings['min_withdrawal_units'])} USDT</b>\n"
        f"👥 Referral reward: <b>{_fmt(settings['referral_reward_units'])} USDT</b> per verified referral\n\n"
        "Choose a value to update. Amounts support up to 4 decimal places."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🎁 Change bonus amount", "adm:set:bonus_units")],
        [_btn("⏱ Change bonus interval", "adm:set:bonus_interval_seconds")],
        [_btn("⬇️ Change minimum withdrawal", "adm:set:min_withdrawal_units")],
        [_btn("👥 Change referral reward", "adm:set:referral_reward_units")],
        [_btn("📢 Manage Force Join channels", "adm:channels")],
        [_btn("⬅️ Dashboard", "adm:dashboard")],
    ])
    await _edit(target, text, keyboard)


async def show_admins(target):
    admins = sorted(storage.get_admin_ids())
    owner_id = _owner_id()
    lines = [
        "👮 <b>ADMIN MANAGEMENT</b>",
        "━━━━━━━━━━━━━━━━━━",
        "Add administrators by Telegram ID. The bot owner is protected and cannot be removed.",
        "",
    ]
    buttons = []
    if not admins:
        lines.append("No administrators are registered.")
    for admin_id in admins:
        if owner_id and admin_id == owner_id:
            lines.append(f"🔒 <b>OWNER</b> · <code>{admin_id}</code>")
        else:
            lines.append(f"✅ Admin · <code>{admin_id}</code>")
            buttons.append([_btn(f"🗑 Remove {admin_id}", f"adm:admin:remove:{admin_id}")])
    buttons.extend([
        [_btn("➕ Add admin", "adm:admin:add")],
        [_btn("⬅️ Admin dashboard", "adm:dashboard")],
    ])
    await _edit(target, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))


def _force_channel_entries():
    raw = storage.get_setting("force_channels", __import__("os").getenv("FORCE_CHANNELS", ""))
    return [entry.strip() for entry in (raw or "").split(";") if entry.strip()]


async def show_force_channels(target):
    entries = _force_channel_entries()
    lines = [
        "📢 <b>FORCE JOIN CHANNELS</b>",
        "━━━━━━━━━━━━━━━━━━",
        "Users must join each channel listed here before using the bot.",
        "",
    ]
    buttons = []
    if entries:
        for index, entry in enumerate(entries):
            lines.append(f"<b>{index + 1}.</b> <code>{html.escape(entry)}</code>")
            buttons.append([_btn(f"🗑 Remove channel {index + 1}", f"adm:channel:remove:{index}")])
    else:
        lines.append("No channels configured yet.")

    buttons.extend([
        [_btn("➕ Add channel", "adm:channel:add")],
        [_btn("⬅️ Admin dashboard", "adm:dashboard")],
    ])
    await _edit(target, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))


def _normalize_force_channel_entry(value):
    raw = (value or "").strip()
    if not raw:
        return None

    parts = [part.strip() for part in raw.split("|")]
    raw_id = parts[0]

    if raw_id.startswith("@"):
        username = raw_id[1:].strip().strip("/")
        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            return f"@{username}"
        return None

    if raw_id.startswith("https://t.me/"):
        match = re.fullmatch(r"https://t\.me/([A-Za-z0-9_]{5,32})/?", raw_id)
        if match:
            return f"@{match.group(1)}"
        return None

    if re.fullmatch(r"-100[0-9]{5,18}", raw_id) and len(parts) >= 2:
        join_url = parts[1]
        if not re.fullmatch(r"https://t\.me/[A-Za-z0-9_+/-]+", join_url):
            return None
        title = parts[2] if len(parts) > 2 and parts[2] else raw_id
        if len(title) > 60 or any(char in title for char in ";|\r\n"):
            return None
        return f"{raw_id}|{join_url}|{title}"

    return None


@admin_router.callback_query(F.data.startswith("adm:"))
async def admin_callback(callback: CallbackQuery, state: FSMContext, bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Admin access required.", show_alert=True)
        return
    data = (callback.data or "").split(":")
    action = data[1] if len(data) > 1 else ""
    await callback.answer()

    if action == "admins":
        await state.clear()
        await show_admins(callback)
    elif action == "admin" and len(data) == 3 and data[2] == "add":
        await state.set_state(AdminStates.waiting_admin_id)
        await callback.message.answer(
            "Send the Telegram ID of the new administrator.\n"
            "The user can get their ID by sending /myid to the bot.",
        )
    elif action == "admin" and len(data) == 4 and data[2] == "remove" and data[3].isdigit():
        await state.clear()
        target_admin_id = int(data[3])
        owner_id = _owner_id()
        if owner_id and target_admin_id == owner_id:
            await callback.message.answer("🔒 The bot owner is protected and cannot be removed.")
        else:
            result = storage.remove_admin(target_admin_id, owner_id)
            if result == "ok":
                try:
                    await bot.set_my_commands(
                        [
                            BotCommand(command="start", description="Start the bot"),
                            BotCommand(command="help", description="How to use the bot"),
                        ],
                        scope=BotCommandScopeChat(chat_id=target_admin_id),
                    )
                except TelegramAPIError:
                    logger.info("Could not reset commands for removed admin %s", target_admin_id, exc_info=True)
            await callback.message.answer(
                "✅ Administrator removed." if result == "ok" else "That ID is not an active administrator."
            )
            if result == "ok":
                await _notify_admin_change(
                    bot,
                    callback.from_user,
                    "Administrator removed",
                    f"Removed administrator Telegram ID: {target_admin_id}",
                    target_user_id=target_admin_id,
                )
        await show_admins(callback)
    elif action == "channels":
        await state.clear()
        await show_force_channels(callback)
    elif action == "channel" and len(data) == 3 and data[2] == "add":
        await state.set_state(AdminStates.waiting_channel)
        await callback.message.answer(
            "Send a public channel username, for example <code>@mychannel</code>.\n"
            "For a private channel send: <code>-1001234567890|https://t.me/+INVITECODE|Channel name</code>.\n\n"
            "Make sure the bot is an administrator in the channel.",
            parse_mode="HTML",
        )
    elif action == "channel" and len(data) == 4 and data[2] == "remove" and data[3].isdigit():
        entries = _force_channel_entries()
        index = int(data[3])
        if 0 <= index < len(entries):
            removed = entries.pop(index)
            storage.update_setting("force_channels", ";".join(entries))
            await _notify_admin_change(
                bot,
                callback.from_user,
                "Force Join channel removed",
                f"Removed channel entry: {removed}",
            )
            await callback.message.answer(f"✅ Removed Force Join channel: <code>{html.escape(removed)}</code>", parse_mode="HTML")
        await show_force_channels(callback)
    elif action == "dashboard":
        await state.clear()
        await show_dashboard(callback)
    elif action == "users":
        await state.clear()
        await show_users(callback)
    elif action == "user" and len(data) == 3 and data[2].isdigit():
        await state.clear()
        await show_user(callback, int(data[2]))
    elif action == "search":
        await state.set_state(AdminStates.waiting_search)
        await callback.message.answer("Send a Telegram user ID or username (example: <code>123456789</code> or <code>@username</code>).", parse_mode="HTML")
    elif action in {"ban", "unban"} and len(data) == 3 and data[2].isdigit():
        target_id = int(data[2])
        if target_id in _admin_ids():
            await callback.answer("You cannot ban an admin.", show_alert=True)
            return
        changed = storage.set_user_banned(target_id, action == "ban", callback.from_user.id)
        if changed:
            await _notify_admin_change(
                bot,
                callback.from_user,
                "User banned" if action == "ban" else "User unbanned",
                f"Target Telegram ID: {target_id}",
                target_user_id=target_id,
            )
        try:
            await bot.send_message(target_id, "Your USDT09 account has been banned." if action == "ban" else "Your USDT09 account has been unbanned.")
        except Exception:
            pass
        await show_user(callback, target_id)
    elif action in {"add", "deduct"} and len(data) == 3 and data[2].isdigit():
        await state.set_state(AdminStates.waiting_balance)
        await state.update_data(balance_user_id=int(data[2]), balance_action=action)
        await callback.message.answer(
            f"Send the amount in USDT to {'add to' if action == 'add' else 'deduct from'} user <code>{data[2]}</code>.\nExample: <code>0.50</code>",
            parse_mode="HTML",
        )
    elif action == "withdrawals":
        await state.clear()
        await show_withdrawals(callback, "pending")
    elif action == "wlist" and len(data) == 3:
        await state.clear()
        await show_withdrawals(callback, data[2])
    elif action == "withdrawal" and len(data) == 3 and data[2].isdigit():
        await state.clear()
        await show_withdrawal(callback, int(data[2]))
    elif action == "approve" and len(data) == 3 and data[2].isdigit():
        wid = int(data[2])
        result = storage.set_withdrawal_status(wid, "approved", callback.from_user.id)
        if result == "ok":
            row = storage.get_withdrawal(wid)
            await _notify_admin_change(
                bot,
                callback.from_user,
                "Withdrawal approved",
                (
                    f"Request ID: #{wid}\n"
                    f"User ID: {row['telegram_id']}\n"
                    f"Amount: {_fmt(row['amount_units'])} USDT\n"
                    f"Network: {row['network']}\n"
                    f"Wallet address: {row['wallet_address']}\n"
                    "Status: pending → approved"
                ),
                target_user_id=row["telegram_id"],
            )
            try:
                await bot.send_message(row["telegram_id"], f"✅ Your withdrawal #{wid} has been approved and is awaiting manual transfer.")
            except Exception:
                pass
            await show_withdrawal(callback, wid)
        else:
            await callback.message.answer(f"Could not approve request: {result}.")
    elif action == "reject" and len(data) == 3 and data[2].isdigit():
        await state.set_state(AdminStates.waiting_reject_note)
        await state.update_data(withdrawal_id=int(data[2]))
        await callback.message.answer("Send a rejection reason. Send <code>-</code> for no reason.", parse_mode="HTML")
    elif action == "paid" and len(data) == 3 and data[2].isdigit():
        wid = int(data[2])
        row = storage.get_withdrawal(wid)
        if not row or row["status"] != "approved":
            await callback.message.answer("Only an approved request can be marked paid.")
            return
        await state.set_state(AdminStates.waiting_txid)
        await state.update_data(withdrawal_id=wid)
        await callback.message.answer(
            "Send the real blockchain transaction ID (TXID) after you have sent USDT manually.\n"
            "Do not mark as paid before checking the transfer.\n"
            "Send /cancel to cancel.",
        )
    elif action == "settings":
        await state.clear()
        await show_settings(callback)
    elif action == "set" and len(data) == 3 and data[2] in {"bonus_units", "bonus_interval_seconds", "min_withdrawal_units", "referral_reward_units"}:
        await state.set_state(AdminStates.waiting_setting)
        await state.update_data(setting_key=data[2])
        if data[2] == "bonus_interval_seconds":
            prompt = "Interval in seconds (minimum 60)."
        elif data[2] == "referral_reward_units":
            prompt = "Referral reward per verified referral in USDT (example 0.01). Send 0 to disable rewards."
        else:
            prompt = "USDT amount (example 0.05)."
        await callback.message.answer(prompt)
    elif action == "ledger":
        stats = storage.get_dashboard_stats()
        await _edit(callback,
            "📈 <b>FINANCIAL SUMMARY</b>\n"
            f"Available balances: <b>{_fmt(stats['available_balance_units'])} USDT</b>\n"
            f"Pending: <b>{_fmt(stats['pending_units'])} USDT</b>\n"
            f"Approved: <b>{_fmt(stats['approved_units'])} USDT</b>\n"
            f"Paid total: <b>{_fmt(stats['paid_units'])} USDT</b>\n"
            f"Rejected total: <b>{_fmt(stats['rejected_units'])} USDT</b>",
            back_dashboard(),
        )


@admin_router.message(AdminStates.waiting_admin_id, F.text, ~F.text.startswith("/"))
async def receive_admin_id(message: Message, state: FSMContext, bot: Bot):
    if not message.from_user or not is_admin(message.from_user.id):
        await state.clear()
        return

    raw_id = (message.text or "").strip()
    if not raw_id.isdigit() or not 0 < int(raw_id) <= 9223372036854775807:
        await message.answer("⚠️ Send a valid positive Telegram numeric ID.")
        return

    target_id = int(raw_id)
    if storage.is_admin(target_id):
        await state.clear()
        await message.answer("This user is already an administrator.")
        await show_admins(message)
        return

    result = storage.add_admin(target_id, message.from_user.id)
    await state.clear()
    if result == "ok":
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Start the bot"),
                    BotCommand(command="help", description="How to use the bot"),
                    BotCommand(command="admin", description="Open admin control panel"),
                ],
                scope=BotCommandScopeChat(chat_id=target_id),
            )
        except TelegramAPIError:
            logger.info(
                "Admin %s added; their chat commands will be set after they open the bot.",
                target_id,
                exc_info=True,
            )
        await _notify_admin_change(
            bot,
            message.from_user,
            "Administrator added",
            f"Added administrator Telegram ID: {target_id}",
            target_user_id=target_id,
        )
        await message.answer(
            f"✅ Administrator <code>{target_id}</code> added. "
            "Ask them to open the bot and send /start, then use /admin.",
            parse_mode="HTML",
        )
    else:
        await message.answer("⚠️ I couldn't add that administrator.")
    await show_admins(message)


@admin_router.message(AdminStates.waiting_channel, F.text, ~F.text.startswith("/"))
async def receive_force_channel(message: Message, state: FSMContext, bot: Bot):
    if not message.from_user or not is_admin(message.from_user.id):
        await state.clear()
        return

    entry = _normalize_force_channel_entry(message.text or "")
    if not entry:
        await message.answer(
            "⚠️ Invalid channel details.\n"
            "Public channel: <code>@mychannel</code>\n"
            "Private channel: <code>-1001234567890|https://t.me/+INVITECODE|Channel name</code>",
            parse_mode="HTML",
        )
        return

    entries = _force_channel_entries()
    new_id = entry.split("|", 1)[0].lower()
    if any(existing.split("|", 1)[0].lower() == new_id for existing in entries):
        await state.clear()
        await message.answer("That channel is already in the Force Join list.")
        await show_force_channels(message)
        return

    entries.append(entry)
    storage.update_setting("force_channels", ";".join(entries))
    await _notify_admin_change(
        bot,
        message.from_user,
        "Force Join channel added",
        f"Added channel entry: {entry}",
    )
    await state.clear()
    await message.answer(
        "✅ Force Join channel added. The bot must be an administrator in this channel.",
        parse_mode="HTML",
    )
    await show_force_channels(message)


@admin_router.message(AdminStates.waiting_search)
async def receive_search(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        await state.clear()
        return
    query = (message.text or "").strip()
    await state.clear()
    await show_users(message, query)


@admin_router.message(AdminStates.waiting_balance)
async def receive_balance_adjustment(message: Message, state: FSMContext, bot: Bot):
    if not message.from_user or not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    amount = storage.parse_amount_units((message.text or "").strip())
    if amount is None:
        await message.answer("Invalid amount. Send a positive amount, e.g. <code>0.50</code>.", parse_mode="HTML")
        return
    delta = amount if data.get("balance_action") == "add" else -amount
    target_id = int(data["balance_user_id"])
    previous_balance = storage.get_balance_units(target_id)
    result, balance = storage.adjust_balance(target_id, delta, "admin_manual_adjustment", message.from_user.id)
    if result == "ok":
        await _notify_admin_change(
            bot,
            message.from_user,
            "User balance increased" if delta > 0 else "User balance decreased",
            (
                f"Target Telegram ID: {target_id}\n"
                f"Change: {'+' if delta > 0 else '-'}{_fmt(abs(delta))} USDT\n"
                f"Previous balance: {_fmt(previous_balance)} USDT\n"
                f"New balance: {_fmt(balance)} USDT"
            ),
            target_user_id=target_id,
        )
        await state.clear()
        await message.answer(f"✅ Balance updated. New balance: <b>{_fmt(balance)} USDT</b>.", parse_mode="HTML")
        await show_user(message, data["balance_user_id"])
    elif result == "insufficient":
        await message.answer(f"Insufficient balance. Current balance: <b>{_fmt(balance)} USDT</b>.", parse_mode="HTML")
    else:
        await state.clear()
        await message.answer("User not found.")


@admin_router.message(AdminStates.waiting_setting)
async def receive_setting(message: Message, state: FSMContext, bot: Bot):
    if not message.from_user or not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    key = data.get("setting_key")
    raw = (message.text or "").strip()
    if key == "bonus_interval_seconds":
        if not raw.isdigit() or not 60 <= int(raw) <= 2592000:
            await message.answer("Send whole seconds from 60 to 2592000 (30 days).")
            return
        value = int(raw)
    elif key == "referral_reward_units" and re.fullmatch(r"0+(?:\.0{1,4})?", raw):
        value = 0
    else:
        value = storage.parse_amount_units(raw)
        if value is None:
            await message.answer("Invalid amount. Use up to 4 decimal places. Referral reward can also be set to 0 to disable it.")
            return
    old_value = storage.get_settings().get(key)
    storage.update_setting(key, value)
    if key == "bonus_interval_seconds":
        old_display = f"{old_value} seconds"
        new_display = f"{value} seconds"
        setting_label = "Bonus interval"
    else:
        old_display = f"{_fmt(old_value)} USDT"
        new_display = f"{_fmt(value)} USDT"
        setting_label = {
            "bonus_units": "Bonus amount",
            "min_withdrawal_units": "Minimum withdrawal",
            "referral_reward_units": "Referral reward rate",
        }.get(key, key)
    await _notify_admin_change(
        bot,
        message.from_user,
        f"Setting changed: {setting_label}",
        f"Previous value: {old_display}\nNew value: {new_display}",
    )
    await state.clear()
    await message.answer("✅ Setting saved.")
    await show_settings(message)


@admin_router.message(AdminStates.waiting_txid)
async def receive_txid(message: Message, state: FSMContext, bot):
    if not message.from_user or not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    txid = (message.text or "").strip()
    if not 10 <= len(txid) <= 200 or any(ch.isspace() for ch in txid):
        await message.answer("TXID must be 10–200 characters with no spaces.")
        return
    wid = int(data["withdrawal_id"])
    result = storage.set_withdrawal_status(wid, "paid", message.from_user.id, txid=txid)
    if result != "ok":
        await message.answer(f"Could not mark paid: {result}.")
        return
    await state.clear()
    row = storage.get_withdrawal(wid)
    await _notify_admin_change(
        bot,
        message.from_user,
        "Withdrawal marked paid",
        (
            f"Request ID: #{wid}\n"
            f"User ID: {row['telegram_id']}\n"
            f"Amount: {_fmt(row['amount_units'])} USDT\n"
            f"Network: {row['network']}\n"
            f"Wallet address: {row['wallet_address']}\n"
            f"TXID: {txid}\n"
            "Status: approved → paid"
        ),
        target_user_id=row["telegram_id"],
    )
    try:
        await bot.send_message(row["telegram_id"], f"🟢 Your withdrawal #{wid} is marked PAID.\nTXID: <code>{html.escape(txid)}</code>", parse_mode="HTML")
    except Exception:
        pass
    await message.answer("✅ Withdrawal marked paid. TXID saved.")
    await show_withdrawal(message, wid)


@admin_router.message(AdminStates.waiting_reject_note)
async def receive_reject_note(message: Message, state: FSMContext, bot):
    if not message.from_user or not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    wid = int(data["withdrawal_id"])
    note = (message.text or "").strip()
    if note == "-":
        note = ""
    if len(note) > 300:
        await message.answer("Keep the reason under 300 characters.")
        return
    result = storage.set_withdrawal_status(wid, "rejected", message.from_user.id, note=note)
    if result != "ok":
        await message.answer(f"Could not reject request: {result}.")
        return
    await state.clear()
    row = storage.get_withdrawal(wid)
    await _notify_admin_change(
        bot,
        message.from_user,
        "Withdrawal rejected and refunded",
        (
            f"Request ID: #{wid}\n"
            f"User ID: {row['telegram_id']}\n"
            f"Refunded amount: {_fmt(row['amount_units'])} USDT\n"
            f"Network: {row['network']}\n"
            f"Wallet address: {row['wallet_address']}\n"
            f"Reason: {note or 'Not specified'}\n"
            "Status: pending/approved → rejected"
        ),
        target_user_id=row["telegram_id"],
    )
    try:
        await bot.send_message(row["telegram_id"], f"🔴 Your withdrawal #{wid} was rejected. The reserved amount has been returned to your balance.\nReason: {html.escape(note or 'Not specified')}", parse_mode="HTML")
    except Exception:
        pass
    await message.answer("✅ Withdrawal rejected. User balance has been refunded.")
    await show_withdrawal(message, wid)
