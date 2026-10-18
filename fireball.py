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


def digit_cost_for_prize(prize: int) -> int:
    """Each of the 10 digits is worth a tenth of the prize (prize 500 -> 50 tickets a digit)."""
    return max(1, math.ceil(prize / 10))


def satellite_cost(digit_cost: int) -> int:
    """
    One satellite spot's price: the cost of the digits it is for (the 3 that are
    still open when it starts), split evenly across the 10 spots. Whole tickets
    only, so it rounds up (3 digits at 10 tickets = 30 tickets / 10 spots = 3).
    """
    return math.ceil((10 - SAT_THRESHOLD) * digit_cost / SAT_SPOTS)


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
    room = SAT_THRESHOLD - len(claimed_digits(board))
    clamped = False
    if len(avail) > room:
        avail = random.sample(avail, room)
        clamped = True
    cost = board["digit_cost"] * len(avail)
    charge(board, user_id, cost, "digit", ",".join(str(d) for d in sorted(avail)))
    for d in avail:
        board["digits"][str(d)] = {"user_id": str(user_id), "name": name, "via": "claim"}
    opened = False
    if len(claimed_digits(board)) >= SAT_THRESHOLD:
        board["status"] = "satellite"
        board["sat"]["reserved"] = open_digits(board)
        opened = True
    shown = ", ".join(str(d) for d in sorted(avail))
    msg = f"You claimed digit **{shown}** for **{cost}** ticket(s). You're in for **{owed(board, user_id)}** on this game."
    if clamped:
        msg += " (Rounded down to what was left before the satellite.)"
    return {"ok": True, "msg": msg, "claimed": sorted(avail), "opened_sat": opened}


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
    if filled:
        board["status"] = "animating"
        board["sat"]["outcome"] = compute_outcome(board)
    return {
        "ok": True,
        "msg": f"You're in the satellite ({sum(1 for x in spots if x)}/{SAT_SPOTS}) for **{cost}** ticket(s). You're in for **{owed(board, user_id)}** on this game.",
        "filled": filled,
    }


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


def lock_board(data: dict, board: dict) -> str:
    """Close claiming (Close button or draw time). Returns a short note for the channel."""
    note = ""
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


def spot_labels(spots: list[dict]) -> list[str]:
    """Display names for ducks; duplicates get (1), (2)... so you can tell them apart."""
    names = [s["name"] for s in spots]
    labels = []
    seen: dict[str, int] = {}
    for nm in names:
        if names.count(nm) > 1:
            seen[nm] = seen.get(nm, 0) + 1
            labels.append(f"{nm} ({seen[nm]})")
        else:
            labels.append(nm)
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
                     f"each digit is worth **{board['digit_cost']}**\n\n" + "\n".join(lines)),
        color=colors.get(status, 0xE67E22),
    )
    state_text = {
        "open": f"Claims close {ts(board['lock_at'], 'R')} · draw at {ts(board['draw_at'], 't')}",
        "satellite": "7 digits are gone. The satellite is open below for the last 3.",
        "animating": "Satellite is full. The shuffle and the fire are underway...",
        "locked": f"Claims are closed. Draw at {ts(board['draw_at'], 't')}. Result posts automatically.",
        "settled": "Settled. See the result below.",
        "cancelled": "Cancelled. All tickets were returned.",
    }
    emb.add_field(name="Status", value=state_text.get(status, status), inline=False)
    emb.set_footer(
        text=f"Digit: {board['digit_cost']} ticket(s) · Satellite spot: {board['sat_cost']} (the last 3 digits split 10 ways) · Host: {board['host_name']}"
    )
    return emb


def sat_embed(board: dict, cfg: dict) -> discord.Embed:
    spots = board["sat"]["spots"]
    taken = sum(1 for s in spots if s)
    reserved = ", ".join(str(d) for d in board["sat"]["reserved"]) or "none"
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    for sp in spots:
        if sp:
            counts[sp["user_id"]] = counts.get(sp["user_id"], 0) + 1
            names[sp["user_id"]] = sp["name"]
    entered = ", ".join(esc(names[u]) + (f" x{n}" if n > 1 else "") for u, n in counts.items()) or "nobody yet"
    prefs = board["sat"].get("prefs") or {}
    pref_text = ""
    if prefs:
        pref_text = "\n\n**Spot preferences:**\n" + "\n".join(
            f"<@{uid}> " + ",".join(str(d) for d in lst) for uid, lst in prefs.items() if lst)
    emb = discord.Embed(
        title="🦆🔥 Fireball Satellite",
        description=(
            f"**{taken}/{SAT_SPOTS}** spots filled · **{board['sat_cost']}** ticket(s) each.\n"
            f"Tap **X1** to take a spot. When it fills, the ducks are lit on fire and the **last 3 still burning** "
            f"each win one of the open digits: **{reserved}**.\n"
            f"Tap **⭐ Preferences** to rank those digits. Winners get their best available pick.\n\n"
            f"**Entered:** {entered}" + pref_text
        ),
        color=0xF1C40F if board["status"] == "satellite" else 0x7F8C8D,
    )
    return emb


class BoardButton(discord.ui.DynamicItem[discord.ui.Button], template=r"fb:(?P<board>[0-9a-f]+):(?P<action>d\d|yolo|odds|evens|close)"):
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
    closable = board["status"] in ("open", "satellite")
    view.add_item(BoardButton(board["id"], "close", "Close", discord.ButtonStyle.danger, not closable, row=2))
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
        self.bot.add_dynamic_items(BoardButton, SatButton)
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
            elif b["status"] == "locked" and not b.get("result"):
                await self._maybe_fetch_result(bid, now)

    # ---- helpers ---------------------------------------------------------

    def _lock_for(self, board_id: str) -> asyncio.Lock:
        return self.edit_locks.setdefault(board_id, asyncio.Lock())

    async def _channel(self, board: dict):
        cid = int(board["channel_id"])
        ch = self.bot.get_channel(cid)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(cid)
            except (discord.NotFound, discord.Forbidden):
                return None
        return ch

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
                if board["sat"]["msg_id"]:
                    await ch.get_partial_message(int(board["sat"]["msg_id"])).edit(
                        embed=sat_embed(board, cfg), view=sat_view(board))
            except discord.HTTPException:
                log.exception("Could not edit Fireball board %s", board_id)

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
            return await ch.send(content, **kw)
        except discord.HTTPException:
            log.exception("Could not post in channel for board %s", board["id"])
            return None

    # ---- button handlers -------------------------------------------------

    async def on_board_click(self, interaction: discord.Interaction, board_id: str, action: str):
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
                if str(user.id) != board["host_id"] and not is_admin(interaction):
                    await interaction.response.send_message("Only the host or an admin can close the board.", ephemeral=True)
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
        if opened_sat:
            await self._post_satellite(board_id)

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

        await interaction.response.defer()
        await self._refresh(board_id)
        await interaction.followup.send(result["msg"], ephemeral=True)
        if filled:
            self._start_finale(board_id, animate=True)

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

    async def _post_satellite(self, board_id: str):
        async with self.store.lock:
            board = self.store.data["boards"].get(board_id)
            if not board:
                return
            snap = json.loads(json.dumps(board))
            cfg = get_config(self.store.data, board["guild_id"])
        msg = await self._say(
            snap,
            "🦆🔥 **7 digits are claimed. The satellite is open for the last 3!**",
            embed=sat_embed(snap, cfg), view=sat_view(snap),
        )
        if msg:
            async with self.store.lock:
                b = self.store.data["boards"].get(board_id)
                if b:
                    b["sat"]["msg_id"] = str(msg.id)
                    self.store.save()

    # ---- shuffle + finale ------------------------------------------------

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
            ch = await self._channel(snap)

            if animate and ch is not None:
                await self._play_shuffle(ch, labels, out)
                await self._play_fire(ch, labels, out, snap)

            async with self.store.lock:
                board = self.store.data["boards"][board_id]
                apply_outcome(board)
                self.store.save()
                snap = json.loads(json.dumps(board))
            await self._refresh(board_id)
            await self._tag(snap, "CLOSED")

            out = snap["sat"]["outcome"]
            lines = []
            for d, idx in sorted(out["assignment"].items()):
                s = snap["sat"]["spots"][idx]
                lines.append(
                    f"🎉 Congrats <@{s['user_id']}>! Digit **{d}** on the Fireball board has been called "
                    f"for you ({out.get('how', {}).get(d, 'assigned')}).")
            await self._say(
                snap,
                "🔥 **The fire has settled.** The last three ducks standing:\n" + "\n".join(lines) +
                f"\n\nThe board is full. Draw at {ts(snap['draw_at'], 't')}; the result posts automatically.",
            )
        except Exception:
            log.exception("Fireball finale crashed for board %s", board_id)

    async def _play_shuffle(self, ch, labels: list[str], out: dict):
        rounds = out["rounds"]
        total = len(rounds)
        msg = await ch.send("🌀 **The satellite is full! Shuffling the ducks...**")
        await asyncio.sleep(SHUFFLE_DELAY)
        for r, perm in enumerate(rounds, start=1):
            if r < total:
                top = "\n".join(f"{i + 1}. {esc(labels[idx])}" for i, idx in enumerate(perm[:3]))
                text = f"🌀 **Shuffle round {r}/{total}** — top 3:\n{top}"
            else:
                full = "\n".join(f"{i + 1}. {esc(labels[idx])}" for i, idx in enumerate(perm))
                text = f"🌀 **Final shuffle ({r}/{total})** — full order:\n{full}"
            try:
                await msg.edit(content=text)
            except discord.HTTPException:
                msg = await ch.send(text)
            await asyncio.sleep(SHUFFLE_DELAY)

    async def _play_fire(self, ch, labels: list[str], out: dict, board: dict):
        """The ducks-on-fire finale: an animated GIF of them racing. Falls back to a text version."""
        try:
            from fireanim import render_finale_gif
            gif, seconds = await asyncio.to_thread(
                render_finale_gif, labels, out["elim_order"], out["winners"], board["id"])
            if len(gif) > 7_500_000:
                raise RuntimeError(f"finale GIF too big ({len(gif)} bytes)")
            await ch.send("🔥🔥 **THE DUCKS ARE ON FIRE** 🔥🔥",
                          file=discord.File(io.BytesIO(gif), filename="duck-fire.gif"))
            await asyncio.sleep(seconds * FINALE_WAIT_SCALE)
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

        msg = await ch.send(render("Ten ducks. Three survive. Nobody is safe..."))
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
        emb = discord.Embed(
            title=f"🔥 Fireball result — {draw_title(snap)}",
            description=f"**Pick 3:** {pick}\n**Fireball:** 🔥 **{fireball}**",
            color=0x2ECC71 if holder else 0x95A5A6,
        )
        if holder:
            via = " (won it in the satellite 🦆)" if holder.get("via") == "satellite" else ""
            text = f"🏆 <@{holder['user_id']}> held digit **{fireball}**{via} and wins the **{snap['prize']}** prize!"
        else:
            text = f"Nobody held digit **{fireball}**. No winner this draw."
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
        draw="Midday or evening (default: the next one still open)")
    @app_commands.choices(draw=[app_commands.Choice(name="Midday (12:40 PM CT)", value="midday"),
                                app_commands.Choice(name="Evening (9:22 PM CT)", value="evening")])
    async def start(self, interaction: discord.Interaction, code: str,
                    prize: app_commands.Range[int, 1, 1000000], image: discord.Attachment, notes: str,
                    draw: app_commands.Choice[str] | None = None):
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
        image_file = await image.to_file()
        announcement = await interaction.channel.send(
            f"🔥 **Fireball {esc(code)} is open!** {draw_title(board)}\n"
            f"Prize: **{int(prize)}** (winner takes all). Each digit is worth **{board['digit_cost']}** tickets.\n"
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

        board["channel_id"] = str(thread.id)
        board["announce_msg_id"] = str(announcement.id)
        notes_msg = await thread.send(f"📋 **Host notes ({esc(interaction.user.display_name)}):** {board['notes']}")
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
        if str(interaction.user.id) != board["host_id"] and not is_admin(interaction):
            await interaction.response.send_message("Only the host or an admin can edit the notes.", ephemeral=True)
            return
        if not text.strip():
            await interaction.response.send_message("Notes can't be blank.", ephemeral=True)
            return
        content = f"📋 **Host notes ({esc(board.get('notes_author') or interaction.user.display_name)}):** {text.strip()}"
        async with self.store.lock:
            self.store.data["boards"][board["id"]]["notes"] = text.strip()
            self.store.save()
        ch = await self._channel(board)
        try:
            if board.get("notes_message_id") and ch is not None:
                await ch.get_partial_message(int(board["notes_message_id"])).edit(content=content)
                await interaction.response.send_message("Notes updated.", ephemeral=True)
                return
        except discord.HTTPException:
            pass
        await interaction.response.send_message(content)

    async def _find_board(self, interaction: discord.Interaction, statuses: tuple[str, ...]) -> dict | None:
        async with self.store.lock:
            found = [b for b in self.store.data["boards"].values()
                     if b["channel_id"] == str(interaction.channel_id) and b["status"] in statuses]
        found.sort(key=lambda b: b["created_at"])
        return found[-1] if found else None

    @app_commands.command(name="cancel", description="Cancel the live Fireball board here and void every claim")
    async def cancel(self, interaction: discord.Interaction):
        board = await self._find_board(interaction, ACTIVE_STATUSES + ("locked",))
        if not board:
            await interaction.response.send_message("No live Fireball board in this channel.", ephemeral=True)
            return
        if str(interaction.user.id) != board["host_id"] and not is_admin(interaction):
            await interaction.response.send_message("Only the host or an admin can cancel.", ephemeral=True)
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


async def setup(bot: commands.Bot):
    await bot.add_cog(FireballCog(bot))
