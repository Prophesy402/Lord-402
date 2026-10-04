# Duck Race Bot

A Discord bot that runs the logistics of a duck race — capacity tracking,
confirmations, shuffling, and result capture — the same way "Duck Don"
does in the screenshots this was modeled on, **minus the money**. This
bot does not collect or track payments; if your race has an entry fee,
settle that yourselves outside the bot (Venmo, Cash App, cash, whatever),
then have people type `confirm` once they're squared away.

One race is tracked per channel.

## How a race flows

1. **Host opens the race**: `/duck open code:ER13 size:10`
   Channel is renamed to `[OPEN] - ER13`.
2. **People call spots** by typing a bare number or `X`+number in the
   channel — `X2`, `x1`, `3`, etc. The bot replies with their running
   total and spots remaining, live, the same way Duck Don does.
3. **When the race fills up**, the bot announces it, lists who has how
   many spots, and renames the channel to `[PENDING] - ER13`.
4. **Everyone who called a spot types `confirm`** (or `confirmed`,
   `ready`, `in`) in the channel. The bot acknowledges each one; once
   everyone's confirmed, it renames the channel to `[CLOSED] - ER13`
   and says the race is ready.
5. **Host runs `/duck race`**. The bot posts the full entry list (one
   line per spot, grouped by person — `Alice (1)`, `Alice (2)`, `Bob`),
   then shuffles every individual spot into a random order and posts a
   paste-ready numbered list.
6. **Host copies that list into your race tool** (e.g.
   [duckrace-game.com](https://www.duckrace-game.com)), runs the race,
   and pastes the results back into the channel. The bot automatically
   detects the pasted results and reassembles them even if Discord
   splits a long paste into several messages (it waits ~8 seconds after
   the last fragment before finalizing).
7. **`/duck reset`** clears the channel so it's ready for the next race.

You can also type `!race` as a shortcut for step 5.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/duck open <code> <size>` | Manage Messages perm | Start a new race in this channel |
| `/duck status` | anyone | Show current entries, remaining spots, and who's confirmed |
| `/duck confirm` | anyone with a spot | Confirm you're squared away (or just type `confirm` in chat) |
| `/duck race` | Manage Messages perm | Post the entry list and shuffle the order |
| `/duck reset` | Manage Messages perm | Clear the race in this channel |

`/duck open`, `/duck race`, and `/duck reset` are restricted to members
with the **Manage Messages** permission (or Administrator) so randoms
can't hijack a race — everyone can call spots, confirm, and check status.

## 1. Create the bot in Discord

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**. Name it (e.g. "Duck Race Bot").
2. In the left sidebar, open **Bot** → click **Reset Token** (or **Add Bot**) → copy the token. You'll need it below. Keep it secret — anyone with it controls your bot.
3. Still under **Bot**, scroll to **Privileged Gateway Intents** and turn **ON**:
   - **Message Content Intent** — required, since the bot reads plain messages like `X2` and `confirm`, not just slash commands.
4. In the left sidebar, open **OAuth2 → URL Generator**:
   - Scopes: check `bot` and `applications.commands`
   - Bot Permissions: check `Send Messages`, `Read Message History`, and `Manage Channels` (needed for the `[OPEN]`/`[PENDING]`/`[CLOSED]` renaming)
5. Copy the generated URL at the bottom, open it in a browser, and choose the server to add the bot to.

## 2. Run the bot

Requires Python 3.10+.

```bash
cd duckbot
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and paste your bot token into DISCORD_TOKEN

python3 bot.py
```

If it starts correctly you'll see:
```
Logged in as Duck Race Bot#1234 (id=...)
Synced 5 slash command(s)
```

Slash commands can take up to an hour to show up globally the first
time, but typically appear within a minute or two.

## Hosting it long-term

Running `python3 bot.py` locally only keeps the bot online while that
terminal is open. To keep it running continuously, host it on a small
always-on machine or service — a spare Linux box, a cheap VPS (e.g. a
DigitalOcean droplet), or a platform like Railway or Fly.io. Install the
requirements, set the `DISCORD_TOKEN` environment variable, and run
`python3 bot.py`, ideally under a process manager (`systemd`, `pm2`, or
the platform's own restart policy) so it comes back up if it crashes.

## Notes on how the pieces work

- **State** is stored per-channel in `data/races.json`, so a race
  survives a bot restart. Only one race can be open per channel at a
  time — reset before opening a new one.
- **Spot calls** only work while a race is `open`. The bot rejects a
  call that would exceed the remaining spots and tells you how many are
  actually left.
- **Confirmation** only counts people who actually called a spot; a
  bystander typing "confirm" is ignored.
- **Result capture** is a heuristic: while the race is `shuffled`, any
  message with 3+ lines matching `1. something` / `2. something...` or
  containing the word "winner" is treated as (the start of) results.
  Follow-up messages from the same author within 8 seconds are appended
  before the bot finalizes and posts the captured results. If your race
  tool's output doesn't look like that, just tell the bot the result
  manually in whatever format you like — the auto-capture is a
  convenience, not a requirement, and `/duck reset` always works
  regardless of what got captured.
- Renaming channels requires the bot to have the **Manage Channels**
  permission in that channel; without it, the bot logs a warning and
  keeps going (races still work, the channel name just won't update).
