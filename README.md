# USDT09 Telegram Bot

USDT09 is a Telegram rewards and referral bot backed by PostgreSQL.

## User features

- Required-channel join and subscription verification.
- Available balance shown in the main menu.
- Configurable hourly bonus, protected by a two-question random Math CAPTCHA Mini App. The bonus is credited after both answers are correct. An optional AdsGram ad opens only when the user taps OK after verification.
- Referral links, a configurable USDT reward for each newly verified referral (default 0.01 USDT), and a verified-referral leaderboard.
- Two-step USDT BEP20 withdrawal requests (amount first, then wallet address).
- Withdrawal history with Pending, Approved, Rejected and Paid states.

## Manual withdrawal workflow

1. A user submits the withdrawal amount, then sends a BEP20 wallet address.
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
- Admin management: add and remove administrators by Telegram ID. The configured bot owner cannot be removed.
- Admin activity notifications: send detailed alerts for successful admin changes to a private Telegram group. Default chat ID: -5399347693; optionally override with ADMIN_AUDIT_CHAT_ID.
- Broadcast: send an administrator-approved message to all active registered users, with preview, confirmation, progress and stop controls.
- Referral rewards: credit the inviter's balance once when a referred user first becomes verified; the reward rate is configurable in Settings.
- Balance management: add or deduct USDT from a user's internal balance.
- Withdrawal management: separate Pending, Approved, Rejected and Paid lists; approve, reject/refund, and mark paid with TXID.
- Force Join channels: add or remove public channel usernames or private channel IDs and invite links from the admin panel.
- Settings: bonus amount, bonus interval and minimum withdrawal.
- Financial summary and transaction ledger records.

The public bot command /myid shows the caller's own Telegram ID so the owner can add it to ADMIN_IDS.

## PostgreSQL and Render

This project requires PostgreSQL. It no longer uses SQLite or the container filesystem for balances and withdrawals.

Environment variables:

- BOT_TOKEN — required Telegram bot token.
- DATABASE_URL — required PostgreSQL connection string. On Render, use a private connection string from the same region.
- ADMIN_IDS — comma-separated initial administrator Telegram IDs.
- BOT_OWNER_ID — the bot owner's Telegram ID. Set this explicitly so the owner is protected from administrator removal. If omitted, the bot uses legacy ADMIN_ID, then the first ID in ADMIN_IDS.
- ADMIN_AUDIT_CHAT_ID — optional private group ID for admin activity alerts; defaults to -5399347693. Add the bot to that group and allow it to send messages.
- WEBAPP_URL — HTTPS URL for the Math CAPTCHA Mini App; defaults to https://usdt09.onrender.com/app.
- ADSGRAM_BLOCK_ID — optional AdsGram Rewarded block ID. It is used only after the user finishes the math verification and taps OK. The bonus is credited before the optional ad opens.
- FORCE_CHANNELS — optional initial Force Join channel entries. After startup, administrators can add or remove channels from the private admin panel. Public examples: @yourchannel or https://t.me/yourchannel. Private channel format: -1001234567890|https://t.me/+INVITE_CODE|Channel title.
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


## Math CAPTCHA bonus flow

When a user taps **🎁 Bonus**, the bot checks the existing hourly-bonus timer and replies with a short message and an inline **🧮 Start Math CAPTCHA** Web App button. The Mini App serves two random questions with A/B/C choices. Questions and correct answers are stored and verified by the backend in PostgreSQL; client-side edits cannot award a bonus. After both answers are correct, the existing hourly bonus is credited and the Mini App shows a Verified page with the amount and new balance.

The Verified page shows an **OK** button. AdsGram is opened only when the user chooses to tap OK. It is optional: not tapping OK, closing the ad, or an unavailable ad does not remove or delay the already-credited bonus. Configure the numeric AdsGram Rewarded block ID from **Admin → Settings → Set AdsGram Block ID** after registering the TMA block with AdsGram.
