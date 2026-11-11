import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import storage

logger = logging.getLogger("usdt09.admin")
admin_router = Router()


def _admin_ids():
    raw = __import__("os").getenv("ADMIN_IDS", "")
    ids = set()
    for value in raw.split(","):
        value = value.strip()
        if value.isdigit() and int(value) > 0:
            ids.add(int(value))
    legacy = __import__("os").getenv("ADMIN_ID", "").strip()
    if legacy.isdigit() and int(legacy) > 0:
        ids.add(int(legacy))
    return ids


def is_admin(user_id):
    return int(user_id or 0) in _admin_ids()


class AdminStates(StatesGroup):
    waiting_search = State()
    waiting_balance = State()
    waiting_setting = State()
    waiting_txid = State()
    waiting_reject_note = State()


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📊 Dashboard", "adm:dashboard"), _btn("👥 Users", "adm:users")],
        [_btn("🔎 Search user", "adm:search"), _btn("💸 Withdrawals", "adm:withdrawals")],
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
    buttons.extend([
        [_btn("🔎 Search", "adm:search")],
        [_btn("⬅️ Dashboard", "adm:dashboard")],
    ])
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
        f"⬇️ Minimum withdrawal: <b>{_fmt(settings['min_withdrawal_units'])} USDT</b>\n\n"
        "Choose a value to update. Amounts support up to 4 decimal places."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🎁 Change bonus amount", "adm:set:bonus_units")],
        [_btn("⏱ Change bonus interval", "adm:set:bonus_interval_seconds")],
        [_btn("⬇️ Change minimum withdrawal", "adm:set:min_withdrawal_units")],
        [_btn("⬅️ Dashboard", "adm:dashboard")],
    ])
    await _edit(target, text, keyboard)


@admin_router.callback_query(F.data.startswith("adm:"))
async def admin_callback(callback: CallbackQuery, state: FSMContext, bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Admin access required.", show_alert=True)
        return
    data = (callback.data or "").split(":")
    action = data[1] if len(data) > 1 else ""
    await callback.answer()

    if action == "dashboard":
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
    elif action == "set" and len(data) == 3 and data[2] in {"bonus_units", "bonus_interval_seconds", "min_withdrawal_units"}:
        await state.set_state(AdminStates.waiting_setting)
        await state.update_data(setting_key=data[2])
        prompt = "USDT amount (example 0.05)." if data[2] != "bonus_interval_seconds" else "Interval in seconds (minimum 60)."
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


@admin_router.message(AdminStates.waiting_search)
async def receive_search(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        await state.clear()
        return
    query = (message.text or "").strip()
    await state.clear()
    await show_users(message, query)


@admin_router.message(AdminStates.waiting_balance)
async def receive_balance_adjustment(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    amount = storage.parse_amount_units((message.text or "").strip())
    if amount is None:
        await message.answer("Invalid amount. Send a positive amount, e.g. <code>0.50</code>.", parse_mode="HTML")
        return
    delta = amount if data.get("balance_action") == "add" else -amount
    result, balance = storage.adjust_balance(data["balance_user_id"], delta, "admin_manual_adjustment", message.from_user.id)
    if result == "ok":
        await state.clear()
        await message.answer(f"✅ Balance updated. New balance: <b>{_fmt(balance)} USDT</b>.", parse_mode="HTML")
        await show_user(message, data["balance_user_id"])
    elif result == "insufficient":
        await message.answer(f"Insufficient balance. Current balance: <b>{_fmt(balance)} USDT</b>.", parse_mode="HTML")
    else:
        await state.clear()
        await message.answer("User not found.")


@admin_router.message(AdminStates.waiting_setting)
async def receive_setting(message: Message, state: FSMContext):
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
    else:
        value = storage.parse_amount_units(raw)
        if value is None:
            await message.answer("Invalid amount. Use a positive amount with up to 4 decimal places.")
            return
    storage.update_setting(key, value)
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
    try:
        await bot.send_message(row["telegram_id"], f"🔴 Your withdrawal #{wid} was rejected. The reserved amount has been returned to your balance.\nReason: {html.escape(note or 'Not specified')}", parse_mode="HTML")
    except Exception:
        pass
    await message.answer("✅ Withdrawal rejected. User balance has been refunded.")
    await show_withdrawal(message, wid)
