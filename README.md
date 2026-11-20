# Math CAPTCHA Telegram Mini App

This project is now a Telegram Mini App built around a short, ad-supported math challenge. The bot opens the Mini App from its **🧮 Open Math CAPTCHA** keyboard button.

## User experience

- Each round contains exactly **two random math questions**.
- Every question shows a target number and three multiple-choice expressions (A, B and C). One expression equals the target.
- Every answer attempt triggers a real AdsGram **Rewarded** ad.
- The selected answer is revealed only after AdsGram confirms that the rewarded ad was completed. If the ad is skipped, unavailable, or errors, the app does not count it as completed and offers a retry.
- A correct answer advances to the next question. An incorrect answer lets the user retry the same question.
- There are no cash rewards, USDT balances, or public withdrawal features in the new user flow.

The sample question format is:

```text
18

A) 8 + 8
B) 8 + 10
C) 5 + 8
```

The correct expression is B because it equals 18. Questions are generated randomly in the Mini App.

## AdsGram setup

1. Create/approve a Mini App ad block in your AdsGram publisher account: https://partner.adsgram.ai
2. Copy the numeric **blockId** for the Rewarded format.
3. Set `ADSGRAM_BLOCK_ID` in the Render service environment.
4. Restart/redeploy the service, then open the Mini App in Telegram and test a real ad.

The SDK is loaded from `https://sad.adsgram.ai/js/sad.min.js`. The app proceeds only after the AdsGram Rewarded SDK reports completion. The app does not ask users to click ads. AdsGram determines the actual ad duration; the integration cannot promise that every ad lasts exactly 15 seconds. Test/debug ads are not counted as production impressions.

## Telegram Mini App

- App route: `/app`
- Static assets: `/miniapp/`
- Public SDK configuration: `/api/miniapp/config`
- Telegram session verification: `/api/miniapp/session`

The backend validates Telegram `initData` using the bot token and rejects invalid or expired sessions. The app uses Telegram's WebApp interface and opens from the bot's reply-keyboard Web App button.

## Environment variables

- `BOT_TOKEN` — required Telegram bot token.
- `DATABASE_URL` — required PostgreSQL connection string (used by the bot's existing user/admin records).
- `ADMIN_IDS` — comma-separated administrator Telegram IDs.
- `BOT_OWNER_ID` — protected bot owner Telegram ID.
- `WEBAPP_URL` — HTTPS Mini App URL; defaults to `https://usdt09.onrender.com/app`.
- `ADSGRAM_BLOCK_ID` — numeric AdsGram Rewarded ad block ID; leave empty until AdsGram has issued one.
- `ADMIN_AUDIT_CHAT_ID` — optional private admin-log group ID.
- `FORCE_CHANNELS` — optional required-channel entries.
- `WEBHOOK_URL`, `WEBHOOK_SECRET` — optional Telegram webhook configuration.
- `LOG_LEVEL` — optional log level.

## Render

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

The existing PostgreSQL/admin records are retained for continuity. The public user menu no longer exposes the old bonus, balance, or withdrawal workflow.
