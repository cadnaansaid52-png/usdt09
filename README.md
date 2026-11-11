# USDT09 Telegram Bot

A Telegram bot preview inspired by the supplied reference screenshots.

## Included in this version

- Required-channel join screen.
- **Verify** button that checks Telegram channel membership.
- Reply keyboard with:
  - 💰 Balance
  - 🎁 Bonus
  - 💸 Withdraw
  - 👥 Referrals
  - 🏆 Leaderboard
- Every menu feature intentionally responds with **Coming Soon**.

## Important: preview only

Balance, bonus, withdrawal, referral, leaderboard, wallet, payment, and reward features are **not implemented**. The bot does not collect deposits, promise earnings, or send USDT. This version only provides the Channel Force flow and the menu layout/placeholders.

## Requirements

- Python 3.10+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- The bot must be an **administrator** in every channel listed in `FORCE_CHANNELS`.

## Environment variables

| Variable | Required | Description |
|---|---:|---|
| `BOT_TOKEN` | Yes | Telegram bot token from BotFather |
| `FORCE_CHANNELS` | Recommended | Required channels, separated by semicolons |
| `WEBHOOK_URL` | Render webhook mode | Public base URL of this service, e.g. `https://your-service.onrender.com` |
| `WEBHOOK_SECRET` | With webhook | Secret containing only letters, numbers, underscore or hyphen |
| `LOG_LEVEL` | No | Defaults to `INFO` |

### FORCE_CHANNELS examples

Public channel (short format):

```text
@yourchannel
```

Multiple public channels:

```text
@channel_one;@channel_two
```

Private channel or custom display label:

```text
-1001234567890|https://t.me/+YOUR_INVITE_CODE|Private community
```

You can combine entries:

```text
@public_channel|https://t.me/public_channel|Public Channel;-1001234567890|https://t.me/+INVITE_CODE|Private Channel
```

For private channels, use the channel ID and a valid invite link. Do not include real bot tokens or secrets in GitHub files.

## Run locally

1. Create a virtual environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Set `BOT_TOKEN` and `FORCE_CHANNELS`.
4. Start the app:

   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```

When `WEBHOOK_URL` is empty, the app uses polling (convenient for local testing).

## Deploy on Render

Create a **Python Web Service** connected to this repository.

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Add `BOT_TOKEN` and `FORCE_CHANNELS` in Environment.
- For webhook mode, also set:
  - `WEBHOOK_URL` to the public base URL Render assigned to your service.
  - `WEBHOOK_SECRET` to a strong random value using letters, numbers, `_` and `-`.

The app sets the Telegram webhook during startup when `WEBHOOK_URL` is configured. If it is not configured, the app uses polling.

## Channel Force troubleshooting

If membership verification fails:

1. Confirm the channel ID or username is correct.
2. Add the bot as an administrator in each required channel.
3. Confirm the configured invite URL is valid.
4. Check the Render application logs for Telegram API errors.
