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
1. Go to a normal text channel (not a thread) and run `/fireball start`
   • `code` — short game code, e.g. FB18
   • `prize` — each digit costs 1/10 of it (500 → 50 a digit)
   • `image` + `notes` — picture and anything it doesn't show (required)
   • `draw` — Midday (12:40 CT) / Evening (9:22 CT), blank = next
   • `bb` — on for **90% buyback** games
2. The bot opens a thread **[OPEN] - CODE** with the digit board.
3. Claims lock **10 min before the draw** (or tap **Close**).

**PLAYERS — claiming digits**
• Tap a digit **0–9** to claim it.
• 🎲 **YOLO** = random open digit. **Odds** / **Evens** = grab all of them.
• Only claim what you can cover.

**THE SATELLITE**
• Once **7 digits** are claimed, there's a **5-second** last call, then
  the remaining digits go to a **10-spot satellite** in its own thread.
• Type `X1`, `X2`, etc. in the satellite thread (or tap **X1**) to take
  spots. Tap **⭐ Preferences** to rank which open digits you want.
• When it fills, you get **30 seconds** to set preferences, then the
  ducks get lit on fire 🔥. The **last 3 still burning** each win a digit
  (best pick still free from their preferences, otherwise random).

**SIP**
• Sent the host your tickets? Tap **💸 Sip** on the board (or type `sip`).
  The host gets a DM to ✅ confirm or ❌ deny; you hear back privately.
• Host: **🔔 Remind sips** pings everyone who still owes.

**THE RESULT**
• Minutes after the draw the bot posts the result from
  illinoislottery.com and tags whoever holds the Fireball digit.
• Winner takes the prize. In **90% BB** games the post shows the buyback math.

**HOST / ADMIN TOOLS**
• `/fireball result` — re-check the site, or enter the digit by hand
• `/fireball notes` — update the host notes
• `/fireball-cancel` — **mods only**: cancel the board; **every claim is void**
• `/fireball config lock_minutes:` — admins: change the lock time
```
