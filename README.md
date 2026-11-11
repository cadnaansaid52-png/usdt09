# USDT09 Telegram Bot

USDT09 is a Telegram rewards and referral bot backed by PostgreSQL.

## User features

- Required-channel join and subscription verification.
- Available balance shown in the main menu.
- Configurable hourly bonus.
- Referral links and verified-referral leaderboard.
- Withdrawal requests with supported USDT networks: BEP20, TRC20, TON and ERC20.
- Withdrawal history with Pending, Approved, Rejected and Paid states.

## Manual withdrawal workflow

1. A user submits an amount, network and wallet address.
2. The requested amount is reserved from the user's available balance.
3. The request appears in the private admin panel as **Pending**.
4. An administrator reviews and approves or rejects it.
5. After approval, an administrator sends USDT manually from the project's own wallet.
6. Only after checking that transfer, the administrator enters its real blockchain TXID and marks the request **Paid**.
7. If the request is rejected, the reserved amount is returned to the user's available balance.

The bot does not automatically send crypto or verify blockchain transfers. Admins must check the network, address, amount and TXID themselves. Never mark a withdrawal paid before the real transfer is confirmed.

## Admin panel

Configure one or more Telegram IDs in ADMIN_IDS (comma-separated). Administrators open the private panel with /admin.

Admin features:

- Dashboard: users, daily signups, banned users, available balances and withdrawal totals by status.
- User management: recent users, search by Telegram ID or username, user details, ban/unban.
- Balance management: add or deduct USDT from a user's internal balance.
- Withdrawal management: separate Pending, Approved, Rejected and Paid lists; approve, reject/refund, and mark paid with TXID.
- Settings: bonus amount, bonus interval and minimum withdrawal.
- Financial summary and transaction ledger records.

The public bot command /myid shows the caller's own Telegram ID so the owner can add it to ADMIN_IDS.

## PostgreSQL and Render

This project requires PostgreSQL. It no longer uses SQLite or the container filesystem for balances and withdrawals.

Environment variables:

- BOT_TOKEN — required Telegram bot token.
- DATABASE_URL — required PostgreSQL connection string. On Render, use a private connection string from the same region.
- ADMIN_IDS — comma-separated administrator Telegram IDs.
- FORCE_CHANNELS — required channel entries. Public examples: @yourchannel or https://t.me/yourchannel. Private channel format: -1001234567890|https://t.me/+INVITE_CODE|Channel title.
- WEBHOOK_URL — optional public service URL. When set, webhook mode is used.
- WEBHOOK_SECRET — required when webhook mode is enabled.
- LOG_LEVEL — optional; defaults to INFO.

Render build command:

pip install -r requirements.txt

Render start command:

uvicorn main:app --host 0.0.0.0 --port $PORT

The bot must be an administrator in every required channel so it can verify membership.

## Important database plan note

The USDT09 PostgreSQL resource was created on Render's Free plan for initial setup. Render reports that this free database expires on **December 11, 2026**. Upgrade or migrate it before that date to avoid losing service availability or data. Do not rely on an expiring free database for long-term financial records.

## Safety

Balances are internal records, not funds held in a custodial wallet by the bot. The project owner is responsible for funding and manually processing real withdrawals, keeping accurate accounting records, securing admin IDs and verifying blockchain transactions.
