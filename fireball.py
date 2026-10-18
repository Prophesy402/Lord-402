"""
Fireball (Illinois Pick 3) side game for Lord 402
-------------------------------------------------
A separate game from the duck races. It lives entirely in this module and its
own data file (data/fireball.json). The only link to bot.py is one line that
loads this file as an extension; nothing in the duck race code is touched.

How a game runs
  1. Host:  /fireball start prize:"$20 Venmo"        (draw: midday or evening)
       -> bot posts a board with buttons for digits 0-9 plus YOLO / Odds /
          Evens / Close. Tapping a digit claims it (costs tickets).
  2. When 7 of the 10 digits are claimed, the last 3 digits are locked and a
     10-spot satellite opens. Whoever fills it triggers the shuffle (3-12
     rounds) and the "ducks on fire" finale. The 3 surviving ducks each win
     one of the 3 open digits. The outcome is random; the animation is visual.
  3. At draw time the board locks. A few minutes after the draw the bot reads
     the result from illinoislottery.com, posts it and tags whoever held the
     Fireball digit. (Winner takes the one prize the host set.)

Tickets are a game-only currency, separate from duck races. Players are
expected to hold enough to cover whatever they call, so the bot does not keep
balances or block anyone; it just records what each claim costs. Cancelling a
board voids every claim.

Version 1 is Fireball-only, winner take all. A later "multi" mode (paying
Fireball + 1st/2nd/3rd ball) can be added without touching the existing flow;
each board already stores a `mode` field for that.
"""
from __future__ import annotations

import asyncio
import html as htmllib
import io
import json
import logging
import math
import random
import re
import secrets
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("duckbot.fireball")

DATA_PATH = Path(__file__).parent / "data" / "fireball.json"
TZ = ZoneInfo("America/Chicago")

# Illinois Pick 3 draw times (Central). Midday ~12:40 PM, evening ~9:22 PM.
DRAW_TIMES = {"midday": dtime(12, 40), "evening": dtime(21, 22)}
RESULTS_URL = "https://www.illinoislottery.com/dbg/results/pick3"
RESULT_FIRST_TRY_MIN = 4      # start looking this many minutes after the draw
RESULT_RETRY_SECONDS = 180    # then retry about every 3 minutes
RESULT_GIVE_UP_HOURS = 6      # then ask the host to enter it by hand

SAT_THRESHOLD = 7             # digits claimed before the satellite opens
SAT_SPOTS = 10
SAT_PREF_DELAY = 30           # seconds between the satellite filling and the shuffle (time to set preferences)
SAT_DELAY = 5                 # seconds after the 7th digit before the satellite opens (last chance for others)
SIP_WORDS = {"sip", "sipped"}                               # "I've sent the host my tickets"
REMIND_COOLDOWN = 60                                        # seconds between 🔔 Remind sips presses
MIN_ROUNDS, MAX_ROUNDS = 3, 12
SHUFFLE_DELAY = 2.5           # seconds between shuffle rounds
FIRE_DELAY = 2.2              # seconds between finale edits (text fallback only)
FINALE_WAIT_SCALE = 1.0       # 1.0 = wait for the race GIF to finish playing before revealing winners

DEFAULT_CONFIG = {
    "lock_minutes": 10,     # board locks this many minutes before the draw
}
CONFIG_LIMITS = {
    "lock_minutes": (0, 240),
}

ACTIVE_STATUSES = ("open", "satellite", "animating")

USE_WEBHOOK = True            # off when the bot running this is Pyro Steve himself (steve.py sets bot.is_pyro_steve)
PERSONA_NAME = "Pyro Steve"
PERSONA_AVATAR = Path(__file__).with_name("pyro_steve.png")
STEVE_LINES = [
    "Quack chaos!", "Burnin' happy!", "Every day I'm flaming awesome!",
    "*sips bourbon, ducks on fire*", "Numbers go brrr, feathers go crispy.",
    "Quack quack, hold my bourbon!", "Smells like victory and singed feathers.",
    "The thread is NOT on fire. (The thread is a little on fire.)",
]


def steve() -> str:
    return random.choice(STEVE_LINES)


BURN_QUIPS = [
    "A flame sputters out...",
    "Somebody's duck just ran out of luck...",
    "Another duck bites the dust...",
    "The fire has chosen...",
    "That's one less duck on the pond...",
    "Quack. Cough. Gone.",
    "A little puff of smoke is all that's left...",
    "No mercy in the flames tonight...",
]


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

class Store:
    """All state in memory, saved atomically to one JSON file after changes."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.data = self._load()

    def _load(self) -> dict:
        data: dict = {}
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                log.exception("Could not read %s; starting fresh", self.path)
                data = {}
        data.setdefault("guilds", {})
        data.setdefault("boards", {})
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        tmp.replace(self.path)


# --------------------------------------------------------------------------
# Pure game logic (no Discord objects in here, so it is easy to test)
# --------------------------------------------------------------------------

def today_ct() -> date:
    return datetime.now(TZ).date()


def guild_state(data: dict, guild_id: int | str) -> dict:
    g = data["guilds"].setdefault(str(guild_id), {})
    g.setdefault("config", {})
    return g


def get_config(data: dict, guild_id: int | str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(guild_state(data, guild_id)["config"])
    return cfg


def charge(board: dict, user_id: str, amount: int, kind: str, slot: str) -> None:
    """Record what a claim costs. Players are trusted to cover it; nothing is blocked."""
    if amount:
        board["ledger"].append({"user_id": str(user_id), "amount": amount, "kind": kind, "slot": slot})


def void_charges(board: dict, kinds: tuple[str, ...] | None = None) -> int:
    """Drop recorded charges (all, or only certain kinds). Returns the total voided."""
    total = 0
    keep = []
    for entry in board["ledger"]:
        if kinds is None or entry["kind"] in kinds:
            total += entry["amount"]
        else:
            keep.append(entry)
    board["ledger"] = keep
    return total


def owed(board: dict, user_id: str) -> int:
    return sum(e["amount"] for e in board["ledger"] if e["user_id"] == str(user_id))


def fmt_num(x: float) -> str:
    return f"{x:,.2f}".rstrip("0").rstrip(".") if x != int(x) else f"{int(x):,}"


def ticket_terms(board: dict) -> list[str]:
    """One term per digit sold, in order, with the satellite spots grouped in brackets at the end."""
    terms: list[str] = []
    sats: list[str] = []
    for e in board["ledger"]:
        if e["kind"] == "sat":
            sats.append(fmt_num(e["amount"]))
        else:
            slots = [x for x in str(e["slot"]).split(",") if x] or [""]
            for _ in slots:
                terms.append(fmt_num(e["amount"] / len(slots)))
    if sats:
        terms.append("(" + " + ".join(sats) + ")")
    return terms


def ticket_total(board: dict) -> int:
    return sum(e["amount"] for e in board["ledger"])


def ticket_names(board: dict) -> dict[str, str]:
    names: dict[str, str] = {}
    for c in board["digits"].values():
        if c:
            names[c["user_id"]] = c["name"]
    for sp in board["sat"]["spots"]:
        if sp:
            names[sp["user_id"]] = sp["name"]
    return names


def result_text(board: dict, holder: dict | None, fireball: int) -> str:
    """The post under the result: congrats, the buyback math (90% BB games) and every player's tickets."""
    total = ticket_total(board)
    dc = board["digit_cost"]
    lines: list[str] = []
    if holder:
        via = " (won it in the satellite 🦆)" if holder.get("via") == "satellite" else ""
        if board.get("bb"):
            bb = total * 0.9
            lines.append(
                f"Congrats <@{holder['user_id']}>! Total expected tickets were **{fmt_num(total)}**, therefore the buyback is "
                f"**{fmt_num(bb)}**, or **{fmt_num(max(0, bb - dc))}** if the winner hasn't sipped.\n"
                f"**Here's the math:**\n```\nTotal Tickets: {' + '.join(ticket_terms(board)) or '0'} = {fmt_num(total)}\n"
                f"Buy back: {fmt_num(total)} * .9 = {fmt_num(bb)}\n"
                f"Buyback if winner didn't sip: {fmt_num(bb)} - {fmt_num(dc)} = {fmt_num(max(0, bb - dc))}\n```")
        else:
            lines.append(f"🏆 Quack chaos! Congrats <@{holder['user_id']}>! Digit **{fireball}**{via} wins the **{esc(str(board['prize']))}** prize!")
    else:
        lines.append(f"Nobody held digit **{fireball}**. No winner this draw.")
    names = ticket_names(board)
    per: dict[str, int] = {}
    for e in board["ledger"]:
        per[e["user_id"]] = per.get(e["user_id"], 0) + e["amount"]
    roster = ", ".join(f"{esc(names.get(u, 'Player'))} **{fmt_num(a)}**"
                       for u, a in sorted(per.items(), key=lambda kv: -kv[1]))
    lines.append(f"🎟️ **Tickets by player:** {roster or 'none'}\n🎟️ **Total tickets from players: {fmt_num(total)}**")
    lines.append(f"🦆🔥 *{steve()}*")
    text = "\n".join(lines)
    return text if len(text) <= 1900 else text[:1890] + "…"


def digit_cost_for_prize(prize: int) -> int:
    """Each of the 10 digits is worth a tenth of the prize (prize 500 -> 50 tickets a digit)."""
    return max(1, math.ceil(prize / 10))


def satellite_cost(digit_cost: int, open_count: int | None = None) -> int:
    """
    One satellite spot's price: the cost of the digits it is for (the 3 that are
    still open when it starts), split evenly across the 10 spots. Whole tickets
    only, so it rounds up (3 digits at 10 tickets = 30 tickets / 10 spots = 3).
    """
    k = (10 - SAT_THRESHOLD) if open_count is None else open_count
    return math.ceil(k * digit_cost / SAT_SPOTS)


def new_board(guild_id, channel_id, host_id, host_name, draw: str, draw_date: date, prize: str, lock_minutes: int,
              digit_cost: int = 1, sat_cost: int | None = None, code: str | None = None,
              image_url: str | None = None, notes: str | None = None, host_mention: str | None = None) -> dict:
    draw_at = datetime.combine(draw_date, DRAW_TIMES[draw], tzinfo=TZ).astimezone(timezone.utc)
    lock_at = draw_at - timedelta(minutes=lock_minutes)
    return {
        "id": secrets.token_hex(4),
        "mode": "fireball",              # a later "multi" mode will use this
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "host_id": str(host_id),
        "host_name": host_name,
        "draw": draw,
        "draw_date": draw_date.isoformat(),
        "draw_at": draw_at.isoformat(),
        "lock_at": lock_at.isoformat(),
        "prize": prize,
        "code": code,
        "image_url": image_url,
        "notes": notes,
        "notes_message_id": None,
        "notes_author": host_name,
        "digit_cost": digit_cost,        # set by the host when the board starts
        "sat_cost": satellite_cost(digit_cost) if sat_cost is None else sat_cost,
        "status": "open",                # open > satellite > animating > locked > settled | cancelled
        "digits": {str(d): None for d in range(10)},
        "sat_at": None,
        "sat": {"spots": [None] * SAT_SPOTS, "reserved": [], "prefs": {}, "outcome": None, "msg_id": None},
        "ledger": [],
        "board_msg_id": None,
        "result": None,
        "result_msg_id": None,
        "next_fetch_at": None,
        "gave_up": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def claimed_digits(board: dict) -> list[int]:
    return [int(d) for d, c in board["digits"].items() if c]


def open_digits(board: dict) -> list[int]:
    return [int(d) for d, c in board["digits"].items() if not c]


def next_draw(now: datetime | None = None, lock_minutes: int = 10) -> tuple[str, date]:
    """The next draw whose board is still open for claims."""
    now = (now or datetime.now(timezone.utc)).astimezone(TZ)
    for offset in (0, 1):
        day = now.date() + timedelta(days=offset)
        for name in ("midday", "evening"):
            draw_at = datetime.combine(day, DRAW_TIMES[name], tzinfo=TZ)
            if now < draw_at - timedelta(minutes=lock_minutes):
                return name, day
    raise RuntimeError("unreachable")


def claim_digits(data: dict, board: dict, user_id: str, name: str, requested: list[int]) -> dict:
    """
    Claim digits for a user. Bulk claims (Odds/Evens) are clamped so the board
    stops at exactly SAT_THRESHOLD claimed digits, same idea as the duck race
    rounding a call down to what is left.

    Returns {"ok": bool, "msg": str, "claimed": [...], "opened_sat": bool}.
    """
    if board["status"] != "open":
        return {"ok": False, "msg": "This board isn't taking digit claims right now.", "claimed": [], "opened_sat": False}
    avail = [d for d in requested if not board["digits"][str(d)]]
    if not avail:
        return {"ok": False, "msg": "Those digits are already taken.", "claimed": [], "opened_sat": False}
    pending = bool(board.get("sat_at"))      # grace window: others may still claim before the satellite opens
    room = (10 if pending else SAT_THRESHOLD) - len(claimed_digits(board))
    clamped = False
    if len(avail) > room:
        avail = random.sample(avail, room)
        clamped = True
    cost = board["digit_cost"] * len(avail)
    charge(board, user_id, cost, "digit", ",".join(str(d) for d in sorted(avail)))
    for d in avail:
        board["digits"][str(d)] = {"user_id": str(user_id), "name": name, "via": "claim"}
    opened = False
    if not pending and len(claimed_digits(board)) >= SAT_THRESHOLD:
        board["sat_at"] = (datetime.now(timezone.utc) + timedelta(seconds=SAT_DELAY)).isoformat()
        opened = True      # means: the grace window just started
    shown = ", ".join(str(d) for d in sorted(avail))
    msg = f"You claimed digit **{shown}** for **{cost}** ticket(s). You're in for **{owed(board, user_id)}** on this game."
    if clamped:
        msg += " (Rounded down to what was left before the satellite.)"
    return {"ok": True, "msg": msg, "claimed": sorted(avail), "opened_sat": opened}


def open_satellite(board: dict) -> str | None:
    """
    End of the grace window. Whatever digits are still open become the satellite prize:
    normally 3, fewer if somebody grabbed one in the meantime. Returns "satellite",
    "full" (all ten went, so no satellite) or None (nothing to do).
    """
    if board["status"] != "open" or not board.get("sat_at"):
        return None
    board["sat_at"] = None
    left = open_digits(board)
    if not left:
        board["status"] = "locked"
        return "full"
    board["status"] = "satellite"
    board["sat"]["reserved"] = left
    board["sat_cost"] = satellite_cost(board["digit_cost"], len(left))
    return "satellite"


def start_animation(board: dict) -> bool:
    """Preference window is over: decide the outcome (using everyone's preferences) and move to the show."""
    if board["status"] != "satellite" or not all(board["sat"]["spots"]) or board["sat"].get("outcome"):
        return False
    board["sat"]["outcome"] = compute_outcome(board)
    board["status"] = "animating"
    return True


def claim_spot(data: dict, board: dict, user_id: str, name: str, spot: int) -> dict:
    """Claim a satellite spot (0-9). Returns {"ok", "msg", "filled"}."""
    if board["status"] != "satellite":
        return {"ok": False, "msg": "The satellite isn't open.", "filled": False}
    spots = board["sat"]["spots"]
    if spots[spot]:
        return {"ok": False, "msg": "That spot is already taken.", "filled": False}
    cost = board["sat_cost"]
    charge(board, user_id, cost, "sat", str(spot))
    spots[spot] = {"user_id": str(user_id), "name": name}
    filled = all(spots)
    if filled:      # status stays "satellite" for a short while so players can still set preferences
        board["sat"]["fill_at"] = (datetime.now(timezone.utc) + timedelta(seconds=SAT_PREF_DELAY)).isoformat()
    return {
        "ok": True,
        "msg": f"You're in the satellite ({sum(1 for x in spots if x)}/{SAT_SPOTS}) for **{cost}** ticket(s). You're in for **{owed(board, user_id)}** on this game.",
        "filled": filled,
    }


def spot_claim_line(board: dict, user_id: str, taken: list[int]) -> str:
    """Public line posted in the satellite thread for every claim, like the duck race's spot-call reply."""
    left = sum(1 for s in board["sat"]["spots"] if not s)
    nums = ", ".join(f"#{i + 1}" for i in taken)
    tail = "the satellite is **full**! 🔥" if not left else f"**{left}**/{SAT_SPOTS} spot(s) left."
    return (f"🦆🔥 <@{user_id}> took **{len(taken)}** spot(s): **{nums}** · "
            f"**{board['sat_cost']}** ticket(s) each, in for **{owed(board, user_id)}** — {tail}")


def compute_outcome(board: dict, rng: random.Random | None = None) -> dict:
    """
    Decide everything up front, fully at random, and store it. The shuffle
    rounds and the elimination order are only a show; the survivors are what
    count. The surviving ducks are then handled in a random order and each one
    gets the best-ranked open digit from their owner's preferences (or a random
    one if their picks are gone or they set none). Saving this first also means
    a restart in the middle of the animation can still finish the game.
    """
    rng = rng or random.SystemRandom()
    reserved = list(board["sat"]["reserved"])
    spots = board["sat"].get("spots") or []
    prefs = board["sat"].get("prefs") or {}
    n = SAT_SPOTS
    k = len(reserved)
    rounds = [rng.sample(range(n), n) for _ in range(rng.randint(MIN_ROUNDS, MAX_ROUNDS))]
    elim_order = rng.sample(range(n), n)[: n - k]
    winners = [i for i in range(n) if i not in elim_order]

    order = winners[:]
    rng.shuffle(order)             # random priority for who picks first
    remaining = reserved[:]
    assignment: dict[str, int] = {}
    how: dict[str, str] = {}
    for idx in order:
        wanted = prefs.get(spots[idx]["user_id"], []) if spots and spots[idx] else []
        pick = next((d for d in wanted if d in remaining), None)
        if pick is not None:
            how[str(pick)] = f"your #{wanted.index(pick) + 1} choice"
        else:
            pick = rng.choice(remaining)
            how[str(pick)] = "random pick, your choices were taken" if wanted else "random pick, no preference set"
        remaining.remove(pick)
        assignment[str(pick)] = idx
    return {
        "rounds": rounds,
        "elim_order": elim_order,
        "winners": winners,
        "assignment": assignment,
        "how": how,
        "applied": False,
    }


def set_pref(board: dict, user_id: str, digit: int | None) -> dict:
    """Add a digit to the end of a satellite player's ranking (None clears it)."""
    if board["status"] != "satellite":
        return {"ok": False, "msg": "Preferences are locked."}
    if not any(s and s["user_id"] == str(user_id) for s in board["sat"]["spots"]):
        return {"ok": False, "msg": "Take a satellite spot first, then rank the open digits."}
    prefs = board["sat"].setdefault("prefs", {})
    if digit is None:
        prefs.pop(str(user_id), None)
        return {"ok": True, "msg": "Preferences cleared."}
    if digit not in board["sat"]["reserved"]:
        return {"ok": False, "msg": "That digit isn't one of the open ones."}
    lst = prefs.setdefault(str(user_id), [])
    if digit not in lst:
        lst.append(digit)
    return {"ok": True, "msg": "Saved."}


def apply_outcome(board: dict) -> None:
    """Give the reserved digits to the surviving spots' owners."""
    out = board["sat"]["outcome"]
    if out["applied"]:
        return
    for d, spot_idx in out["assignment"].items():
        s = board["sat"]["spots"][spot_idx]
        board["digits"][d] = {"user_id": s["user_id"], "name": s["name"], "via": "satellite"}
    out["applied"] = True
    board["status"] = "locked"


def request_sip(board: dict, user_id: str, name: str, channel_id: str) -> dict | None:
    """
    A player says they've sent the host their tickets. Returns None if they have
    nothing on this board (ignored, like a bystander typing "sip" in a duck race),
    else {"ok", "msg", "amount"}; ok means a fresh request should go to the host.
    """
    amount = owed(board, user_id)
    if amount <= 0:
        return None
    sips = board.setdefault("sips", {})
    s = sips.get(str(user_id))
    if s and s["status"] == "pending":
        return {"ok": False, "msg": "⏳ Already waiting on the host to confirm your sip.", "amount": amount}
    if s and s["status"] == "confirmed" and s["amount"] >= amount:
        return {"ok": False, "msg": "✅ The host already confirmed your sip.", "amount": amount}
    sips[str(user_id)] = {"name": name, "amount": amount, "status": "pending", "channel_id": str(channel_id)}
    return {"ok": True, "msg": "", "amount": amount}


def decide_sip(board: dict, user_id: str, approve: bool) -> dict | None:
    """Host's answer to a pending sip. Returns the sip record, or None if there's nothing pending."""
    s = board.get("sips", {}).get(str(user_id))
    if not s or s["status"] != "pending":
        return None
    s["status"] = "confirmed" if approve else "denied"
    return s


def sip_reminder_targets(board: dict) -> dict[str, int]:
    """user_id -> tickets still unconfirmed, for everyone who hasn't sipped, was denied, or owes more.
    People already waiting on the host are left out; it's the host's move, not theirs."""
    sips = board.get("sips", {})
    due: dict[str, int] = {}
    for uid in dict.fromkeys(e["user_id"] for e in board["ledger"]):
        amount, s = owed(board, uid), sips.get(uid)
        if s and s["status"] == "pending":
            continue
        short_by = amount - s["amount"] if s and s["status"] == "confirmed" else amount
        if short_by > 0:
            due[uid] = short_by
    return due


def sip_lines(board: dict) -> str:
    """One line per player for the board embed: confirmed / waiting on host / denied / not sipped yet."""
    sips = board.get("sips", {})
    names = ticket_names(board)
    lines = []
    for uid in dict.fromkeys(e["user_id"] for e in board["ledger"]):
        amount, s = owed(board, uid), sips.get(uid)
        name = esc(names.get(uid) or (s or {}).get("name") or "Player")
        if s and s["status"] == "confirmed":
            short_by = amount - s["amount"]
            lines.append(f"✅ {name} **{fmt_num(s['amount'])}**" + (f" · owes **{fmt_num(short_by)}** more" if short_by > 0 else ""))
        elif s and s["status"] == "pending":
            lines.append(f"⏳ {name} **{fmt_num(amount)}** · waiting on host")
        elif s and s["status"] == "denied":
            lines.append(f"❌ {name} **{fmt_num(amount)}** · host denied")
        else:
            lines.append(f"💤 {name} **{fmt_num(amount)}** · not sipped")
    text = "\n".join(lines)
    return text if len(text) <= 1024 else text[:1020] + "…"


def releasable(board: dict) -> list[tuple[str, str]]:
    """(value, label) for every spot the host can release right now: main-board digits while the
    board is open, satellite spots while the satellite is still filling. Digits won in the
    satellite stay put."""
    out = []
    if board["status"] == "open":
        for d in range(10):
            c = board["digits"][str(d)]
            if c and c.get("via") == "claim":
                out.append((f"d{d}", f"Digit {d} · {c['name']}"))
    if board["status"] == "satellite" and not all(board["sat"]["spots"]):
        for i, s in enumerate(board["sat"]["spots"]):
            if s:
                out.append((f"s{i}", f"Satellite spot #{i + 1} · {s['name']}"))
    return out


def release(board: dict, value: str) -> dict | None:
    """
    Undo one claim ("d3" = digit 3, "s4" = satellite spot #5): free the spot and drop what it cost.
    Returns {"user_id", "name", "what"} or None if it can't be released (anymore).
    """
    if value not in {v for v, _ in releasable(board)}:
        return None
    kind, n = value[0], int(value[1:])
    if kind == "d":
        c = board["digits"][str(n)]
        uid = c["user_id"]
        for e in board["ledger"]:
            slots = [x for x in str(e["slot"]).split(",") if x]
            if e["kind"] == "digit" and e["user_id"] == uid and str(n) in slots:
                slots.remove(str(n))
                if slots:     # an Odds/Evens claim: keep the rest of it
                    e["amount"] -= board["digit_cost"]
                    e["slot"] = ",".join(slots)
                else:
                    board["ledger"].remove(e)
                break
        board["digits"][str(n)] = None
        # Dropped back under 7 during the last-call countdown: the satellite isn't due anymore.
        if board.get("sat_at") and len(claimed_digits(board)) < SAT_THRESHOLD:
            board["sat_at"] = None
        return {"user_id": uid, "name": c["name"], "what": f"digit **{n}**"}
    s = board["sat"]["spots"][n]
    uid = s["user_id"]
    for e in board["ledger"]:
        if e["kind"] == "sat" and e["user_id"] == uid and str(e["slot"]) == str(n):
            board["ledger"].remove(e)
            break
    board["sat"]["spots"][n] = None
    if not any(x and x["user_id"] == uid for x in board["sat"]["spots"]):
        board["sat"].get("prefs", {}).pop(uid, None)
    return {"user_id": uid, "name": s["name"], "what": f"satellite spot **#{n + 1}**"}


def force_sip(board: dict, user_id: str) -> int:
    """Host/mod marks a player as paid in full without waiting for them to sip. Returns the amount."""
    amount = owed(board, user_id)
    names = ticket_names(board)
    board.setdefault("sips", {})[str(user_id)] = {
        "name": names.get(str(user_id), "Player"), "amount": amount, "status": "confirmed", "channel_id": None}
    return amount


def lock_board(data: dict, board: dict) -> str:
    """Close claiming (Close button or draw time). Returns a short note for the channel."""
    note = ""
    board["sat_at"] = None
    if board["status"] == "satellite" and all(board["sat"]["spots"]):
        start_animation(board)       # full satellite: skip the rest of the wait, never void a filled one
        return "The satellite was full, so it is going ahead now."
    if board["status"] == "satellite":
        spots_taken = sum(1 for s in board["sat"]["spots"] if s)
        returned = void_charges(board, ("sat",))
        board["sat"]["spots"] = [None] * SAT_SPOTS
        board["sat"]["reserved"] = []
        board["sat"]["prefs"] = {}
        note = (
            f"The satellite didn't fill ({spots_taken}/{SAT_SPOTS}), so it was cancelled and "
            f"those **{returned}** ticket(s) of satellite claims are void. Those digits stay unclaimed."
        )
    board["status"] = "locked"
    return note


def multi_spots(board: dict, user_id: str) -> bool:
    return sum(1 for x in board["sat"]["spots"] if x and x["user_id"] == str(user_id)) > 1


def spot_labels(spots: list[dict]) -> list[str]:
    """Display names for ducks; a player with several spots gets (#spot number) on each so you can tell them apart."""
    names = [s["name"] for s in spots]
    labels = []
    for n, nm in enumerate(names, start=1):
        labels.append(f"{nm} (#{n})" if names.count(nm) > 1 else nm)
    return labels


# --- result parsing ---------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_RESULT_RE = re.compile(
    r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),\s*(\d{4})\s+(midday|evening)\s+(\d)\s*(\d)\s*(\d)\s*(\d)",
    re.IGNORECASE,
)


def parse_results(page_html: str) -> list[dict]:
    """
    Pull draws out of the Illinois Lottery Pick 3 results page. Each draw reads
    like 'Saturday Oct 17, 2026 evening 6 5 1 7': three Pick 3 balls, then the
    Fireball digit.
    """
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", page_html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmllib.unescape(text)
    text = re.sub(r"\s+", " ", text)
    out = []
    for m in _RESULT_RE.finditer(text):
        mon = _MONTHS.get(m.group(1).lower())
        if not mon:
            continue
        try:
            d = date(int(m.group(3)), mon, int(m.group(2)))
        except ValueError:
            continue
        digits = [int(m.group(i)) for i in (5, 6, 7)]
        out.append({
            "date": d.isoformat(),
            "draw": m.group(4).lower(),
            "balls": digits,
            "fireball": int(m.group(8)),
        })
    return out


async def fetch_results(session: aiohttp.ClientSession) -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    async with session.get(RESULTS_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        return parse_results(await resp.text())


def find_result(results: list[dict], board: dict) -> dict | None:
    for r in results:
        if r["date"] == board["draw_date"] and r["draw"] == board["draw"]:
            return r
    return None


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def esc(text: str) -> str:
    return discord.utils.escape_markdown(text)


def ts(iso: str, style: str = "t") -> str:
    return f"<t:{int(datetime.fromisoformat(iso).timestamp())}:{style}>"


def draw_title(board: dict) -> str:
    d = date.fromisoformat(board["draw_date"])
    return f"{board['draw'].title()} draw · {d.strftime('%a %b %d').replace(' 0', ' ')}"


def board_embed(board: dict, cfg: dict) -> discord.Embed:
    status = board["status"]
    lines = []
    for d in range(10):
        c = board["digits"][str(d)]
        if c:
            tag = " 🦆" if c.get("via") == "satellite" else ""
            lines.append(f"`{d}`  **{esc(c['name'])}**{tag}")
        elif status == "satellite":
            lines.append(f"`{d}`  🔒 *reserved for satellite winners*")
        else:
            lines.append(f"`{d}`  — open")
    colors = {"open": 0xE67E22, "satellite": 0xF1C40F, "animating": 0xE74C3C,
              "locked": 0x7F8C8D, "settled": 0x2ECC71, "cancelled": 0x95A5A6}
    emb = discord.Embed(
        title=f"🔥 Fireball{(' ' + board['code']) if board.get('code') else ''} — {draw_title(board)}",
        description=(f"**Prize:** {esc(str(board['prize']))}  *(winner takes all)* · "
                     f"each digit is worth **{board['digit_cost']}**"
                     f"{' · **90% BB**' if board.get('bb') else ''}\n\n" + "\n".join(lines)),
        color=colors.get(status, 0xE67E22),
    )
    if status == "open" and board.get("sat_at"):
        state_text_override = "7 digits are claimed! The satellite opens in a few seconds. Grab a digit now if you want one."
    else:
        state_text_override = None
    state_text = {
        "open": f"Claims close {ts(board['lock_at'], 'R')} · draw at {ts(board['draw_at'], 't')}",
        "satellite": f"The satellite is open for the last {len(board['sat']['reserved'])} digit(s).",
        "animating": "Satellite is full. The shuffle and the fire are underway...",
        "locked": f"Claims are closed. Draw at {ts(board['draw_at'], 't')}. Result posts automatically.",
        "settled": "Settled. See the result below.",
        "cancelled": "Cancelled. All tickets were returned.",
    }
    emb.add_field(name="Status", value=state_text_override or state_text.get(status, status), inline=False)
    if board["ledger"] and status != "cancelled":
        emb.add_field(name="Sips (type `sip` once you've sent the host your tickets)", value=sip_lines(board), inline=False)
    emb.set_footer(
        text=f"Digit: {board['digit_cost']} ticket(s) · Satellite spot: {board['sat_cost']} (the open digits split 10 ways) · Host: {board['host_name']}"
    )
    return emb


def sat_embed(board: dict, cfg: dict) -> discord.Embed:
    spots = board["sat"]["spots"]
    taken = sum(1 for s in spots if s)
    reserved = ", ".join(str(d) for d in board["sat"]["reserved"]) or "none"
    roster = "\n".join(
        f"**{n}\\)** " + (f"<@{sp['user_id']}>" if sp else "")
        for n, sp in enumerate(spots, start=1))
    prefs = board["sat"].get("prefs") or {}
    pref_text = ""
    if prefs:
        pref_text = "\n\n**Spot preferences:**\n" + "\n".join(
            f"<@{uid}> " + ",".join(str(d) for d in lst) for uid, lst in prefs.items() if lst)
    emb = discord.Embed(
        title="🦆🔥 Fireball Satellite",
        description=(
            f"**{taken}/{SAT_SPOTS}** spots filled · **{board['sat_cost']}** ticket(s) each.\n"
            + (f"🔔 **The satellite is full!** The shuffle starts {ts(board['sat']['fill_at'], 'R')}. "
               f"Set your ⭐ Preferences now!\n" if taken == SAT_SPOTS and board['sat'].get('fill_at') and board["status"] == "satellite" else "") +
            f"Tap **X1** to take a spot. When it fills, the ducks are lit on fire and the **last {len(board['sat']['reserved'])} still burning** "
            f"each win one of the open digits: **{reserved}**.\n"
            f"Tap **⭐ Preferences** to rank those digits. Winners get their best available pick.\n\n"
            f"{roster}" + pref_text
        ),
        color=0xF1C40F if board["status"] == "satellite" else 0x7F8C8D,
    )
    return emb


class BoardButton(discord.ui.DynamicItem[discord.ui.Button], template=r"fb:(?P<board>[0-9a-f]+):(?P<action>d\d|yolo|odds|evens|close|sip|remind|host|admin)"):
    def __init__(self, board_id: str, action: str, label: str = "?", style=discord.ButtonStyle.secondary,
                 disabled: bool = False, row: int | None = None):
        super().__init__(discord.ui.Button(
            label=label, style=style, custom_id=f"fb:{board_id}:{action}", disabled=disabled, row=row))
        self.board_id = board_id
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["board"], match["action"])

    async def callback(self, interaction: discord.Interaction):
        cog = _active_cog
        if cog is None:
            await interaction.response.send_message("Fireball isn't loaded right now.", ephemeral=True)
            return
        await cog.on_board_click(interaction, self.board_id, self.action)


class SatButton(discord.ui.DynamicItem[discord.ui.Button], template=r"fs:(?P<board>[0-9a-f]+):(?P<action>s\d|yolo|pref|x1)"):
    def __init__(self, board_id: str, action: str, label: str = "?", style=discord.ButtonStyle.secondary,
                 disabled: bool = False, row: int | None = None):
        super().__init__(discord.ui.Button(
            label=label, style=style, custom_id=f"fs:{board_id}:{action}", disabled=disabled, row=row))
        self.board_id = board_id
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["board"], match["action"])

    async def callback(self, interaction: discord.Interaction):
        cog = _active_cog
        if cog is None:
            await interaction.response.send_message("Fireball isn't loaded right now.", ephemeral=True)
            return
        await cog.on_sat_click(interaction, self.board_id, self.action)


class SipButton(discord.ui.DynamicItem[discord.ui.Button], template=r"fp:(?P<board>[0-9a-f]+):(?P<user>\d+):(?P<action>ok|no)"):
    """Confirm / Deny on a sip request in the host's DMs. Survives restarts like the board buttons."""

    def __init__(self, board_id: str, user_id: str, action: str):
        ok = action == "ok"
        super().__init__(discord.ui.Button(
            label="Confirm" if ok else "Deny", emoji="✅" if ok else "❌",
            style=discord.ButtonStyle.success if ok else discord.ButtonStyle.danger,
            custom_id=f"fp:{board_id}:{user_id}:{action}"))
        self.board_id, self.user_id, self.action = board_id, user_id, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["board"], match["user"], match["action"])

    async def callback(self, interaction: discord.Interaction):
        cog = _active_cog
        if cog is None:
            await interaction.response.send_message("Fireball isn't loaded right now.", ephemeral=True)
            return
        await cog.on_sip_decision(interaction, self.board_id, self.user_id, self.action == "ok")


def sip_view(board_id: str, user_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(SipButton(board_id, user_id, "ok"))
    view.add_item(SipButton(board_id, user_id, "no"))
    return view


NOTES_MAX_LENGTH = 1800


def notes_text(board: dict, fallback_author: str) -> str:
    return f"📋 **Host notes ({esc(board.get('notes_author') or fallback_author)}):** {board.get('notes') or ''}"


class NotesButton(discord.ui.DynamicItem[discord.ui.Button], template=r"fn:(?P<board>[0-9a-f]+)"):
    """✏️ Edit notes under the host notes, like the duck race one. Survives restarts."""

    def __init__(self, board_id: str):
        super().__init__(discord.ui.Button(label="Edit notes", emoji="✏️", style=discord.ButtonStyle.secondary,
                                           custom_id=f"fn:{board_id}"))
        self.board_id = board_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["board"])

    async def callback(self, interaction: discord.Interaction):
        cog = _active_cog
        if cog is None:
            await interaction.response.send_message("Fireball isn't loaded right now.", ephemeral=True)
            return
        await cog.open_notes_editor(interaction, self.board_id)


def notes_view(board_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(NotesButton(board_id))
    return view


class NotesModal(discord.ui.Modal, title="Edit host notes"):
    def __init__(self, cog, board_id: str, current: str):
        super().__init__()
        self.cog, self.board_id = cog, board_id
        self.notes_input = discord.ui.TextInput(label="Notes", style=discord.TextStyle.paragraph,
                                                default=current[:NOTES_MAX_LENGTH], max_length=NOTES_MAX_LENGTH)
        self.add_item(self.notes_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.set_notes(interaction, self.board_id, self.notes_input.value)


def thread_name(tag: str, code: str | None) -> str:
    return f"[{tag}] - {code}"[:100] if code else f"[{tag}]"


def short(name: str, n: int = 9) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


_active_cog: "FireballCog | None" = None  # set when the cog loads; the buttons call back into it


def board_view(board: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    live = board["status"] == "open"
    for d in range(10):
        c = board["digits"][str(d)]
        if c:
            btn = BoardButton(board["id"], f"d{d}", f"{d} · {short(c['name'])}",
                              discord.ButtonStyle.secondary, True, row=d // 5)
        else:
            btn = BoardButton(board["id"], f"d{d}", str(d), discord.ButtonStyle.primary, not live, row=d // 5)
        view.add_item(btn)
    view.add_item(BoardButton(board["id"], "yolo", "🎲 YOLO", discord.ButtonStyle.success, not live, row=2))
    view.add_item(BoardButton(board["id"], "odds", "Odds", discord.ButtonStyle.secondary, not live, row=2))
    view.add_item(BoardButton(board["id"], "evens", "Evens", discord.ButtonStyle.secondary, not live, row=2))
    no_sips = board["status"] == "cancelled" or not board["ledger"]
    view.add_item(BoardButton(board["id"], "sip", "💸 Sip", discord.ButtonStyle.success, no_sips, row=3))
    done = board["status"] == "cancelled"
    view.add_item(BoardButton(board["id"], "host", "Host Controls", discord.ButtonStyle.primary, done, row=3))
    view.add_item(BoardButton(board["id"], "admin", "Admin Controls", discord.ButtonStyle.primary, done, row=3))
    return view


class ControlPanel(discord.ui.View):
    """The private panel behind Host Controls / Admin Controls. Every button re-checks who's pressing."""

    def __init__(self, cog, board_id: str, admin: bool):
        super().__init__(timeout=600)
        self.cog, self.board_id = cog, board_id
        buttons = [
            ("🔓 Release Spot", discord.ButtonStyle.primary, self.cog.panel_release),
            ("✅ Force Sip", discord.ButtonStyle.primary, self.cog.panel_force_sip),
            ("🔔 Remind Sips", discord.ButtonStyle.secondary, self.cog._remind_sips),
            ("📋 Expected Sips", discord.ButtonStyle.secondary, self.cog.panel_expected),
            ("🔒 Close Board", discord.ButtonStyle.danger, self.cog.panel_close),
        ]
        if admin:
            buttons.append(("🛑 Cancel Board", discord.ButtonStyle.danger, self.cog.panel_cancel))
        for label, style, handler in buttons:
            btn = discord.ui.Button(label=label, style=style)
            btn.callback = self._wrap(handler)
            self.add_item(btn)

    def _wrap(self, handler):
        async def cb(interaction: discord.Interaction):
            await handler(interaction, self.board_id)
        return cb


class PickSelect(discord.ui.Select):
    """A one-shot private dropdown (which spot to release / whose sip to force)."""

    def __init__(self, options: list[tuple[str, str]], placeholder: str, on_pick):
        super().__init__(placeholder=placeholder,
                         options=[discord.SelectOption(label=lab[:100], value=val) for val, lab in options[:25]])
        self.on_pick = on_pick

    async def callback(self, interaction: discord.Interaction):
        await self.on_pick(interaction, self.values[0])


def pick_view(options: list[tuple[str, str]], placeholder: str, on_pick) -> discord.ui.View:
    view = discord.ui.View(timeout=300)
    view.add_item(PickSelect(options, placeholder, on_pick))
    return view


def sat_view(board: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    live = board["status"] == "satellite" and not all(board["sat"]["spots"])
    view.add_item(SatButton(board["id"], "x1", "X1", discord.ButtonStyle.primary, not live, row=0))
    view.add_item(SatButton(board["id"], "pref", "⭐ Preferences", discord.ButtonStyle.secondary,
                            board["status"] != "satellite", row=0))
    return view


class PrefButton(discord.ui.Button):
    """One button in the private preference picker. Tap digits in order of preference."""

    def __init__(self, cog, board_id: str, digit: int | None, current: list[int]):
        if digit is None:
            super().__init__(label="Reset", style=discord.ButtonStyle.danger)
        else:
            super().__init__(label=str(digit), style=discord.ButtonStyle.primary, disabled=digit in current)
        self.cog, self.board_id, self.digit = cog, board_id, digit

    async def callback(self, interaction: discord.Interaction):
        await self.cog.on_pref_pick(interaction, self.board_id, self.digit)


def pref_view(cog, board_id: str, reserved: list[int], current: list[int]) -> discord.ui.View:
    view = discord.ui.View(timeout=300)
    for d in reserved:
        view.add_item(PrefButton(cog, board_id, d, current))
    view.add_item(PrefButton(cog, board_id, None, current))
    return view


def pref_text(current: list[int], reserved: list[int]) -> str:
    order = " › ".join(str(d) for d in current) if current else "none yet"
    return (f"⭐ **Rank the open digits** ({', '.join(str(d) for d in reserved)}). Tap them in the order you want them.\n"
            f"Your order: **{order}**\n*If you win a spot you get your highest pick that's still free.*")


# --------------------------------------------------------------------------
# The cog
# --------------------------------------------------------------------------

def is_admin(interaction: discord.Interaction) -> bool:
    m = interaction.user
    return isinstance(m, discord.Member) and m.guild_permissions.administrator


def can_host(interaction: discord.Interaction, board: dict) -> bool:
    """The game's host, or a moderator."""
    return str(interaction.user.id) == board["host_id"] or is_mod(interaction)


def is_mod(interaction: discord.Interaction) -> bool:
    """Moderator = Manage Messages (admins have it too), same bar the duck race README uses."""
    m = interaction.user
    return isinstance(m, discord.Member) and m.guild_permissions.manage_messages


@app_commands.guild_only()
class FireballCog(commands.GroupCog, name="fireball", description="Illinois Pick 3 Fireball digit game"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = Store(DATA_PATH)
        self.edit_locks: dict[str, asyncio.Lock] = {}
        self.tasks_running: dict[str, asyncio.Task] = {}
        self.session: aiohttp.ClientSession | None = None
        super().__init__()

    # ---- lifecycle -------------------------------------------------------

    async def cog_load(self):
        global _active_cog
        _active_cog = self
        self.bot.add_dynamic_items(BoardButton, SatButton, SipButton, NotesButton)
        self.session = aiohttp.ClientSession()
        self.tick.start()

    async def cog_unload(self):
        global _active_cog
        _active_cog = None
        self.tick.cancel()
        if self.session:
            await self.session.close()

    @tasks.loop(seconds=30)
    async def tick(self):
        try:
            await self._tick_once()
        except Exception:
            log.exception("Fireball tick failed")

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()
        # Finish any satellite that was mid-animation when the bot last stopped.
        async with self.store.lock:
            stuck = [b["id"] for b in self.store.data["boards"].values() if b["status"] == "animating"]
        for bid in stuck:
            self._start_finale(bid, animate=False)

    async def _tick_once(self):
        now = datetime.now(timezone.utc)
        async with self.store.lock:
            boards = list(self.store.data["boards"].values())
        for b in boards:
            bid = b["id"]
            if b["status"] in ("open", "satellite") and now >= datetime.fromisoformat(b["lock_at"]):
                await self._auto_lock(bid)
            elif b["status"] == "open" and b.get("sat_at") and now >= datetime.fromisoformat(b["sat_at"]):
                await self._open_sat_flow(bid)
            elif (b["status"] == "satellite" and b["sat"].get("fill_at") and all(b["sat"]["spots"])
                  and now >= datetime.fromisoformat(b["sat"]["fill_at"])):
                await self._fill_countdown(bid, wait=0)
            elif b["status"] == "locked" and not b.get("result"):
                await self._maybe_fetch_result(bid, now)

    # ---- helpers ---------------------------------------------------------

    def _lock_for(self, board_id: str) -> asyncio.Lock:
        return self.edit_locks.setdefault(board_id, asyncio.Lock())

    async def _chan_by_id(self, cid):
        if not cid:
            return None
        ch = self.bot.get_channel(int(cid))
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(int(cid))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return ch

    async def _channel(self, board: dict):
        return await self._chan_by_id(board["channel_id"])

    async def _sat_channel(self, board: dict):
        """The satellite's own thread; boards made before that existed keep it in the main thread."""
        ch = await self._chan_by_id(board["sat"].get("thread_id"))
        return ch if ch is not None else await self._channel(board)

    async def _hook_for(self, ch):
        """A 'Pyro Steve' webhook on the channel (made on first use). None if we can't (no permission)."""
        parent = ch.parent if isinstance(ch, discord.Thread) else ch
        pid = getattr(parent, "id", None)
        if pid is None or not hasattr(parent, "webhooks"):
            return None
        cache = self.__dict__.setdefault("_hooks", {})
        if pid in cache:
            return cache[pid] or None
        hook = None
        try:
            for h in await parent.webhooks():
                if h.name == PERSONA_NAME and h.user is not None and h.user.id == self.bot.user.id:
                    hook = h
                    break
            if hook is None:
                avatar = PERSONA_AVATAR.read_bytes() if PERSONA_AVATAR.exists() else None
                hook = await parent.create_webhook(name=PERSONA_NAME, avatar=avatar)
        except Exception as exc:
            log.warning("Can't create the %s webhook in channel %s (%r); posting as the bot instead", PERSONA_NAME, pid, exc)
            self.__dict__.setdefault("_hook_errors", {})[pid] = repr(exc)[:150]
            hook = None
        cache[pid] = hook or False
        return hook

    async def _send(self, ch, content: str | None = None, **kw):
        """Post as Pyro Steve when possible (anything without buttons), otherwise as the bot."""
        if "view" not in kw and USE_WEBHOOK and not getattr(self.bot, "is_pyro_steve", False):
            try:
                hook = await self._hook_for(ch)
                if hook is None:
                    parent = ch.parent if isinstance(ch, discord.Thread) else ch
                    pid = getattr(parent, "id", None)
                    errs = self.__dict__.get("_hook_errors", {})
                    warned = self.__dict__.setdefault("_hook_warned", set())
                    if pid in errs and pid not in warned:
                        warned.add(pid)
                        try:
                            await ch.send(f"⚠️ I can't post as **{PERSONA_NAME}** here yet. Give **Lord 402** the "
                                          f"**Manage Webhooks** permission in this channel (error: `{errs[pid]}`).")
                        except discord.HTTPException:
                            pass
                if hook is not None:
                    extra = {"thread": ch} if isinstance(ch, discord.Thread) else {}
                    return await hook.send(content, wait=True, **extra, **kw)
            except (discord.HTTPException, ValueError, TypeError):
                log.warning("Pyro Steve webhook post failed; falling back to the bot", exc_info=True)
        return await ch.send(content, **kw)

    async def _sat_say(self, board: dict, content: str | None = None, **kw):
        ch = await self._sat_channel(board)
        if ch is None:
            return None
        try:
            return await self._send(ch, content, **kw)
        except discord.HTTPException:
            log.exception("Could not post in satellite thread for board %s", board["id"])
            return None

    async def _tag_sat(self, board: dict, tag: str):
        tid = board["sat"].get("thread_id")
        if not tid or not board.get("code"):
            return
        ch = await self._chan_by_id(tid)
        if isinstance(ch, discord.Thread):
            try:
                await ch.edit(name=thread_name(tag, "SAT " + board["code"]))
            except discord.HTTPException:
                log.warning("Couldn't rename Fireball satellite thread %s", board["id"])

    async def _retire_sat(self, board: dict):
        """If a satellite thread was left unfilled when the board closed/cancelled, mark it dead."""
        if board["sat"].get("thread_id") and not board["sat"].get("outcome"):
            await self._sat_say(board, "🛑 The satellite didn't fill before the board closed. It's cancelled and every spot is void.")
            await self._tag_sat(board, "CANCELLED")

    async def _refresh(self, board_id: str):
        """Re-draw the main board message (and the satellite one if there is one) from current state."""
        async with self._lock_for(board_id):
            async with self.store.lock:
                board = self.store.data["boards"].get(board_id)
                if not board:
                    return
                board = json.loads(json.dumps(board))
                cfg = get_config(self.store.data, board["guild_id"])
            ch = await self._channel(board)
            if ch is None:
                return
            try:
                if board["board_msg_id"]:
                    await ch.get_partial_message(int(board["board_msg_id"])).edit(
                        embed=board_embed(board, cfg), view=board_view(board))
            except discord.HTTPException:
                log.exception("Could not edit Fireball board %s", board_id)
            try:
                if board["sat"]["msg_id"]:
                    sch = await self._sat_channel(board)
                    if sch is not None:
                        await sch.get_partial_message(int(board["sat"]["msg_id"])).edit(
                            embed=sat_embed(board, cfg), view=sat_view(board))
            except discord.HTTPException:
                log.exception("Could not edit Fireball satellite for board %s", board_id)

    async def _tag(self, board: dict, tag: str):
        """Rename the game's thread to [OPEN]/[CLOSED]/[DONE]/[CANCELLED] - code, like the duck race does."""
        if not board.get("code"):
            return
        ch = await self._channel(board)
        if isinstance(ch, discord.Thread):
            try:
                await ch.edit(name=thread_name(tag, board["code"]))
            except discord.HTTPException:
                log.warning("Couldn't rename Fireball thread %s", board["id"])

    async def _say(self, board: dict, content: str | None = None, **kw):
        ch = await self._channel(board)
        if ch is None:
            return None
        try:
            return await self._send(ch, content, **kw)
        except discord.HTTPException:
            log.exception("Could not post in channel for board %s", board["id"])
            return None

    # ---- button handlers -------------------------------------------------

    async def on_board_click(self, interaction: discord.Interaction, board_id: str, action: str):
        if action == "sip":
            await self._board_sip(interaction, board_id)
            return
        if action == "remind":
            await self._remind_sips(interaction, board_id)
            return
        if action in ("host", "admin"):
            await self._open_panel(interaction, board_id, admin=action == "admin")
            return
        user = interaction.user
        opened_sat = False
        reply = ""
        closed_note = None
        async with self.store.lock:
            board = self.store.data["boards"].get(board_id)
            if not board or board["status"] == "cancelled":
                await interaction.response.send_message("This board is no longer active.", ephemeral=True)
                return

            if action == "close":
                if not can_host(interaction, board):
                    await interaction.response.send_message("Only the host or a mod can close the board.", ephemeral=True)
                    return
                if board["status"] not in ("open", "satellite"):
                    await interaction.response.send_message("The board can't be closed right now.", ephemeral=True)
                    return
                closed_note = lock_board(self.store.data, board)
                self.store.save()
                reply = "Board closed."
            else:
                if action.startswith("d"):
                    requested = [int(action[1:])]
                elif action == "yolo":
                    requested = [random.choice(open_digits(board))] if open_digits(board) else []
                elif action == "odds":
                    requested = [d for d in open_digits(board) if d % 2 == 1]
                else:  # evens
                    requested = [d for d in open_digits(board) if d % 2 == 0]
                result = claim_digits(self.store.data, board, str(user.id), user.display_name, requested)
                if not result["ok"]:
                    await interaction.response.send_message(result["msg"], ephemeral=True)
                    return
                self.store.save()
                opened_sat = result["opened_sat"]
                reply = result["msg"]
            snapshot = json.loads(json.dumps(board))

        await interaction.response.defer()
        await self._refresh(board_id)
        await interaction.followup.send(reply, ephemeral=True)
        if closed_note is not None:
            await self._say(snapshot, f"🔒 **Fireball board closed** by {esc(user.display_name)}. {closed_note}".strip())
            await self._tag(snapshot, "CLOSED")
            await self._retire_sat(snapshot)
        if opened_sat:     # the 7th digit just went: short grace window, then the satellite opens
            await self._say(snapshot, f"⏳ **7 digits are claimed! Burnin' happy!** The satellite opens in {SAT_DELAY} seconds. "
                                      "Last chance to grab a digit on the main board.")
            asyncio.create_task(self._open_sat_flow(board_id, delay=SAT_DELAY))

    async def on_sat_click(self, interaction: discord.Interaction, board_id: str, action: str):
        user = interaction.user
        filled = False
        async with self.store.lock:
            board = self.store.data["boards"].get(board_id)
            if not board or board["status"] == "cancelled":
                await interaction.response.send_message("This board is no longer active.", ephemeral=True)
                return
            if action == "pref":
                mine = any(s and s["user_id"] == str(user.id) for s in board["sat"]["spots"])
                if board["status"] != "satellite":
                    await interaction.response.send_message("Preferences are locked.", ephemeral=True)
                    return
                if not mine:
                    await interaction.response.send_message(
                        "Take a satellite spot first, then rank the open digits.", ephemeral=True)
                    return
                reserved = list(board["sat"]["reserved"])
                current = list(board["sat"].get("prefs", {}).get(str(user.id), []))
                await interaction.response.send_message(
                    pref_text(current, reserved), view=pref_view(self, board_id, reserved, current), ephemeral=True)
                return
            if action in ("x1", "yolo"):
                free = [i for i, s in enumerate(board["sat"]["spots"]) if not s]
                if not free:
                    await interaction.response.send_message("The satellite is full.", ephemeral=True)
                    return
                spot = free[0] if action == "x1" else random.choice(free)
            else:
                spot = int(action[1:])
            result = claim_spot(self.store.data, board, str(user.id), user.display_name, spot)
            if not result["ok"]:
                await interaction.response.send_message(result["msg"], ephemeral=True)
                return
            self.store.save()
            filled = result["filled"]
            line = spot_claim_line(board, str(user.id), [spot])
            snap = json.loads(json.dumps(board))

        await interaction.response.defer()
        await self._refresh(board_id)
        await self._sat_say(snap, line, allowed_mentions=discord.AllowedMentions.none())
        if filled:
            asyncio.create_task(self._fill_countdown(board_id))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Typed "sip". Satellite spots are button-only (X1), so typed "X2" etc. are ignored."""
        if message.author.bot or not message.guild:
            return
        if message.content.strip().lower() in SIP_WORDS:
            await self._handle_sip(message)

    async def _submit_sip(self, board_id: str, member, channel, jump_url: str) -> str | None:
        """
        Record a sip and send the host a Confirm / Deny request (DM, or the thread if their DMs
        are closed). Returns the private note for the player, or None if they have nothing to sip.
        """
        uid = str(member.id)
        async with self.store.lock:
            board = self.store.data["boards"].get(board_id)
            if not board or board["status"] == "cancelled":
                return None
            res = request_sip(board, uid, member.display_name, str(channel.id))
            if res is None:
                return None
            if res["ok"]:
                self.store.save()
            snap = json.loads(json.dumps(board))
        if not res["ok"]:
            return res["msg"]

        request = (f"💸 **{esc(member.display_name)}** (<@{uid}>) says they sipped "
                   f"**{fmt_num(res['amount'])}** ticket(s) for 🔥 Fireball {esc(snap.get('code') or '')} "
                   f"({draw_title(snap)}). Did you get it?\n{jump_url}")
        sent_dm = False
        try:
            host = self.bot.get_user(int(snap["host_id"])) or await self.bot.fetch_user(int(snap["host_id"]))
            await host.send(request, view=sip_view(snap["id"], uid))
            sent_dm = True
        except (discord.HTTPException, ValueError):
            # Host has DMs closed: put the buttons in the thread instead (only the host or an admin can press them).
            try:
                await channel.send(f"<@{snap['host_id']}> {request}", view=sip_view(snap["id"], uid))
            except discord.HTTPException:
                log.exception("Couldn't send sip request for board %s", snap["id"])
        await self._refresh(snap["id"])
        return (f"📨 Your sip for **{fmt_num(res['amount'])}** ticket(s) on Fireball {esc(snap.get('code') or '')} "
                f"was sent to the host to confirm" + ("." if sent_dm else " (in the thread, their DMs are closed)."))

    def _board_for_channel(self, cid: str) -> dict | None:
        found = sorted(
            (b for b in self.store.data["boards"].values()
             if b["status"] != "cancelled" and cid in (b["channel_id"], str(b["sat"].get("thread_id")))),
            key=lambda b: b["created_at"])
        return found[-1] if found else None

    async def _handle_sip(self, message: discord.Message):
        """Typed "sip". No reply in the thread (too much filler): a 📨 reaction, and the note goes by DM."""
        async with self.store.lock:
            board = self._board_for_channel(str(message.channel.id))
        if not board:
            return
        note = await self._submit_sip(board["id"], message.author, message.channel, message.jump_url)
        if note is None:
            return
        try:
            await message.add_reaction("📨" if note.startswith("📨") else "👀")
        except discord.HTTPException:
            pass
        try:
            await message.author.send(note)
        except discord.HTTPException:
            pass

    async def _board_sip(self, interaction: discord.Interaction, board_id: str):
        """💸 Sip button: same as typing sip, but the answer is only visible to the player."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        note = await self._submit_sip(board_id, interaction.user, interaction.channel, interaction.channel.jump_url)
        await interaction.followup.send(note or "You don't have any digits or satellite spots on this board.",
                                        ephemeral=True)

    async def _remind_sips(self, interaction: discord.Interaction, board_id: str):
        """🔔 Remind sips button (host/admin): one message pinging everyone who still needs to sip."""
        async with self.store.lock:
            board = self.store.data["boards"].get(board_id)
            if not board:
                await interaction.response.send_message("This board is no longer active.", ephemeral=True)
                return
            if not can_host(interaction, board):
                await interaction.response.send_message("Only the host or a mod can send sip reminders.", ephemeral=True)
                return
            now = datetime.now(timezone.utc)
            last = board.get("last_remind_at")
            if last and now - datetime.fromisoformat(last) < timedelta(seconds=REMIND_COOLDOWN):
                left = REMIND_COOLDOWN - int((now - datetime.fromisoformat(last)).total_seconds())
                await interaction.response.send_message(f"You just sent one. Try again in {left}s.", ephemeral=True)
                return
            due = sip_reminder_targets(board)
            waiting = sum(1 for s in board.get("sips", {}).values() if s["status"] == "pending")
            if due:
                board["last_remind_at"] = now.isoformat()
                self.store.save()
            snap = json.loads(json.dumps(board))
        pending_note = f" **{waiting}** sip(s) are waiting on you to confirm." if waiting else ""
        if not due:
            await interaction.response.send_message("Nobody needs a reminder." + pending_note, ephemeral=True)
            return
        who = ", ".join(f"<@{uid}> (**{fmt_num(n)}**)" for uid, n in due.items())
        await interaction.response.send_message(f"🔔 Reminder sent to {len(due)} player(s).{pending_note}", ephemeral=True)
        await self._say(snap, f"🔔 **Sip reminder** for Fireball {esc(snap.get('code') or '')}: {who}\n"
                              f"Send the host your tickets, then tap **💸 Sip** on the board (or type `sip`).")

    async def _panel_board(self, interaction: discord.Interaction, board_id: str) -> dict | None:
        """Snapshot of the board if the presser may use host controls on it; otherwise answers them and returns None."""
        async with self.store.lock:
            board = self.store.data["boards"].get(board_id)
            snap = json.loads(json.dumps(board)) if board else None
        if not snap:
            await interaction.response.send_message("This board is no longer active.", ephemeral=True)
            return None
        if not can_host(interaction, snap):
            await interaction.response.send_message("Only the host or a mod can use these controls.", ephemeral=True)
            return None
        return snap

    async def _open_panel(self, interaction: discord.Interaction, board_id: str, admin: bool):
        if admin:
            if not is_mod(interaction):
                await interaction.response.send_message("Admin Controls are for mods only.", ephemeral=True)
                return
            snap = await self._panel_board(interaction, board_id)
        else:
            snap = await self._panel_board(interaction, board_id)
        if not snap:
            return
        title = "🛡️ **Admin Controls**" if admin else "🛠️ **Host Controls**"
        await interaction.response.send_message(
            f"{title} for Fireball {esc(snap.get('code') or '')}", view=ControlPanel(self, board_id, admin), ephemeral=True)

    async def panel_release(self, interaction: discord.Interaction, board_id: str):
        snap = await self._panel_board(interaction, board_id)
        if not snap:
            return
        options = releasable(snap)
        if not options:
            why = ("The satellite is full, so its spots are locked in." if snap["status"] == "satellite"
                   else "Nothing can be released right now (digits only while the board is open, "
                        "satellite spots only while it's filling).")
            await interaction.response.send_message(why, ephemeral=True)
            return

        async def picked(inter: discord.Interaction, value: str):
            async with self.store.lock:
                board = self.store.data["boards"].get(board_id)
                if not board or not can_host(inter, board):
                    await inter.response.send_message("You can't do that.", ephemeral=True)
                    return
                gone = release(board, value)
                if gone is None:
                    await inter.response.edit_message(content="That spot can't be released anymore.", view=None)
                    return
                self.store.save()
                s = json.loads(json.dumps(board))
            await inter.response.edit_message(
                content=f"🔓 Released {gone['what']} from **{esc(gone['name'])}**. Their tickets for it are void.", view=None)
            note = (f"🔓 {esc(inter.user.display_name)} released {gone['what']} from <@{gone['user_id']}>. "
                    f"It's open again.")
            if value.startswith("s"):
                await self._sat_say(s, note, allowed_mentions=discord.AllowedMentions.none())
            else:
                await self._say(s, note, allowed_mentions=discord.AllowedMentions.none())
            await self._refresh(board_id)

        await interaction.response.send_message(
            "Which spot do you want to release?", view=pick_view(options, "Pick a spot", picked), ephemeral=True)

    async def panel_force_sip(self, interaction: discord.Interaction, board_id: str):
        snap = await self._panel_board(interaction, board_id)
        if not snap:
            return
        names = ticket_names(snap)
        sips = snap.get("sips", {})
        options = []
        for uid in dict.fromkeys(e["user_id"] for e in snap["ledger"]):
            s, amount = sips.get(uid), owed(snap, uid)
            if s and s["status"] == "confirmed" and s["amount"] >= amount:
                continue
            state = {"pending": "waiting on you", "denied": "denied"}.get((s or {}).get("status"), "not sipped")
            options.append((uid, f"{names.get(uid, 'Player')} · {fmt_num(amount)} · {state}"))
        if not options:
            await interaction.response.send_message("Everyone's sip is already confirmed.", ephemeral=True)
            return

        async def picked(inter: discord.Interaction, uid: str):
            async with self.store.lock:
                board = self.store.data["boards"].get(board_id)
                if not board or not can_host(inter, board):
                    await inter.response.send_message("You can't do that.", ephemeral=True)
                    return
                amount = force_sip(board, uid)
                self.store.save()
                code = board.get("code") or ""
            await inter.response.edit_message(
                content=f"✅ Marked <@{uid}> as sipped (**{fmt_num(amount)}** ticket(s)).", view=None)
            try:
                player = self.bot.get_user(int(uid)) or await self.bot.fetch_user(int(uid))
                await player.send(f"✅ The host marked your sip as confirmed (**{fmt_num(amount)}** ticket(s)) "
                                  f"on Fireball {esc(code)}.")
            except (discord.HTTPException, ValueError):
                pass
            await self._refresh(board_id)

        await interaction.response.send_message(
            "Whose sip do you want to confirm?", view=pick_view(options, "Pick a player", picked), ephemeral=True)

    async def panel_expected(self, interaction: discord.Interaction, board_id: str):
        snap = await self._panel_board(interaction, board_id)
        if not snap:
            return
        if not snap["ledger"]:
            await interaction.response.send_message("Nobody has claimed anything yet.", ephemeral=True)
            return
        confirmed = sum(min(s["amount"], owed(snap, u)) for u, s in snap.get("sips", {}).items()
                        if s["status"] == "confirmed")
        total = ticket_total(snap)
        await interaction.response.send_message(
            f"📋 **Expected sips:** **{fmt_num(total)}** ticket(s) · confirmed **{fmt_num(confirmed)}** · "
            f"still out **{fmt_num(total - confirmed)}**\n{sip_lines(snap)}", ephemeral=True)

    async def panel_close(self, interaction: discord.Interaction, board_id: str):
        await self.on_board_click(interaction, board_id, "close")

    async def panel_cancel(self, interaction: discord.Interaction, board_id: str):
        await self.cancel(interaction)

    async def on_sip_decision(self, interaction: discord.Interaction, board_id: str, user_id: str, approve: bool):
        async with self.store.lock:
            board = self.store.data["boards"].get(board_id)
            if not board:
                await interaction.response.send_message("That Fireball game is gone.", ephemeral=True)
                return
            if str(interaction.user.id) != board["host_id"] and not is_admin(interaction):
                await interaction.response.send_message("Only the host or an admin can confirm sips.", ephemeral=True)
                return
            s = decide_sip(board, user_id, approve)
            if s is None:
                await interaction.response.send_message("That sip was already handled.", ephemeral=True)
                return
            self.store.save()
            snap = json.loads(json.dumps(board))
        verdict = "✅ **Confirmed**" if approve else "❌ **Denied**"
        await interaction.response.edit_message(content=f"{interaction.message.content}\n\n{verdict}", view=None)
        game = f"Fireball {esc(snap.get('code') or '')}"
        if approve:
            note = f"✅ The host confirmed your sip (**{fmt_num(s['amount'])}** ticket(s)) on {game}."
        else:
            note = (f"❌ The host denied your sip (**{fmt_num(s['amount'])}** ticket(s)) on {game}. "
                    f"Sort it out with them, then sip again.")
        # Tell the player privately; the board's Sips list shows everyone the tally.
        try:
            player = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
            await player.send(note)
        except (discord.HTTPException, ValueError):
            ch = await self._chan_by_id(s.get("channel_id")) or await self._channel(snap)
            if ch is not None:
                try:
                    await self._send(ch, f"<@{user_id}> {note}")
                except discord.HTTPException:
                    pass
        await self._refresh(board_id)

    async def on_pref_pick(self, interaction: discord.Interaction, board_id: str, digit: int | None):
        async with self.store.lock:
            board = self.store.data["boards"].get(board_id)
            if not board:
                await interaction.response.send_message("This board is no longer active.", ephemeral=True)
                return
            result = set_pref(board, str(interaction.user.id), digit)
            if not result["ok"]:
                await interaction.response.send_message(result["msg"], ephemeral=True)
                return
            self.store.save()
            reserved = list(board["sat"]["reserved"])
            current = list(board["sat"].get("prefs", {}).get(str(interaction.user.id), []))
        await interaction.response.edit_message(
            content=pref_text(current, reserved), view=pref_view(self, board_id, reserved, current))
        await self._refresh(board_id)

    async def _open_sat_flow(self, board_id: str, delay: float = 0):
        if delay:
            await asyncio.sleep(delay)
        async with self.store.lock:
            board = self.store.data["boards"].get(board_id)
            if not board:
                return
            result = open_satellite(board)
            if result:
                self.store.save()
            snap = json.loads(json.dumps(board))
        if result is None:
            return
        await self._refresh(board_id)
        if result == "full":
            await self._say(snap, "🔒 **All ten digits were claimed during the countdown, so there's no satellite.** "
                                  f"The board is full. Draw at {ts(snap['draw_at'], 't')}.")
            return
        await self._post_satellite(board_id)

    async def _post_satellite(self, board_id: str):
        async with self.store.lock:
            board = self.store.data["boards"].get(board_id)
            if not board:
                return
            snap = json.loads(json.dumps(board))
            cfg = get_config(self.store.data, board["guild_id"])
        main = await self._channel(snap)
        parent = await self._chan_by_id(snap.get("parent_channel_id"))
        thread = None
        if parent is not None and not isinstance(parent, discord.Thread):
            try:
                # Same picture as the main game, re-used from the original announcement.
                file = None
                try:
                    orig = await parent.fetch_message(int(snap["announce_msg_id"]))
                    if orig.attachments:
                        file = await orig.attachments[0].to_file()
                except (discord.HTTPException, KeyError, TypeError, ValueError):
                    file = None
                main_link = main.mention if main is not None else "the main thread"
                text = (
                    f"🦆🔥 **SATELLITE for Fireball {esc(snap.get('code') or '')}** is open! {draw_title(snap)}\n"
                    f"This is the **satellite**, not the main game. The last {len(snap['sat']['reserved'])} "
                    f"(**{', '.join(str(d) for d in snap['sat']['reserved'])}**) are decided here. "
                    f"10 spots at **{snap['sat_cost']}** ticket(s) each; the last {len(snap['sat']['reserved'])} duck(s) still burning win a digit "
                    f"on the main board.\nMain game: {main_link}")
                kw = {"file": file} if file is not None else {}
                ann = await parent.send(text, **kw)
                thread = await ann.create_thread(
                    name=thread_name("OPEN", "SAT " + (snap.get("code") or "")), auto_archive_duration=1440)
                snap["sat"]["announce_msg_id"] = str(ann.id)
            except discord.HTTPException:
                log.exception("Could not create the satellite thread for board %s", board_id)
                thread = None
        if thread is not None:
            await thread.send(
                f"📋 **This is the SATELLITE for Fireball {esc(snap.get('code') or '')}.** "
                f"Winners are moved onto the main board automatically.\n"
                f"Host notes: {snap.get('notes') or '(none)'}")
            msg = await thread.send(embed=sat_embed(snap, cfg), view=sat_view(snap))
            if main is not None:
                try:
                    await main.send(f"🦆🔥 **The satellite is open for the last {len(snap['sat']['reserved'])} digit(s)!** Join it here: {thread.mention}")
                except discord.HTTPException:
                    pass
        else:   # couldn't make a thread: fall back to the old in-place satellite
            msg = await self._say(
                snap, f"🦆🔥 **The satellite is open for the last {len(snap['sat']['reserved'])} digit(s)!**",
                embed=sat_embed(snap, cfg), view=sat_view(snap))
        if msg:
            async with self.store.lock:
                b = self.store.data["boards"].get(board_id)
                if b:
                    b["sat"]["msg_id"] = str(msg.id)
                    if thread is not None:
                        b["sat"]["thread_id"] = str(thread.id)
                        b["sat"]["announce_msg_id"] = snap["sat"].get("announce_msg_id")
                    self.store.save()

    # ---- shuffle + finale ------------------------------------------------

    async def _fill_countdown(self, board_id: str, wait: float | None = None):
        """Satellite is full: give everyone a moment to rank their digits, then start the shuffle."""
        if wait is None:
            async with self.store.lock:
                b = self.store.data["boards"].get(board_id)
                if not b or not b["sat"].get("fill_at"):
                    return
                wait = max(0.0, (datetime.fromisoformat(b["sat"]["fill_at"]) - datetime.now(timezone.utc)).total_seconds())
                snap = json.loads(json.dumps(b))
            await self._sat_say(
                snap, f"🔔 **The satellite is full! Quack chaos!** The shuffle starts in **{int(wait)} seconds**. "
                      "Tap **⭐ Preferences** now to rank the open digits, before I set them on fire.")
        await asyncio.sleep(wait)
        async with self.store.lock:
            b = self.store.data["boards"].get(board_id)
            if not b or not start_animation(b):
                return
            self.store.save()
        await self._refresh(board_id)
        self._start_finale(board_id, animate=True)

    def _start_finale(self, board_id: str, animate: bool):
        if board_id in self.tasks_running and not self.tasks_running[board_id].done():
            return
        self.tasks_running[board_id] = asyncio.create_task(self._finale(board_id, animate))

    async def _finale(self, board_id: str, animate: bool):
        try:
            async with self.store.lock:
                board = self.store.data["boards"].get(board_id)
                if not board or board["status"] != "animating":
                    return
                snap = json.loads(json.dumps(board))
            out = snap["sat"]["outcome"]
            labels = spot_labels(snap["sat"]["spots"])
            n = len(labels)
            ch = await self._sat_channel(snap)

            if animate and ch is not None:
                await self._play_shuffle(ch, labels, out, snap["sat"]["spots"])
                await self._play_fire(ch, labels, out, snap)

            async with self.store.lock:
                board = self.store.data["boards"][board_id]
                apply_outcome(board)
                self.store.save()
                snap = json.loads(json.dumps(board))
            await self._refresh(board_id)
            await self._tag(snap, "CLOSED")
            await self._tag_sat(snap, "DONE")

            out = snap["sat"]["outcome"]
            lines = []
            for d, idx in sorted(out["assignment"].items()):
                s = snap["sat"]["spots"][idx]
                lines.append(
                    f"🎉 Congrats <@{s['user_id']}>! {'Spot **#' + str(idx + 1) + '** won' if multi_spots(snap, s['user_id']) else 'You won'} "
                    f"digit **{d}** on the Fireball board ({out.get('how', {}).get(d, 'assigned')}).")
            sat_ch = await self._sat_channel(snap)
            main_ch = await self._channel(snap)
            await self._sat_say(
                snap,
                "🔥 **The fire has settled. Burnin' happy!** The last ducks standing:\n" + "\n".join(lines) +
                f"\n\nThe board is full. Draw at {ts(snap['draw_at'], 't')}. I'll be back with the numbers. {steve()}",
            )
            if sat_ch is not None and main_ch is not None and sat_ch.id != main_ch.id:
                names = ", ".join(
                    f"**{esc(snap['sat']['spots'][idx]['name'])}**"
                    + (f" spot #{idx + 1}" if multi_spots(snap, snap['sat']['spots'][idx]['user_id']) else "")
                    + f" (digit {d})"
                    for d, idx in sorted(out["assignment"].items()))
                await self._say(
                    snap,
                    f"🔥 **Satellite done! Every day I'm flaming awesome!** {names} moved onto the main board. The board is full. "
                    f"Draw at {ts(snap['draw_at'], 't')}; the result posts automatically.")
        except Exception:
            log.exception("Fireball finale crashed for board %s", board_id)

    async def _play_shuffle(self, ch, labels: list[str], out: dict, spots: list | None = None):
        """Each spin is its own message and stays in the thread, so everyone can read back through them."""
        rounds = out["rounds"]
        total = len(rounds)

        def who(idx: int) -> str:
            sp = spots[idx] if spots and spots[idx] else None
            if not sp:
                return esc(labels[idx])
            multi = sum(1 for x in spots if x and x["user_id"] == sp["user_id"]) > 1
            return f"<@{sp['user_id']}>" + (f" (spot #{idx + 1})" if multi else "")

        none = discord.AllowedMentions.none()
        await self._send(ch, f"🌀 **Spinning the ducks!** {steve()}")
        await asyncio.sleep(SHUFFLE_DELAY)
        for r, perm in enumerate(rounds, start=1):
            if r < total:
                top = "\n".join(f"{i + 1}) {who(idx)}" for i, idx in enumerate(perm[:3]))
                text = f"**Top 3 from spin:**\n{top}\n{total - r} of {total} rounds remaining."
            else:
                full = "\n".join(f"{i + 1}) {who(idx)}" for i, idx in enumerate(perm))
                text = f"**Final spin, full order:**\n{full}\nThat was the last round. Time to light these ducks up! 🔥"
            try:
                await self._send(ch, text, allowed_mentions=none)
            except discord.HTTPException:
                pass
            await asyncio.sleep(SHUFFLE_DELAY)

    async def _play_fire(self, ch, labels: list[str], out: dict, board: dict):
        """The ducks-on-fire finale: an animated GIF of them racing. Falls back to a text version."""
        try:
            from fireanim import render_finale_gif
            gif, seconds = await asyncio.to_thread(
                render_finale_gif, labels, out["elim_order"], out["winners"], board["id"])
            if len(gif) > 7_500_000:
                raise RuntimeError(f"finale GIF too big ({len(gif)} bytes)")
            msg = await self._send(ch, "🔥🔥 **THE DUCKS ARE ON FIRE** 🔥🔥 *Quack chaos!*",
                                   file=discord.File(io.BytesIO(gif), filename="duck-fire.gif"))
            await asyncio.sleep(seconds * FINALE_WAIT_SCALE)
            # GIFs loop forever in Discord, so swap it for the still final frame once it has played.
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(gif))
                im.seek(im.n_frames - 1)
                png = io.BytesIO()
                im.convert("RGB").save(png, format="PNG")
                png.seek(0)
                await msg.edit(attachments=[discord.File(png, filename="duck-fire-final.png")])
            except Exception:
                log.warning("Couldn't swap the finale GIF for a still", exc_info=True)
        except Exception:
            log.warning("Fireball race GIF failed; using the text finale instead", exc_info=True)
            await self._play_fire_text(ch, labels, out, board)

    async def _play_fire_text(self, ch, labels: list[str], out: dict, board: dict):
        state = ["burn"] * len(labels)   # burn | smoke | dead

        def render(note: str) -> str:
            icons = {"burn": "🦆🔥", "smoke": "🦆💨", "dead": "🪦"}
            rows = []
            for i, lab in enumerate(labels):
                if state[i] == "dead":
                    rows.append(f"{icons['dead']} ~~{esc(lab)}~~")
                else:
                    rows.append(f"{icons[state[i]]} **{esc(lab)}**")
            return "🔥🔥 **THE DUCKS ARE ON FIRE** 🔥🔥\n" + "\n".join(rows) + f"\n\n*{note}*"

        msg = await self._send(ch, render("Ten ducks. Three survive. Nobody is safe..."))
        await asyncio.sleep(FIRE_DELAY)
        quips = BURN_QUIPS[:]
        random.shuffle(quips)
        for step, idx in enumerate(out["elim_order"]):
            state[idx] = "smoke"
            quip = quips[step % len(quips)]
            for text in (render(f"{quip} {esc(labels[idx])}'s flame goes out..."),):
                try:
                    await msg.edit(content=text)
                except discord.HTTPException:
                    pass
            await asyncio.sleep(FIRE_DELAY)
            state[idx] = "dead"
            alive = sum(1 for s in state if s == "burn")
            try:
                await msg.edit(content=render(f"{esc(labels[idx])} is dead. {alive} still burning."))
            except discord.HTTPException:
                pass
            await asyncio.sleep(FIRE_DELAY)

    # ---- locking + results -------------------------------------------------

    async def _auto_lock(self, board_id: str):
        async with self.store.lock:
            board = self.store.data["boards"].get(board_id)
            if not board or board["status"] not in ("open", "satellite"):
                return
            note = lock_board(self.store.data, board)
            self.store.save()
            snap = json.loads(json.dumps(board))
        await self._refresh(board_id)
        await self._say(snap, f"🔒 **Fireball claims are closed** for the {snap['draw']} draw. {note}".strip())
        await self._tag(snap, "CLOSED")
        await self._retire_sat(snap)

    async def _maybe_fetch_result(self, board_id: str, now: datetime):
        async with self.store.lock:
            board = self.store.data["boards"].get(board_id)
            if not board or board["status"] != "locked" or board.get("gave_up"):
                return
            draw_at = datetime.fromisoformat(board["draw_at"])
            if now < draw_at + timedelta(minutes=RESULT_FIRST_TRY_MIN):
                return
            nxt = board.get("next_fetch_at")
            if nxt and now < datetime.fromisoformat(nxt):
                return
            board["next_fetch_at"] = (now + timedelta(seconds=RESULT_RETRY_SECONDS)).isoformat()
            self.store.save()
        found = None
        try:
            results = await fetch_results(self.session)
            found = find_result(results, board)
        except Exception:
            log.warning("Fireball result fetch failed", exc_info=True)
        if found:
            await self._settle(board_id, found["balls"], found["fireball"])
            return
        if now > draw_at + timedelta(hours=RESULT_GIVE_UP_HOURS):
            async with self.store.lock:
                b = self.store.data["boards"].get(board_id)
                if b:
                    b["gave_up"] = True
                    self.store.save()
                    snap = json.loads(json.dumps(b))
            await self._say(
                snap,
                f"⚠️ <@{snap['host_id']}> I couldn't read the {snap['draw']} result from illinoislottery.com. "
                f"Enter it yourself with `/fireball result fireball:<digit>`.",
            )

    async def _settle(self, board_id: str, balls: list[int] | None, fireball: int):
        async with self.store.lock:
            board = self.store.data["boards"].get(board_id)
            if not board or board["status"] not in ("locked",):
                return
            board["result"] = {"balls": balls, "fireball": fireball}
            board["status"] = "settled"
            holder = board["digits"][str(fireball)]
            self.store.save()
            snap = json.loads(json.dumps(board))
        pick = "-".join(str(b) for b in balls) if balls else "n/a"
        emb = discord.Embed(title="Illinois Pick 3 Results", color=0x2ECC71 if holder else 0x95A5A6)
        emb.add_field(name="Fireball", value=str(fireball), inline=True)
        emb.add_field(name="Otherballs", value=", ".join(str(b) for b in balls) if balls else "n/a", inline=True)
        emb.add_field(name="Link", value=RESULTS_URL, inline=False)
        emb.set_footer(text=f"{date.fromisoformat(snap['draw_date']).strftime('%A %b %d, %Y').replace(' 0', ' ')} ({snap['draw'].title()})")
        text = result_text(snap, holder, fireball)
        msg = await self._say(snap, text, embed=emb)
        async with self.store.lock:
            b = self.store.data["boards"].get(board_id)
            if b and msg:
                b["result_msg_id"] = str(msg.id)
                self.store.save()
        await self._refresh(board_id)
        await self._tag(snap, "DONE")

    # ---- slash commands --------------------------------------------------

    @app_commands.command(name="start", description="Open a Fireball game for the next Pick 3 draw")
    @app_commands.describe(
        code="Short game code, e.g. FB18",
        prize="Prize amount. Each of the 10 digits is worth a tenth of it (500 -> 50 tickets a digit)",
        image="Image to post with the announcement (required)",
        notes="Notes posted in the thread, e.g. anything the picture doesn't show (required)",
        draw="Midday or evening (default: the next one still open)",
        bb="90% BB (buyback): the result post shows the winner's buyback, 90% of all tickets (default off)")
    @app_commands.choices(draw=[app_commands.Choice(name="Midday (12:40 PM CT)", value="midday"),
                                app_commands.Choice(name="Evening (9:22 PM CT)", value="evening")])
    async def start(self, interaction: discord.Interaction, code: str,
                    prize: app_commands.Range[int, 1, 1000000], image: discord.Attachment, notes: str,
                    draw: app_commands.Choice[str] | None = None, bb: bool = False):
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "Run `/fireball start` from a regular text channel. I'll create a thread there for the game.",
                ephemeral=True)
            return
        if not image.content_type or not image.content_type.startswith("image/"):
            await interaction.response.send_message(
                "That attachment doesn't look like an image. Please attach a picture.", ephemeral=True)
            return
        if not notes.strip():
            await interaction.response.send_message(
                "Notes can't be blank. Add a line about anything the picture doesn't show.", ephemeral=True)
            return
        code = code.strip()[:40]
        if not code:
            await interaction.response.send_message("Give the game a short code, e.g. FB18.", ephemeral=True)
            return

        async with self.store.lock:
            cfg = get_config(self.store.data, interaction.guild_id)
        if draw is None:
            name, day = next_draw(lock_minutes=cfg["lock_minutes"])
        else:
            name = draw.value
            now = datetime.now(TZ)
            day = now.date()
            if now >= datetime.combine(day, DRAW_TIMES[name], tzinfo=TZ) - timedelta(minutes=cfg["lock_minutes"]):
                day += timedelta(days=1)

        # Posting the picture + thread can take longer than Discord's 3-second window.
        await interaction.response.defer()

        board = new_board(interaction.guild_id, interaction.channel_id, interaction.user.id,
                          interaction.user.display_name, name, day, str(int(prize)), cfg["lock_minutes"],
                          digit_cost=digit_cost_for_prize(int(prize)), code=code,
                          image_url=image.url, notes=notes.strip())
        board["bb"] = bool(bb)
        image_file = await image.to_file()
        announcement = await interaction.channel.send(
            f"🔥 **Fireball {esc(code)} is open!** {draw_title(board)}\n"
            f"Prize: **{int(prize)}** (winner takes all). Each digit is worth **{board['digit_cost']}** tickets."
            f"{' **90% BB** (buyback) is on.' if bb else ''}\n"
            f"Hosted by {interaction.user.mention}. Claims close {ts(board['lock_at'], 'R')}.",
            file=image_file,
        )
        try:
            thread = await announcement.create_thread(name=thread_name("OPEN", code), auto_archive_duration=1440)
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to create threads here. The bot needs the "
                "**Create Public Threads** permission in this channel.", ephemeral=True)
            return

        board["parent_channel_id"] = str(interaction.channel_id)
        board["channel_id"] = str(thread.id)
        board["announce_msg_id"] = str(announcement.id)
        notes_msg = await thread.send(notes_text(board, interaction.user.display_name), view=notes_view(board["id"]))
        board["notes_message_id"] = str(notes_msg.id)
        board_msg = await thread.send(embed=board_embed(board, cfg), view=board_view(board))
        board["board_msg_id"] = str(board_msg.id)
        async with self.store.lock:
            self.store.data["boards"][board["id"]] = board
            self.store.save()
        await interaction.followup.send(f"Fireball thread created: {thread.mention}")

    @app_commands.command(name="notes", description="Host: change the notes on this Fireball game")
    @app_commands.describe(text="The new notes")
    async def notes(self, interaction: discord.Interaction, text: str):
        board = await self._find_board(interaction, ACTIVE_STATUSES + ("locked",))
        if not board:
            await interaction.response.send_message("No live Fireball game in this channel.", ephemeral=True)
            return
        await self.set_notes(interaction, board["id"], text)

    async def open_notes_editor(self, interaction: discord.Interaction, board_id: str):
        """✏️ Edit notes button: pop-up pre-filled with the current notes."""
        async with self.store.lock:
            board = self.store.data["boards"].get(board_id)
            snap = json.loads(json.dumps(board)) if board else None
        if not snap or snap["status"] == "cancelled":
            await interaction.response.send_message("This game is no longer active.", ephemeral=True)
            return
        if not can_host(interaction, snap):
            await interaction.response.send_message("Only the host or a mod can edit the notes.", ephemeral=True)
            return
        await interaction.response.send_modal(NotesModal(self, board_id, snap.get("notes") or ""))

    async def set_notes(self, interaction: discord.Interaction, board_id: str, text: str):
        """Shared by /fireball notes and the Edit notes pop-up: save, then edit the notes message in place."""
        text = text.strip()[:NOTES_MAX_LENGTH]
        async with self.store.lock:
            board = self.store.data["boards"].get(board_id)
            if not board:
                await interaction.response.send_message("This game is no longer active.", ephemeral=True)
                return
            if not can_host(interaction, board):
                await interaction.response.send_message("Only the host or a mod can edit the notes.", ephemeral=True)
                return
            if not text:
                await interaction.response.send_message("Notes can't be blank.", ephemeral=True)
                return
            board["notes"] = text
            self.store.save()
            snap = json.loads(json.dumps(board))
        content = notes_text(snap, interaction.user.display_name)
        ch = await self._channel(snap)
        if snap.get("notes_message_id") and ch is not None:
            try:
                # Re-attaching the view also gives notes posted before the button existed an Edit button.
                await ch.get_partial_message(int(snap["notes_message_id"])).edit(
                    content=content, view=notes_view(board_id))
                await interaction.response.send_message("✅ Notes updated.", ephemeral=True)
                return
            except discord.HTTPException:
                pass
        # The original notes message is gone: post a fresh one and track that instead.
        await interaction.response.send_message("✅ Notes updated.", ephemeral=True)
        if ch is not None:
            msg = await ch.send(content, view=notes_view(board_id))
            async with self.store.lock:
                b = self.store.data["boards"].get(board_id)
                if b:
                    b["notes_message_id"] = str(msg.id)
                    self.store.save()

    async def _find_board(self, interaction: discord.Interaction, statuses: tuple[str, ...]) -> dict | None:
        async with self.store.lock:
            found = [b for b in self.store.data["boards"].values()
                     if str(interaction.channel_id) in (b["channel_id"], str(b["sat"].get("thread_id")))
                     and b["status"] in statuses]
        found.sort(key=lambda b: b["created_at"])
        return found[-1] if found else None

    async def cancel(self, interaction: discord.Interaction):
        """/fireball-cancel. A top-level command (not under /fireball) so Discord can hide it from non-mods."""
        board = await self._find_board(interaction, ACTIVE_STATUSES + ("locked",))
        if not board:
            await interaction.response.send_message("No live Fireball board in this channel.", ephemeral=True)
            return
        if not is_mod(interaction):
            await interaction.response.send_message("Only moderators can cancel a Fireball board.", ephemeral=True)
            return
        async with self.store.lock:
            b = self.store.data["boards"][board["id"]]
            if b["status"] == "animating":
                await interaction.response.send_message("The satellite is mid-animation. Give it a moment.", ephemeral=True)
                return
            returned = void_charges(b)
            b["status"] = "cancelled"
            self.store.save()
            snap = json.loads(json.dumps(b))
        await interaction.response.send_message(
            f"🛑 Fireball board cancelled. All claims are void (**{returned}** ticket(s) worth).")
        await self._refresh(snap["id"])
        await self._tag(snap, "CANCELLED")
        await self._retire_sat(snap)

    @app_commands.command(name="config", description="Admin: show or change Fireball settings")
    @app_commands.describe(lock_minutes="Lock claims this many minutes before the draw")
    async def config(self, interaction: discord.Interaction, lock_minutes: int | None = None):
        if not is_admin(interaction):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        changes = {} if lock_minutes is None else {"lock_minutes": lock_minutes}
        for k, v in changes.items():
            lo, hi = CONFIG_LIMITS[k]
            if not lo <= v <= hi:
                await interaction.response.send_message(f"`{k}` must be between {lo} and {hi}.", ephemeral=True)
                return
        async with self.store.lock:
            guild_state(self.store.data, interaction.guild_id)["config"].update(changes)
            self.store.save()
            cfg = get_config(self.store.data, interaction.guild_id)
        lines = "\n".join(f"`{k}`: **{v}**" for k, v in cfg.items())
        await interaction.response.send_message(
            ("Updated.\n" if changes else "Current Fireball settings:\n") + lines, ephemeral=True)

    @app_commands.command(name="result", description="Host: enter or re-check the Fireball result")
    @app_commands.describe(fireball="The Fireball digit (leave blank to re-check illinoislottery.com now)")
    async def result(self, interaction: discord.Interaction, fireball: app_commands.Range[int, 0, 9] | None = None):
        board = await self._find_board(interaction, ("locked", "open", "satellite"))
        if not board:
            await interaction.response.send_message("No Fireball board here waiting for a result.", ephemeral=True)
            return
        if str(interaction.user.id) != board["host_id"] and not is_admin(interaction):
            await interaction.response.send_message("Only the host or an admin can do that.", ephemeral=True)
            return
        if board["status"] != "locked":
            await interaction.response.send_message("The board isn't locked yet. Close it first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if fireball is not None:
            await self._settle(board["id"], None, int(fireball))
            await interaction.followup.send("Result recorded.", ephemeral=True)
            return
        try:
            found = find_result(await fetch_results(self.session), board)
        except Exception:
            log.warning("Manual Fireball fetch failed", exc_info=True)
            found = None
        if found:
            await self._settle(board["id"], found["balls"], found["fireball"])
            await interaction.followup.send("Found it and posted the result.", ephemeral=True)
        else:
            await interaction.followup.send(
                "Couldn't find that draw on illinoislottery.com yet. Try again in a few minutes, or enter it with "
                "`/fireball result fireball:<digit>`.", ephemeral=True)


@app_commands.command(name="fireball-cancel", description="Mods: cancel the live Fireball board here and void every claim")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
async def fireball_cancel(interaction: discord.Interaction):
    if _active_cog is None:
        await interaction.response.send_message("Fireball isn't loaded right now.", ephemeral=True)
        return
    await _active_cog.cancel(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(FireballCog(bot))
    bot.tree.add_command(fireball_cancel, override=True)
