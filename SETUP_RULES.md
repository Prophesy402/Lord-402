# Setup Rules

Two paste-ready rule posts — one for duck races, one for Fireball. Each
section between the lines is sized to fit in a single Discord message, so
copy everything inside a block and pin it in that game's channel.

---

## 🦆 Duck Race — Setup Rules

```
🦆 **DUCK RACE — SETUP RULES** 🦆

**HOSTS — opening a race**
1. Go to a normal text channel (not a thread) and run `/create race`
   • `code` — short race code, e.g. ER13
   • `total_spots` — how many spots (1–200)
   • `image` — picture of what's being raced for (required)
   • `notes` — anything the picture doesn't show (required)
   • `ticket_name` — optional, what to call a spot (default "spot")
2. The bot posts the announcement and opens a thread named **[OPEN] - CODE**.
3. Spot calls open after a **30-second countdown**. Don't call early.

**PLAYERS — calling spots**
• Type **X** + a number: `X1`, `X2`, `X5`. Nothing else counts.
• Calls bigger than what's left get rounded down to what's left.
• `xclose` grabs every spot that's still open.
• `!status` shows who has what and who still owes a sip.

**SATELLITES (SATs)**
• At **70% full**, the host gets asked: reply `yes sats` or `no sats`.
• On `yes sats`, main-race calls pause and the host runs `/racesat`
  inside the race thread. SAT winners are moved into the main race
  automatically, then calls reopen.

**WHEN IT FILLS — SIP**
• Thread flips to **[PENDING]**. Everyone with a spot types `sip`
  (or `confirm` / `ready` / `in`) once you're squared away.
• `vouch for @name` — vouch for someone who hasn't sipped yet.
  A vouch lets the race run, but they still have to sip.
• Host/admin: `force sip @name` if someone forgot to type it.
• Host: `xconfirm` locks entries early (even before it's full).

**RUNNING IT (host or admin)**
1. Type `!race` — bot posts the original list (**don't** paste that one)
   and then the **SHUFFLED** list. Paste the **shuffled** list into the race.
2. Run it on duckrace-game.com and paste the results back in the thread.
   The bot catches them, even if Discord splits the paste.
3. Once every sip is in, type `xfinish` — thread goes **[CLOSED]** and locks.

Payments are handled **outside** the bot. Don't sip until you've paid.
```

---

## 🔥 Fireball — Setup Rules

```
🔥 **FIREBALL — SETUP RULES** 🔥
Played on the **Illinois Pick 3 Fireball** digit (0–9).

**HOSTS — opening a board**
1. In a normal text channel (not a thread) run `/fireball start`
   • `race_name` — e.g. RR15
   • `value` — each digit costs 1/10 of it (500 → 50 a digit)
   • `image` — picture for the announcement
   • `payment_link` — where to send 💰 (blank = your saved `/payinfo`)
   • `draw` — Midday / Evening (blank = next one)
   • `bb` — on for **90% buyback** games
2. The bot opens a thread with the digit board.
3. Claims lock **10 min before the draw** (or Host Controls → Close).

**PLAYERS — claiming digits**
• Tap a digit **0–9** to claim it.
• 🎲 **YOLO** = random digit. **Odds** / **Evens** = all of them.
• 🏁 **Close** = take every open digit. All 10 filled = no satellite.

**THE SATELLITE**
• Once **7 digits** are claimed, there's a **5-second** last call, then
  the remaining digits go to a **10-spot satellite** in its own thread.
• Tap **X1** to take a spot, **⭐ Preferences** to rank the open digits.
• When it fills, you get **30 seconds** to set preferences, then the
  ducks get lit on fire 🔥. The **last 3 still burning** each win a digit
  (best pick still free from their preferences, otherwise random).

**PAY + SIP**
• Save where you get paid once: `/payinfo set`. Host notes show it
  automatically, so players know where to send 💰.
• Sent the host your 💰? Tap **💸 Sip** (or type `sip`). The host gets a
  DM to ✅ confirm or ❌ deny; you hear back privately.

**THE RESULT**
• Minutes after the draw the bot posts the result and tags the winner.
• **90% BB** games: the winner taps **💰 Take 90% BB** and the host gets
  a DM with their pay info and what they're owed.

**HOST / ADMIN TOOLS**
• **Host Controls** (host + mods): release a spot someone didn't mean to
  call, force/remind sips, expected sips, close the board
• **Admin Controls** (mods): all of that plus cancel the board
• `/fireball result` — re-check the site, or enter the digit by hand
```
