# USDT09 Telegram Bot

USDT09 is a demo-only Telegram bot preview with Channel Force, hourly demo credits, referrals, a leaderboard, and simulated withdrawal receipts.

## Important: demo only

- The hourly 0.0500 USDT value is a **demo credit**, not money.
- Withdrawals only create a simulated record and a clearly labelled demo receipt.
- The bot does not send USDT, interact with the TON blockchain, or create transaction hashes.
- The receipt page is not TonScan and clearly states that no on-chain transfer occurred.
- Do not advertise these demo balances or receipts as real payouts.

## Features

- Required-channel join and Verify.
- Dynamic balance keyboard (for example: `💰 0.00 USDT`).
- One hourly demo bonus of 0.0500 per verified user.
- Cooldown response with remaining hours/minutes/seconds.
- Referral deep links and verified referral counts.
- Top 10 leaderboard based on verified referrals.
- Demo withdrawal flow: amount + address, 0.05 minimum.
- Optional demo receipt message to a Telegram group.
- Responsive demo receipt web page.

## Environment variables

| Variable | Required | Description |
|---|---:|---|
| `BOT_TOKEN` | Yes | Telegram bot token |
| `FORCE_CHANNELS` | Yes | Required public/private channels |
| `WEBHOOK_URL` | Optional | Public service URL; enables webhook mode |
| `WEBHOOK_SECRET` | Required if webhook | Telegram webhook secret |
| `PUBLIC_BASE_URL` | Recommended | Public service base URL for demo receipt links |
| `DEMO_PROOF_CHAT_ID` | Optional | Group/chat ID to receive demo withdrawal notices |
| `DATABASE_PATH` | No | SQLite file path |
| `LOG_LEVEL` | No | Defaults to INFO |

### FORCE_CHANNELS examples

Public channel:
`@yourchannel`

Public channel full link:
`https://t.me/yourchannel`

Multiple channels:
`@channel_one;@channel_two`

Private channel:
`-1001234567890|https://t.me/+INVITE_CODE|Private channel`

The bot must be an administrator in every required channel and the optional demo group.

## Deploy on Render

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Add the environment variables in the Render Dashboard.
- Set `PUBLIC_BASE_URL=https://your-service.onrender.com`.
- If using `DEMO_PROOF_CHAT_ID`, add the bot to that group and give it permission to send messages.

If `WEBHOOK_URL` is empty, the app uses polling. If `WEBHOOK_URL` is set, `WEBHOOK_SECRET` must also be set.

## Persistence note

This version uses SQLite. Render Free services have ephemeral local filesystems, so database records (users, balances, bonuses and demo receipts) can be lost when the service is redeployed or restarted. Use a persistent database or persistent disk before relying on long-term records. Do not treat the demo ledger as real money or a payment system.
