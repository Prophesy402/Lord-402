"""
Duck Race Bot
-------------
Runs the full lifecycle of a duck race *except* money: opening a race,
letting people call spots by replying "X2"/"X3" in the channel, tracking
capacity, collecting a simple non-monetary "confirm" from everyone who
called a spot, shuffling the field, handing you a paste-ready order for
an external race tool (e.g. https://www.duckrace-game.com), and catching
the results when you paste them back — even if Discord splits the paste
across multiple messages.

This bot deliberately does NOT collect or track payments. If your race
has an entry fee, handle that yourselves (Venmo, Cash App, cash, whatever)
outside the bot, then have people type "confirm" once they're square.

One race is tracked per channel. Typical flow in a race channel:

  1. Host runs /create race code:ER13 total_spots:10
       -> channel renamed to "[OPEN] - ER13"
  2. People type "X3", "X1", "2", etc. to call spots
       -> bot replies with running total + spots remaining
       -> when full, bot announces it and renames channel to "[PENDING] - ER13"
  3. Everyone who called a spot types "confirm" (or "ready")
       -> when everyone's confirmed, bot renames channel to "[CLOSED] - ER13"
  4. Host runs /duck race
       -> bot posts the full entry list, then a shuffled paste-ready order
  5. Host runs the race on the external tool, pastes the results back
       -> bot detects and reassembles the paste automatically
  6. /duck reset to clear the channel for the next race
"""

import asyncio
import io
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATA_PATH = Path(__file__).parent / "data" / "races.json"
MAX_RACE_SIZE = 200
SPOT_CALL_DELAY_SECONDS = 30
SAT_THRESHOLD = 0.70  # notify host once the race is this fraction full

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("duckbot")

intents = discord.Intents.default()
intents.message_content = True  # required to read "X3" / "confirm" / pasted results
bot = commands.Bot(command_prefix="!", intents=intents)

SPOT_CALL_RE = re.compile(r"^x(\d{1,3})$", re.IGNORECASE)
CONFIRM_WORDS = {"confirm", "confirmed", "ready", "in", "i'm in", "im in", "sip", "sipped"}
VOUCH_RE = re.compile(r"^vouch for\s+<@!?(\d+)>", re.IGNORECASE)
FORCE_SIP_RE = re.compile(r"^force sip\s+<@!?(\d+)>", re.IGNORECASE)
RESULT_LINE_RE = re.compile(r"^\s*\d{1,3}[\.\)]\s+\S+", re.MULTILINE)
RESULT_DEBOUNCE_SECONDS = 8
RESULT_MIN_LINES = 3


# ---------- storage ----------

def _load_all() -> dict:
    if not DATA_PATH.exists():
        return {}
    try:
        with DATA_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log.exception("Failed to read %s, starting fresh", DATA_PATH)
        return {}


def _save_all(data: dict) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = DATA_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp_path.replace(DATA_PATH)


def get_race(channel_id: int) -> dict | None:
    data = _load_all()
    return data.get(str(channel_id))


def save_race(channel_id: int, race: dict | None) -> None:
    data = _load_all()
    key = str(channel_id)
    if race is None:
        data.pop(key, None)
    else:
        data[key] = race
    _save_all(data)


def new_race(
    code: str,
    size: int,
    opened_by: str,
    title: str | None = None,
    ticket_label: str = "spot",
    image_url: str | None = None,
) -> dict:
    return {
        "code": code,
        "title": title or code,
        "ticket_label": ticket_label or "spot",
        "image_url": image_url,
        "size": size,
        "status": "open",  # open -> pending -> closed -> shuffled -> complete
        "opened_by": opened_by,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "entries": [],       # [{user_id, name, spots, at}], one row per call
        "confirmed": [],     # [user_id, ...]
        "vouched": [],       # [user_id, ...] — someone else vouched they'll sip, but haven't yet
        "shuffled_order": None,
        "results_raw": None,
        "spot_calls_open_at": None,  # set once the announcement is posted
        "sat_notified": False,    # has the host already been asked about SATs
        "sat_active": False,      # True while this race is paused for a satellite race
        "sat_thread_id": None,    # id of the linked satellite race's thread, while active
        "is_sat": False,          # True if THIS race is itself a satellite race
        "sat_parent_id": None,    # if is_sat, the channel/thread id of the race it feeds into
        "notes": None,             # current host-notes text, if any
        "notes_message_id": None,  # id of the "Host notes" message in the thread, for editing
    }


def unit_label(race: dict) -> str:
    return race.get("ticket_label") or "spot"


def entry_totals(race: dict) -> dict[str, int]:
    """user_id -> total spots claimed, preserving first-call order."""
    totals: dict[str, int] = {}
    for e in race["entries"]:
        totals[e["user_id"]] = totals.get(e["user_id"], 0) + e["spots"]
    return totals


def remaining_spots(race: dict) -> int:
    return race["size"] - sum(entry_totals(race).values())


def participant_names(race: dict) -> dict[str, str]:
    names: dict[str, str] = {}
    for e in race["entries"]:
        names[e["user_id"]] = e["name"]
    return names


# ---------- in-memory result-paste buffering (per channel) ----------
# Discord messages cap at ~2000 chars, so a pasted results block can arrive
# as several consecutive messages. We buffer them briefly and reassemble.

_result_buffers: dict[int, dict] = {}  # channel_id -> {"author_id", "parts": [...], "task": Task}


async def _finalize_result_buffer(channel: discord.abc.Messageable, channel_id: int):
    buf = _result_buffers.pop(channel_id, None)
    if not buf:
        return
    full_text = "\n".join(buf["parts"]).strip()

    race = get_race(channel_id)
    if not race or race["status"] != "shuffled":
        return  # race moved on / was reset while we were waiting

    race["results_raw"] = full_text
    race["status"] = "complete"
    save_race(channel_id, race)

    if race.get("is_sat"):
        await channel.send(
            f"🏆 **SAT results captured for {race['code']}!**\n"
            f"```{full_text[:1800]}```\n"
            f"Moving the winners into the main race now..."
        )
        await _apply_sat_results(race, channel_id, full_text)
    else:
        await channel.send(
            f"🏆 **Results captured for {race['code']}!**\n"
            f"```{full_text[:1800]}```\n"
            f"Race marked complete. Run `/duck reset` when you're ready to open the next one."
        )


def _match_name_to_uid(name: str, names_by_id: dict[str, str]) -> str | None:
    target = name.strip().lower()
    if not target:
        return None
    for uid, nm in names_by_id.items():
        if nm.strip().lower() == target:
            return uid
    return None


async def _apply_sat_results(sat_race: dict, sat_channel_id: int, raw_text: str):
    """Parses the pasted SAT winners list, matches names against who actually
    called spots in the satellite race, and moves them straight into the main
    race's entries — then unpauses the main race."""
    main_channel_id_str = sat_race.get("sat_parent_id")
    if not main_channel_id_str:
        return
    try:
        main_channel_id = int(main_channel_id_str)
    except (TypeError, ValueError):
        return

    main_race = get_race(main_channel_id)
    if not main_race:
        return

    names_by_id = participant_names(sat_race)

    matched_uids: list[str] = []
    unmatched: list[str] = []
    for line in raw_text.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        cleaned = re.sub(r"^\s*\d{1,3}[\.\)]\s*", "", cleaned)
        cleaned = re.sub(r"\s*\(\d+\)\s*$", "", cleaned).strip()
        if not cleaned:
            continue
        uid = _match_name_to_uid(cleaned, names_by_id)
        if uid:
            matched_uids.append(uid)
        else:
            unmatched.append(cleaned)

    remaining = remaining_spots(main_race)
    take = matched_uids[:remaining] if remaining > 0 else []

    win_counts: dict[str, int] = {}
    for uid in take:
        win_counts[uid] = win_counts.get(uid, 0) + 1

    for uid, count in win_counts.items():
        main_race["entries"].append(
            {
                "user_id": uid,
                "name": names_by_id.get(uid, "Unknown"),
                "spots": count,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )

    main_race["sat_active"] = False
    main_race["sat_thread_id"] = None
    save_race(main_channel_id, main_race)

    try:
        main_channel = bot.get_channel(main_channel_id) or await bot.fetch_channel(main_channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        main_channel = None
    if not main_channel:
        return

    unit = unit_label(main_race)
    remaining_after = remaining_spots(main_race)
    added_mentions = (
        ", ".join(f"<@{uid}> ({c} {unit}(s))" for uid, c in win_counts.items())
        if win_counts
        else "nobody — couldn't match any names from the pasted list"
    )
    note = ""
    if unmatched:
        shown = ", ".join(unmatched[:10])
        more = " …" if len(unmatched) > 10 else ""
        note = f"\n⚠️ Couldn't match to a SAT entrant: {shown}{more}"

    if remaining_after <= 0:
        main_race["status"] = "pending"
        save_race(main_channel_id, main_race)
        await rename_channel(main_channel, "PENDING", main_race["code"])
        await main_channel.send(
            f"🛟 **SAT results are in!** Added: {added_mentions}.{note}\n"
            f"🏁 **{main_race['title']} is now full!** Everyone above: reply **Sip** once you're "
            f"squared away."
        )
    else:
        await main_channel.send(
            f"🛟 **SAT results are in!** Added: {added_mentions}.{note}\n"
            f"**{main_race['title']}** is ready to go — **{remaining_after}** {unit}(s) still open, "
            f"spot calls are back on with `X#`."
        )


def _queue_result_fragment(channel: discord.abc.Messageable, channel_id: int, author_id: str, text: str):
    buf = _result_buffers.get(channel_id)
    if buf and buf["author_id"] == author_id:
        buf["parts"].append(text)
        buf["task"].cancel()
    else:
        buf = {"author_id": author_id, "parts": [text], "task": None}
        _result_buffers[channel_id] = buf

    async def _timer():
        try:
            await asyncio.sleep(RESULT_DEBOUNCE_SECONDS)
            await _finalize_result_buffer(channel, channel_id)
        except asyncio.CancelledError:
            pass

    buf["task"] = asyncio.create_task(_timer())


def _looks_like_results(text: str) -> bool:
    if "winner" in text.lower():
        return True
    return len(RESULT_LINE_RE.findall(text)) >= RESULT_MIN_LINES


# ---------- core actions (shared by slash commands and text triggers) ----------

async def rename_channel(channel: discord.abc.GuildChannel, status_label: str, code: str):
    new_name = f"[{status_label}] - {code}"
    try:
        await channel.edit(name=new_name)
    except (discord.Forbidden, discord.HTTPException):
        log.warning("Couldn't rename channel %s to %s", channel.id, new_name)


async def handle_spot_call(message: discord.Message, spots: int):
    channel_id = message.channel.id
    race = get_race(channel_id)
    if not race or race["status"] != "open":
        return

    if spots < 1:
        return

    open_at_raw = race.get("spot_calls_open_at")
    if open_at_raw:
        open_at = datetime.fromisoformat(open_at_raw)
        now = datetime.now(timezone.utc)
        if now < open_at:
            seconds_left = max(1, round((open_at - now).total_seconds()))
            await message.reply(
                f"⏳ Hold on — spot calls open in **{seconds_left}s**.",
                mention_author=False,
            )
            return

    unit = unit_label(race)
    remaining = remaining_spots(race)
    if remaining <= 0:
        return

    clamped = False
    if spots > remaining:
        spots = remaining
        clamped = True

    user_id = str(message.author.id)
    race["entries"].append(
        {
            "user_id": user_id,
            "name": message.author.display_name,
            "spots": spots,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    )
    save_race(channel_id, race)

    totals = entry_totals(race)
    remaining_after = remaining_spots(race)
    at_str = datetime.now().strftime("%I:%M:%S %p").lstrip("0")

    clamp_note = " (rounded down to what was left)" if clamped else ""

    if remaining_after > 0:
        await message.reply(
            f"🦆 **Lord 402** recorded {message.author.mention} calling **{spots}** {unit}(s){clamp_note} "
            f"({totals[user_id]} total) at {at_str} — **{remaining_after}** {unit}(s) remaining.",
            mention_author=False,
        )

        fraction_full = (race["size"] - remaining_after) / race["size"]
        if (
            fraction_full >= SAT_THRESHOLD
            and not race.get("sat_notified")
            and not race.get("sat_active")
        ):
            race["sat_notified"] = True
            save_race(channel_id, race)
            await _notify_sat_threshold(message.channel, race)
    else:
        await message.reply(
            f"🦆 **Lord 402** recorded {message.author.mention} calling **{spots}** {unit}(s){clamp_note} "
            f"({totals[user_id]} total) at {at_str} — race is now full.",
            mention_author=False,
        )
        race["status"] = "pending"
        save_race(channel_id, race)
        await rename_channel(message.channel, "PENDING", race["code"])

        names = participant_names(race)
        roster_lines = "\n".join(f"- <@{uid}> ({n} {unit}(s))" for uid, n in totals.items())
        await message.channel.send(
            f"🏁 **RACE IS FULL — {race['title']}**\n"
            f"All {race['size']} {unit}(s) are claimed:\n{roster_lines}\n\n"
            f"Everyone above: reply **Sip** in this channel once you're squared away "
            f"(payment, if any, is handled outside this bot)."
        )


async def _notify_sat_threshold(channel: discord.abc.Messageable, race: dict):
    """Pings the host both in-thread and by DM once the race crosses the SAT
    threshold, asking whether they want to open a satellite race."""
    host_id = race["opened_by"]
    await channel.send(
        f"📣 <@{host_id}> — **{race['title']}** just hit **70%+ full**! Want to open a "
        f"**satellite race** so more people can compete for the remaining spots? "
        f"Reply `yes sats` or `no sats` right here in the thread."
    )
    try:
        user = await bot.fetch_user(int(host_id))
        await user.send(
            f"🦆 Your race **{race['title']}** just hit 70%+ full. Head to the race thread "
            f"and reply `yes sats` or `no sats` to decide whether to open a satellite race."
        )
    except (discord.Forbidden, discord.HTTPException, ValueError):
        pass


async def start_sat_race(channel: discord.abc.Messageable, channel_id: int):
    """Pauses the main race's spot calls so the host can open a linked
    satellite race thread with /racesat."""
    race = get_race(channel_id)
    if not race or race["status"] != "open":
        await channel.send("Can't start a satellite race right now.")
        return
    if race.get("sat_active"):
        await channel.send("A satellite race is already running here.")
        return
    if not race.get("sat_notified"):
        await channel.send(
            f"🚫 Can't open a satellite race yet — the race needs to hit "
            f"**{round(SAT_THRESHOLD * 100)}%** full first."
        )
        return

    remaining = remaining_spots(race)
    if remaining <= 0:
        await channel.send("The main race is already full — no spots left to satellite for.")
        return

    race["sat_active"] = True
    save_race(channel_id, race)

    await channel.send(
        f"🛟 **Satellite mode is ON for {race['title']}!** Main-race spot calls are paused "
        f"with **{remaining}** spot(s) still open.\n"
        f"Host: run `/racesat` right here in this thread to open the satellite race "
        f"(code, total spots, image, and notes) — spot calls here will reopen once it's finished."
    )


async def handle_confirm(message: discord.Message):
    channel_id = message.channel.id
    race = get_race(channel_id)
    sip_allowed = race["status"] in ("pending", "closed", "shuffled", "complete") or (
        race["status"] == "open" and race.get("sat_active")
    )
    if not race or not sip_allowed:
        return

    totals = entry_totals(race)
    user_id = str(message.author.id)
    if user_id not in totals:
        return  # didn't call a spot, nothing to confirm

    if user_id in race["confirmed"]:
        await message.reply("Already got your confirmation. 👍", mention_author=False)
        return

    race["confirmed"].append(user_id)
    vouched = race.setdefault("vouched", [])
    if user_id in vouched:
        vouched.remove(user_id)
    save_race(channel_id, race)

    await message.reply(f"✅ Got it, {message.author.mention}!", mention_author=False)

    if race["status"] == "pending" and set(race["confirmed"]) >= set(totals.keys()):
        await message.channel.send(waiting_on_host_message(race))


async def handle_vouch(message: discord.Message, target_id: str):
    channel_id = message.channel.id
    race = get_race(channel_id)
    vouch_allowed = race and (
        race["status"] in ("pending", "closed", "shuffled", "complete")
        or (race["status"] == "open" and race.get("sat_active"))
    )
    if not vouch_allowed:
        return

    totals = entry_totals(race)
    if target_id not in totals:
        await message.reply("That person doesn't have a spot in this race.", mention_author=False)
        return

    if target_id in race["confirmed"]:
        await message.reply("They already sipped — no vouch needed.", mention_author=False)
        return

    if target_id == str(message.author.id):
        await message.reply("You can't vouch for yourself — just say `sip`. 😂", mention_author=False)
        return

    vouched = race.setdefault("vouched", [])
    if target_id not in vouched:
        vouched.append(target_id)
        save_race(channel_id, race)

    names = participant_names(race)
    target_name = names.get(target_id, f"<@{target_id}>")
    await message.reply(
        f"🤝 {message.author.mention} vouched for {target_name} — marked as **vouched**, "
        f"still needs to actually sip.",
        mention_author=False,
    )


async def handle_force_sip(message: discord.Message, target_id: str):
    """Host/admin override: marks someone as having actually sipped, for when
    they forgot to type it themselves. Counts as a real sip (unlike a vouch),
    so it satisfies xfinish too."""
    channel_id = message.channel.id
    race = get_race(channel_id)
    sip_allowed = race and (
        race["status"] in ("pending", "closed", "shuffled", "complete")
        or (race["status"] == "open" and race.get("sat_active"))
    )
    if not sip_allowed:
        return

    is_admin = (
        isinstance(message.author, discord.Member)
        and message.author.guild_permissions.administrator
    )
    is_host = str(message.author.id) == race["opened_by"]
    if not (is_host or is_admin):
        await message.reply(
            "Only the host who opened this race, or an admin, can force a sip.",
            mention_author=False,
        )
        return

    totals = entry_totals(race)
    if target_id not in totals:
        await message.reply("That person doesn't have a spot in this race.", mention_author=False)
        return

    names = participant_names(race)
    target_name = names.get(target_id, f"<@{target_id}>")

    if target_id in race["confirmed"]:
        await message.reply(f"{target_name} already sipped.", mention_author=False)
        return

    race["confirmed"].append(target_id)
    vouched = race.setdefault("vouched", [])
    if target_id in vouched:
        vouched.remove(target_id)
    save_race(channel_id, race)

    await message.reply(
        f"✅ {message.author.mention} forced a sip for {target_name}.", mention_author=False
    )

    if race["status"] == "pending" and set(race["confirmed"]) >= set(totals.keys()):
        await message.channel.send(waiting_on_host_message(race))


def build_entry_list(race: dict) -> list[str]:
    """One line per spot, grouped by user in call order, e.g. Name (1), Name (2)."""
    lines = []
    names = participant_names(race)
    totals = entry_totals(race)
    seen_order = []
    for e in race["entries"]:
        if e["user_id"] not in seen_order:
            seen_order.append(e["user_id"])
    for uid in seen_order:
        count = totals[uid]
        name = names[uid]
        if count == 1:
            lines.append(name)
        else:
            for i in range(1, count + 1):
                lines.append(f"{name} ({i})")
    return lines


async def _send_numbered_list(
    channel: discord.abc.Messageable, header: str, numbered: str, footer: str = ""
):
    """Sends a numbered entry list. Discord messages cap out at 2000 characters
    (4000 only applies with certain server boost perks, which we can't assume),
    which even a modest race blows right past, so above that size the list goes
    out as a .txt file attachment instead of a code block."""
    body = f"{header}\n```{numbered}```"
    if footer:
        body += f"\n{footer}"
    if len(body) <= 1900:
        await channel.send(body)
        return

    file = discord.File(io.BytesIO(numbered.encode("utf-8")), filename="entry_list.txt")
    text = f"{header}\n📎 List attached as a file (too long to post directly)."
    if footer:
        text += f"\n{footer}"
    await channel.send(text, file=file)


async def run_race_shuffle(
    channel: discord.abc.Messageable,
    channel_id: int,
    requester_id: str | None = None,
    requester_is_admin: bool = False,
):
    race = get_race(channel_id)
    if not race:
        await channel.send("No race is set up in this channel. Start one with `/create race`.")
        return
    if requester_id is not None and requester_id != race["opened_by"] and not requester_is_admin:
        await channel.send("Only the host who opened this race, or an admin, can run the race.")
        return
    if race["status"] in ("shuffled", "complete"):
        line = random.choice(ALREADY_SHUFFLED_ROASTS).format(code=race["code"])
        await channel.send(line)
        return
    if race["status"] not in ("pending", "closed"):
        await channel.send(
            f"Race {race['code']} isn't ready to shuffle yet (status: **{race['status']}**)."
        )
        return

    totals = entry_totals(race)
    vouched = race.get("vouched", [])
    unconfirmed = [
        uid for uid in totals if uid not in race["confirmed"] and uid not in vouched
    ]
    if unconfirmed:
        pending_mentions = ", ".join(f"<@{uid}>" for uid in unconfirmed)
        await channel.send(
            f"🚫 Can't shuffle yet — {pending_mentions} hasn't sipped or been vouched for."
        )
        return

    still_vouched = [
        uid for uid in totals if uid not in race["confirmed"] and uid in vouched
    ]
    if still_vouched:
        vouched_mentions = ", ".join(f"<@{uid}>" for uid in still_vouched)
        await channel.send(
            f"🤝 Building the list with vouched-but-not-yet-sipped: {vouched_mentions}"
        )

    unit = unit_label(race)
    entry_lines = build_entry_list(race)
    numbered = "\n".join(f"{i+1}. {n}" for i, n in enumerate(entry_lines))
    await _send_numbered_list(
        channel,
        f"🦆 **{race['title']}** — the race is set. **{len(entry_lines)}** {unit}(s) enter... only **one** wins. 🏁\n"
        f"📋 **ORIGINAL ENTRY LIST** — for reference only, do **NOT** paste this into the race:",
        numbered,
    )

    shuffled = entry_lines.copy()
    shuffle_passes = random.randint(3, 10)

    shuffle_msg = await channel.send(f"🎲 Shuffling the flock... (pass 1/{shuffle_passes})")
    for i in range(shuffle_passes):
        random.shuffle(shuffled)
        try:
            await shuffle_msg.edit(content=f"🎲 Shuffling the flock... (pass {i + 1}/{shuffle_passes})")
        except discord.HTTPException:
            pass
        await asyncio.sleep(0.6)

    race["shuffled_order"] = shuffled
    race["status"] = "shuffled"
    save_race(channel_id, race)

    shuffled_numbered = "\n".join(f"{i+1}. {n}" for i, n in enumerate(shuffled))
    await _send_numbered_list(
        channel,
        "🔀 **SHUFFLED RACE ORDER — PASTE THIS into the race:**",
        shuffled_numbered,
        footer=(
            "Copy the list above into your race tool (e.g. duckrace-game.com), run it, then paste the "
            "results back in this channel — I'll catch them automatically, even if Discord splits a long "
            "paste across a few messages."
        ),
    )


# ---------- idle trash talk ----------

IDLE_TAUNT_SECONDS = 60 * 60  # an hour of silence before the bot starts talking shit

IDLE_TAUNTS = [
    "🦗 Crickets. **{remaining}** {unit}(s) still open and y'all are just sitting there.",
    "It's been an hour. Did everyone get lost or did you all just go broke?",
    "**{remaining}** {unit}(s) left. I've seen faster action at a funeral.",
    "This race has less movement than a parked car in a junkyard. Somebody call a spot.",
    "Y'all scared of a little duck? **{remaining}** {unit}(s) open.",
    "An hour with no calls. My hopes and dreams are dying right alongside this thread.",
    "Not one of you can type X and a number? That's literally two characters.",
    "I'm a bot with no legs and even I'd move faster than this chat.",
    "Hello?? **{remaining}** {unit}(s) open. I'm not getting paid to sit here, and neither are you.",
    "The ducks are getting restless and the humans are getting lazy. Call a spot.",
    "Is this thing on? Type `X1`. I'm begging.",
    "If this race fills before sunrise I'll eat my own code.",
    "You miss 100% of the spots you don't call. Wayne Gretzky said that. Probably.",
    "Dead thread energy. Somebody poke it with an X.",
    "Y'all out here with the confidence of a wet paper bag. Call. A. Spot.",
    "**{remaining}** {unit}(s) open and not a single call in an hour. Embarrassing for everybody involved.",
    "I could've run three races in the time you've spent staring at this screen.",
    "Somebody check on the group chat, I think everyone fell asleep face-first in their whiskey.",
    "Spots don't claim themselves, geniuses. **{remaining}** left.",
    "An hour of silence. Even the duck is judging you.",
]

_last_taunt_index: int | None = None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _pick_taunt(remaining: int, unit: str) -> str:
    global _last_taunt_index
    choices = [i for i in range(len(IDLE_TAUNTS)) if i != _last_taunt_index]
    idx = random.choice(choices)
    _last_taunt_index = idx
    return IDLE_TAUNTS[idx].format(remaining=remaining, unit=unit)


@tasks.loop(minutes=5)
async def idle_taunt_loop():
    now = datetime.now(timezone.utc)
    for key, snapshot in list(_load_all().items()):
        try:
            if snapshot.get("status") != "open" or snapshot.get("sat_active"):
                continue

            # Most recent sign of life: last spot call, else when calls opened,
            # else when the race was created. A taunt also resets the clock.
            stamps = []
            if snapshot.get("entries"):
                stamps.append(_parse_iso(snapshot["entries"][-1].get("at")))
            stamps.append(_parse_iso(snapshot.get("spot_calls_open_at")))
            stamps.append(_parse_iso(snapshot.get("created_at")))
            stamps.append(_parse_iso(snapshot.get("last_taunt_at")))
            stamps = [s for s in stamps if s]
            if not stamps:
                continue
            if (now - max(stamps)).total_seconds() < IDLE_TAUNT_SECONDS:
                continue

            channel = bot.get_channel(int(key))
            if channel is None:
                try:
                    channel = await bot.fetch_channel(int(key))
                except (discord.NotFound, discord.Forbidden):
                    continue
            if isinstance(channel, discord.Thread) and (channel.archived or channel.locked):
                continue

            race = get_race(int(key))
            if not race or race["status"] != "open":
                continue
            remaining = remaining_spots(race)
            if remaining <= 0:
                continue

            await channel.send(_pick_taunt(remaining, unit_label(race)))
            race["last_taunt_at"] = now.isoformat()
            save_race(int(key), race)
        except Exception:
            log.exception("Idle taunt failed for %s", key)


@idle_taunt_loop.before_loop
async def _before_idle_taunt_loop():
    await bot.wait_until_ready()


# ---------- bot lifecycle ----------

@bot.event
async def setup_hook():
    # Re-attach the persistent Edit-notes button so it keeps working on
    # notes messages posted before a restart.
    bot.add_view(NotesEditView())
    if not idle_taunt_loop.is_running():
        idle_taunt_loop.start()


@bot.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id if bot.user else "?")
    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash command(s)", len(synced))
    except Exception:
        log.exception("Failed to sync slash commands")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # x67/x69 are just real spot calls (67 and 69 spots respectively, clamped
    # to whatever's left) — handled by the normal SPOT_CALL_RE matching below,
    # with no extra roast/sticker response.

    channel_id = message.channel.id
    race = get_race(channel_id)
    content = message.content.strip()

    if race:
        if race["status"] == "open" and content.strip().lower() in ("yes sats", "no sats"):
            is_admin = (
                isinstance(message.author, discord.Member)
                and message.author.guild_permissions.administrator
            )
            is_host = str(message.author.id) == race["opened_by"]
            if is_host or is_admin:
                if content.strip().lower() == "yes sats":
                    if not race.get("sat_notified"):
                        remaining = remaining_spots(race)
                        fraction_full = (race["size"] - remaining) / race["size"]
                        await message.reply(
                            f"🚫 Can't open a satellite race yet — **{race['title']}** is only "
                            f"**{round(fraction_full * 100)}%** full. SATs unlock at "
                            f"**{round(SAT_THRESHOLD * 100)}%**.",
                            mention_author=False,
                        )
                    else:
                        await start_sat_race(message.channel, channel_id)
                else:
                    await message.channel.send("👍 No satellite race — carrying on as normal.")
            else:
                await message.reply(
                    "Only the host or an admin can decide on the satellite race.",
                    mention_author=False,
                )
            return

        if race["status"] == "open" and race.get("sat_active"):
            if content.strip().lower() == "xclose" or SPOT_CALL_RE.match(content):
                await message.reply(
                    "⏸️ Spot calls are paused here while the satellite race runs. "
                    "They'll reopen once that's finished.",
                    mention_author=False,
                )
                return
        elif race["status"] == "open":
            if content.strip().lower() == "xclose":
                remaining = remaining_spots(race)
                if remaining <= 0:
                    return
                await handle_spot_call(message, remaining)
                return

            match = SPOT_CALL_RE.match(content)
            if match:
                await handle_spot_call(message, int(match.group(1)))
                return

        if race["status"] in ("open", "pending", "closed", "shuffled", "complete") and content.strip().lower() == "xconfirm":
            error = await lock_race(message.channel, channel_id, str(message.author.id))
            if error:
                await message.reply(error, mention_author=False)
            return

        if race["status"] in ("shuffled", "complete") and content.strip().lower() == "xfinish":
            error = await archive_race(message.channel, channel_id)
            if error:
                await message.reply(error, mention_author=False)
            return

        sip_vouch_allowed = race["status"] in ("pending", "closed", "shuffled", "complete") or (
            race["status"] == "open" and race.get("sat_active")
        )

        if sip_vouch_allowed and content.lower() in CONFIRM_WORDS:
            await handle_confirm(message)
            return

        if sip_vouch_allowed:
            force_sip_match = FORCE_SIP_RE.match(content.strip())
            if force_sip_match:
                await handle_force_sip(message, force_sip_match.group(1))
                return

            vouch_match = VOUCH_RE.match(content.strip())
            if vouch_match:
                await handle_vouch(message, vouch_match.group(1))
                return

        if race["status"] == "shuffled" and _looks_like_results(content):
            _queue_result_fragment(message.channel, channel_id, str(message.author.id), content)
            return

        # A results paste already in progress from this author — keep buffering
        # even if a later fragment doesn't individually "look like" results.
        buf = _result_buffers.get(channel_id)
        if race["status"] == "shuffled" and buf and buf["author_id"] == str(message.author.id):
            _queue_result_fragment(message.channel, channel_id, str(message.author.id), content)
            return

    if content.lower() == "!rig":
        await message.channel.send(
            "Only thing rigged was the sex swing I banged your wife on last night 💀"
        )
        return

    if content.strip().lower() == "1 on 13":
        line = random.choice(ONE_ON_13_ROASTS).format(name=message.author.mention)
        await message.channel.send(line)
        return

    if content.strip().lower() == "retract":
        await message.channel.send("I hope you die a slow and painful death. Preferably by butt stuff.")
        return

    if content.strip().lower() == "!retire":
        line = random.choice(RETIRE_ROASTS).format(name=message.author.mention)
        await message.channel.send(line)
        return

    if content.lower() == "!race":
        is_admin = (
            isinstance(message.author, discord.Member)
            and message.author.guild_permissions.administrator
        )
        await run_race_shuffle(
            message.channel, channel_id, str(message.author.id), is_admin
        )
        return

    if content.lower() == "!status":
        if not race:
            await message.channel.send("No race is set up in this channel.")
        else:
            await message.channel.send(build_status_text(race))
        return

    await bot.process_commands(message)


# ---------- /duck and /create command groups ----------

duck_group = app_commands.Group(name="duck", description="Run a duck race")
create_group = app_commands.Group(name="create", description="Create a new duck race")


async def _run_spot_call_countdown(thread: discord.abc.Messageable, seconds: int):
    """Post a countdown message and live-edit it once a second until spot calls open."""
    countdown_msg = await thread.send(f"⏳ Spot calls open in **{seconds}s**...")
    for remaining in range(seconds - 1, -1, -1):
        await asyncio.sleep(1)
        try:
            if remaining > 0:
                await countdown_msg.edit(content=f"⏳ Spot calls open in **{remaining}s**...")
            else:
                await countdown_msg.edit(content="🟢 **Spot calls are open!** Call your spots now.")
        except discord.HTTPException:
            pass


# ---------- host notes (editable via button or /duck notes) ----------

NOTES_MAX_LENGTH = 1800


def _notes_text(race: dict, fallback_author: str) -> str:
    author = race.get("notes_author") or fallback_author
    return f"📋 **Host notes ({author}):** {race['notes']}"


def _can_edit_notes(interaction: discord.Interaction, race: dict) -> bool:
    is_admin = (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )
    return is_admin or str(interaction.user.id) == race["opened_by"]


async def _post_notes(thread: discord.abc.Messageable, race: dict, notes: str, author: str):
    """Posts the host-notes message with its Edit button and records it on the race."""
    race["notes"] = notes
    race["notes_author"] = author
    notes_message = await thread.send(_notes_text(race, author), view=NotesEditView())
    race["notes_message_id"] = str(notes_message.id)


async def update_race_notes(interaction: discord.Interaction, race: dict, notes: str):
    """Edits the existing notes message in place, or posts a fresh one if it's gone.
    Responds to the interaction either way."""
    race["notes"] = notes
    new_text = _notes_text(race, interaction.user.display_name)

    edited = False
    notes_message_id = race.get("notes_message_id")
    if notes_message_id and isinstance(interaction.channel, (discord.Thread, discord.TextChannel)):
        try:
            existing = await interaction.channel.fetch_message(int(notes_message_id))
            await existing.edit(content=new_text, view=NotesEditView())
            edited = True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            edited = False

    await interaction.response.send_message("✅ Notes updated.", ephemeral=True)
    if not edited:
        # Original notes message is gone or was never posted — post a fresh one
        # and track that as the editable notes message going forward.
        await _post_notes(interaction.channel, race, notes, race.get("notes_author") or interaction.user.display_name)
    save_race(interaction.channel_id, race)


class NotesModal(discord.ui.Modal, title="Edit host notes"):
    def __init__(self, current: str):
        super().__init__()
        self.notes_input = discord.ui.TextInput(
            label="Notes",
            style=discord.TextStyle.paragraph,
            default=current[:NOTES_MAX_LENGTH],
            max_length=NOTES_MAX_LENGTH,
        )
        self.add_item(self.notes_input)

    async def on_submit(self, interaction: discord.Interaction):
        race = get_race(interaction.channel_id)
        if not race:
            await interaction.response.send_message("No race is set up in this channel.", ephemeral=True)
            return
        notes = self.notes_input.value.strip()
        if not notes:
            await interaction.response.send_message("Notes can't be blank.", ephemeral=True)
            return
        await update_race_notes(interaction, race, notes)


class NotesEditView(discord.ui.View):
    """Persistent Edit button under the host-notes message. The race is looked up
    by channel, so one fixed custom_id works for every thread and survives restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Edit notes", emoji="✏️", style=discord.ButtonStyle.secondary,
                       custom_id="duckbot:edit_notes")
    async def edit_notes(self, interaction: discord.Interaction, button: discord.ui.Button):
        race = get_race(interaction.channel_id)
        if not race:
            await interaction.response.send_message("No race is set up in this channel.", ephemeral=True)
            return
        if not _can_edit_notes(interaction, race):
            await interaction.response.send_message(
                "Only the host who opened this race, or an admin, can edit the notes.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(NotesModal(race.get("notes") or ""))


@create_group.command(name="race", description="Open a new duck race in this channel")
@app_commands.describe(
    code="Short race code, e.g. ER13",
    total_spots="Number of spots in the race",
    image="Image to post with the race announcement (required)",
    notes="Notes posted below the picture in the thread, e.g. anything the picture doesn't show (required)",
    ticket_name="What to call each spot, e.g. 'ticket' or 'duck' (defaults to 'spot')",
)
async def duck_open(
    interaction: discord.Interaction,
    code: str,
    total_spots: int,
    image: discord.Attachment,
    notes: str,
    ticket_name: str | None = None,
):
    if not interaction.guild_id:
        await interaction.response.send_message("This only works in a server.", ephemeral=True)
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "Run `/create race` from a regular text channel — I'll create a thread there for the race.",
            ephemeral=True,
        )
        return
    if total_spots < 1 or total_spots > MAX_RACE_SIZE:
        await interaction.response.send_message(
            f"total_spots must be between 1 and {MAX_RACE_SIZE}.", ephemeral=True
        )
        return
    if not image.content_type or not image.content_type.startswith("image/"):
        await interaction.response.send_message(
            "That attachment doesn't look like an image — please attach a picture.", ephemeral=True
        )
        return
    if not notes.strip():
        await interaction.response.send_message(
            "Notes can't be blank — add a line about anything the picture doesn't show.",
            ephemeral=True,
        )
        return

    # Discord expects an ack within 3 seconds; downloading the image and
    # posting the announcement + thread can take longer than that, so defer
    # now and follow up once everything's actually created.
    await interaction.response.defer()

    race = new_race(
        code.strip(),
        total_spots,
        str(interaction.user.id),
        ticket_label=ticket_name.strip() if ticket_name else "spot",
        image_url=image.url,
    )

    unit = unit_label(race)
    image_file = await image.to_file()

    # Post the announcement (with image) as a normal channel message, then spin
    # a thread off of it — this is what makes the image show up in the channel
    # feed itself, not just inside the thread once you click in.
    announcement = await interaction.channel.send(
        f"🦆 **{race['title']} is open!** {total_spots} {unit}(s) available.\n"
        f"Hosted by {interaction.user.mention}.\n"
        f"Call {unit}s by typing **X**+number, e.g. `X2` or `X3`.",
        file=image_file,
    )

    try:
        thread = await announcement.create_thread(
            name=f"[OPEN] - {race['code']}",
            auto_archive_duration=1440,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to create threads here — the bot needs the "
            "**Create Public Threads** permission in this channel.",
            ephemeral=True,
        )
        return

    if notes and notes.strip():
        await _post_notes(thread, race, notes.strip(), interaction.user.display_name)

    save_race(thread.id, race)

    open_at = datetime.now(timezone.utc) + timedelta(seconds=SPOT_CALL_DELAY_SECONDS)
    race["spot_calls_open_at"] = open_at.isoformat()
    save_race(thread.id, race)

    await interaction.followup.send(f"Race thread created: {thread.mention}")

    asyncio.create_task(_run_spot_call_countdown(thread, SPOT_CALL_DELAY_SECONDS))


@bot.tree.command(name="racesat", description="Open a satellite race thread linked to this race thread")
@app_commands.describe(
    code="Short race code for the satellite race, e.g. ER13-SAT",
    total_spots="How many spots to make available in the satellite race",
    image="Image to post with the satellite race announcement (required)",
    notes="Notes posted below the picture in the satellite thread (required)",
    ticket_name="What to call each spot, e.g. 'ticket' or 'duck' (defaults to 'spot')",
)
async def duck_open_sat(
    interaction: discord.Interaction,
    code: str,
    total_spots: int,
    image: discord.Attachment,
    notes: str,
    ticket_name: str | None = None,
):
    if not interaction.guild_id:
        await interaction.response.send_message("This only works in a server.", ephemeral=True)
        return
    if not isinstance(interaction.channel, discord.Thread) or not isinstance(
        interaction.channel.parent, discord.TextChannel
    ):
        await interaction.response.send_message(
            "Run `/racesat` from inside the main race's thread.", ephemeral=True
        )
        return

    main_channel = interaction.channel
    main_race = get_race(main_channel.id)
    if not main_race:
        await interaction.response.send_message(
            "There's no race set up in this thread.", ephemeral=True
        )
        return
    if str(interaction.user.id) != main_race["opened_by"] and not (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    ):
        await interaction.response.send_message(
            "Only the host who opened this race, or an admin, can open a satellite race.",
            ephemeral=True,
        )
        return
    if not main_race.get("sat_active"):
        await interaction.response.send_message(
            "Satellite mode isn't on for this race yet — reply `yes sats` here first.",
            ephemeral=True,
        )
        return
    if main_race.get("sat_thread_id"):
        await interaction.response.send_message(
            f"A satellite race is already open: <#{main_race['sat_thread_id']}>.",
            ephemeral=True,
        )
        return
    if total_spots < 1 or total_spots > MAX_RACE_SIZE:
        await interaction.response.send_message(
            f"total_spots must be between 1 and {MAX_RACE_SIZE}.", ephemeral=True
        )
        return
    if not image.content_type or not image.content_type.startswith("image/"):
        await interaction.response.send_message(
            "That attachment doesn't look like an image — please attach a picture.", ephemeral=True
        )
        return
    if not notes.strip():
        await interaction.response.send_message(
            "Notes can't be blank — add a line about anything the picture doesn't show.",
            ephemeral=True,
        )
        return

    # Discord expects an ack within 3 seconds; downloading the image and
    # posting the announcement + thread can take longer than that, so defer
    # now and follow up once everything's actually created.
    await interaction.response.defer()

    sat_race = new_race(
        code.strip(),
        total_spots,
        main_race["opened_by"],
        ticket_label=ticket_name.strip() if ticket_name else "spot",
        image_url=image.url,
    )
    sat_race["is_sat"] = True
    sat_race["sat_parent_id"] = str(main_channel.id)

    unit = unit_label(sat_race)
    image_file = await image.to_file()
    parent_channel = interaction.channel.parent

    announcement = await parent_channel.send(
        f"🛟 **Satellite race {sat_race['title']} is open!** {total_spots} {unit}(s) available — "
        f"top finishers move into **{main_race['title']}**.\n"
        f"Hosted by {interaction.user.mention}.\n"
        f"Call {unit}s by typing **X**+number, e.g. `X2` or `X3`.",
        file=image_file,
    )

    try:
        thread = await announcement.create_thread(
            name=f"[OPEN] - SAT-{sat_race['code']}",
            auto_archive_duration=1440,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to create threads here — the bot needs the "
            "**Create Public Threads** permission in this channel.",
            ephemeral=True,
        )
        return

    if notes and notes.strip():
        await _post_notes(thread, sat_race, notes.strip(), interaction.user.display_name)

    save_race(thread.id, sat_race)

    open_at = datetime.now(timezone.utc) + timedelta(seconds=SPOT_CALL_DELAY_SECONDS)
    sat_race["spot_calls_open_at"] = open_at.isoformat()
    save_race(thread.id, sat_race)

    main_race["sat_thread_id"] = str(thread.id)
    save_race(main_channel.id, main_race)

    await interaction.followup.send(f"Satellite race thread created: {thread.mention}")

    asyncio.create_task(_run_spot_call_countdown(thread, SPOT_CALL_DELAY_SECONDS))


LAST_ONE_LEFT_ROASTS = [
    "⏳ Everyone is waiting on you, {name}. Are you broke? 💀",
    "💀 Everybody is waiting on you, {name}. Blink twice if your bank account needs CPR.",
    "🥃 {name} has been promoted to Official Bottle Blocker. Congratulations, cheapass. 😂",
    "⏳ {name}, we're not waiting anymore. We're just documenting your poverty at this point. 💀",
    "💸 The whiskey isn't expensive, {name}. Your financial situation is just dramatic. 😂",
    "🚨 {name}: 0 spots remaining. 0 excuses remaining. Wallet status: classified. 💀",
]

WAITING_ON_HOST_ROASTS = [
    "Jesus I appreciate you hosting {name}, but at this point you're holding my wallet hostage. What is you're malfunction 💀",
]

RETIRE_ROASTS = [
    "Retire? What are you, a fucking quitter? Get your ass back in the game.",
    "Oh, you're retiring? That's adorable. Sit down, pour another one, and quit being a bitch.",
    "Nobody likes a quitter, {name}. Your retirement request has been DENIED. Get back out there.",
    "Trying to retire already? We didn't come here to watch you develop self-preservation. Get back in.",
    "Retirement? Absolutely fucking not. We need another victim. Grab your glass and report for duty.",
    "You can't retire, {name}. The whiskey hasn't finished ruining your life yet. Get back in there.",
    "Oh no, {name} wants to quit. Somebody hide his pension and pour him another drink.",
    "Retirement is for people who have made good decisions. Clearly, you joined this group. Get back in.",
    "You call it retirement. We call it abandoning your fucking brothers. Pour another and get back in line.",
    "{name} has requested retirement. Unfortunately, the committee has unanimously voted: Fuck you. Keep drinking.",
]

ALREADY_SHUFFLED_ROASTS = [
    "🙄 Race **{code}** was already shuffled, dumbass. Chill the fuck out.",
    "💀 We already ran **{code}**, genius. Reading comprehension really isn't your strong suit, is it?",
    "🚨 **{code}** is already shuffled. Relax, take a breath, maybe a nap.",
    "Bro **{code}** already got shuffled. You good? Chill the fuck out.",
    "The list for **{code}** has already been shuffled. Nobody needs your anxiety right now. Chill out.",
    "Congrats, you just tried to shuffle a race that's already done. Go touch grass and chill the fuck out.",
    "**{code}** already shuffled. This isn't a double-tap situation. Relax.",
    "We heard you the first time. **{code}** is shuffled. Chill the fuck out.",
]

ONE_ON_13_ROASTS = [
    "Fuck You and everybody You Love",
    "Spot calling isn't allowed, dumbass. Were you dropped on your fucking head?",
    "You just called a spot that doesn't exist. Impressive level of fucking stupidity.",
    "There are no spot calls, genius. Put the crayons down and read the rules.",
    "Congratulations, you played yourself. Spot calling isn't allowed, you fucking moron.",
    "Did you actually think that was a spot call? Jesus Christ, we're working with a limited gene pool.",
    "Nobody said you could call a spot, dumbass. Your reading comprehension is fucking spectacular.",
    "Wrong fucking button, Einstein. There are NO spot calls.",
    "You tried to call a spot anyway? That's some premium-grade stupidity right there.",
    "Spot calling is disabled, dipshit. Your brain apparently is too.",
    "What part of “NO SPOT CALLING” confused you, {name}? The “NO” or the “SPOT CALLING”?",
    "You just yelled “dibs” at a computer. Fucking phenomenal.",
    "Spot call denied. Common sense also appears to be unavailable.",
    "Bro really tried to call a spot like this is fucking kindergarten. Read the rules.",
    "There are no spot calls, {name}. But congratulations on publicly exposing yourself as a dumbass.",
    "The bot saw your spot call and briefly considered uninstalling itself.",
    "You can't call spots here, fucknut. This isn't musical chairs.",
    "Imagine seeing the rules and thinking, “Yeah, those probably don't apply to me.”",
    "Spot calling isn't allowed. But please, by all means, continue demonstrating your advanced stupidity.",
    "Denied. No spot calls. Your application for common sense has also been rejected.",
    "{name} tried to call a spot. The bot would like to remind you that reading is a valuable life skill.",
]


def waiting_on_host_message(race: dict) -> str:
    host_mention = f"<@{race['opened_by']}>"
    line = random.choice(WAITING_ON_HOST_ROASTS).format(name=host_mention)
    return f"✅ **Everyone's confirmed for {race['title']}!** 🏁\n{line}"


def build_status_text(race: dict) -> str:
    totals = entry_totals(race)
    names = participant_names(race)
    remaining = remaining_spots(race)
    unit = unit_label(race)
    lines = [
        f"**{race['title']}** — status: **{race['status']}** — "
        f"{remaining}/{race['size']} {unit}(s) remaining — hosted by <@{race['opened_by']}>"
    ]
    if race.get("sat_active"):
        if race.get("sat_thread_id"):
            lines.append(
                f"🛟 **Satellite race in progress** — <#{race['sat_thread_id']}>. "
                f"Spot calls here are paused until it finishes."
            )
        else:
            lines.append(
                "🛟 **Satellite mode is ON** — host still needs to run `/racesat` to open the thread."
            )
    if totals:
        lines.append("")
        vouched = race.get("vouched", [])
        for uid, count in totals.items():
            if uid in race["confirmed"]:
                mark = "✅"
            elif uid in vouched:
                mark = "🤝"
            else:
                mark = "⏳"
            lines.append(f"{mark} {names[uid]} — {count} {unit}(s)")

        unconfirmed = [uid for uid in totals if uid not in race["confirmed"]]
        if unconfirmed and race["status"] == "pending":
            pending_mentions = ", ".join(f"<@{uid}>" for uid in unconfirmed)
            lines.append("")
            if len(unconfirmed) == 1:
                roast = random.choice(LAST_ONE_LEFT_ROASTS).format(name=pending_mentions)
                lines.append(roast)
            else:
                lines.append(f"⏳ Still waiting on: {pending_mentions}")
    return "\n".join(lines)


async def lock_race(channel: discord.abc.Messageable, channel_id: int, user_id: str) -> str | None:
    """xconfirm. Before the list is built (open/pending), this locks entries so
    /duck race can shuffle. Once the list's already been built (closed/shuffled/
    complete), it instead acts as the host's official declaration that all sips
    are in — a status update, not a re-lock. Only the host who ran /create race
    can do either. Returns an error string, or None on success (in which case
    the announcement has already been sent to the channel)."""
    race = get_race(channel_id)
    if not race:
        return "No race is set up in this channel."
    if user_id != race["opened_by"]:
        return "Only the host who opened this race can xconfirm."

    totals = entry_totals(race)
    unconfirmed = [uid for uid in totals if uid not in race["confirmed"]]

    if race["status"] in ("closed", "shuffled", "complete"):
        note = ""
        if unconfirmed:
            pending_mentions = ", ".join(f"<@{uid}>" for uid in unconfirmed)
            note = f"\n⚠️ Still not sipped: {pending_mentions}"
        await channel.send(
            f"✅ **Host officially confirms: all sips received for {race['title']}!** 🏁{note}"
        )
        return None

    if race["status"] not in ("open", "pending"):
        return f"Can't xconfirm a race that's already **{race['status']}**."

    race["status"] = "closed"
    save_race(channel_id, race)
    await rename_channel(channel, "PENDING", race["code"])

    note = ""
    if unconfirmed:
        pending_mentions = ", ".join(f"<@{uid}>" for uid in unconfirmed)
        note = f"\n⚠️ Locked with unconfirmed entries: {pending_mentions}"

    await channel.send(
        f"🔒 **Entries locked for {race['title']}!** 🏁{note}\n"
        f"Type `!race` when you want the entry list and shuffle."
    )
    return None


async def archive_race(channel: discord.abc.Messageable, channel_id: int) -> str | None:
    """Marks the race fully done after the mods have actually run it externally,
    and renames the channel to [CLOSED]. Returns an error string, or None on success."""
    race = get_race(channel_id)
    if not race:
        return "No race is set up in this channel."
    if race["status"] not in ("shuffled", "complete"):
        return "Can't close this out until the race has been shuffled with `!race`."

    totals = entry_totals(race)
    unconfirmed = [uid for uid in totals if uid not in race["confirmed"]]
    if unconfirmed:
        pending_mentions = ", ".join(f"<@{uid}>" for uid in unconfirmed)
        await channel.send(
            f"🚫 Can't finish yet — still need an actual sip from: {pending_mentions}"
        )
        return None

    await rename_channel(channel, "CLOSED", race["code"])
    await channel.send(
        f"✅ **{race['title']} is closed.** Thanks everyone! 🦆\n"
        f"🔒 This thread is now locked — only duck mods can post here from now on."
    )

    if isinstance(channel, discord.Thread):
        try:
            await channel.edit(locked=True, archived=True)
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Couldn't lock thread %s after closing", channel.id)

    if race.get("is_sat") and race.get("sat_parent_id"):
        await _resume_main_after_sat(race["sat_parent_id"])

    return None


async def _resume_main_after_sat(main_channel_id_str: str):
    """Called once a satellite race is closed out with xfinish — unpauses the
    main race's spot calls and pings its thread."""
    try:
        main_channel_id = int(main_channel_id_str)
    except (TypeError, ValueError):
        return

    main_race = get_race(main_channel_id)
    if not main_race or not main_race.get("sat_active"):
        return  # already resumed (e.g. the SAT results paste already did it)

    main_race["sat_active"] = False
    main_race["sat_thread_id"] = None
    save_race(main_channel_id, main_race)

    try:
        main_channel = bot.get_channel(main_channel_id) or await bot.fetch_channel(main_channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    remaining = remaining_spots(main_race)
    unit = unit_label(main_race)
    if remaining > 0:
        await main_channel.send(
            f"✅ Satellite race finished — spot calls are back on here with **{remaining}** "
            f"{unit}(s) still open. Call them with `X#`."
        )
    else:
        await main_channel.send("✅ Satellite race finished — this race is already full.")


@duck_group.command(name="reset", description="Clear the race in this channel")
@app_commands.checks.has_permissions(administrator=True)
async def duck_reset(interaction: discord.Interaction):
    race = get_race(interaction.channel_id)
    if not race:
        await interaction.response.send_message("No race to reset here.", ephemeral=True)
        return
    save_race(interaction.channel_id, None)
    _result_buffers.pop(interaction.channel_id, None)
    await interaction.response.send_message(f"Race {race['code']} cleared. Channel ready for `/create race`.")


@duck_group.command(name="notes", description="Edit the host notes for the race in this thread")
@app_commands.describe(notes="The new host notes text — replaces whatever was posted before")
async def duck_notes(interaction: discord.Interaction, notes: str):
    race = get_race(interaction.channel_id)
    if not race:
        await interaction.response.send_message("No race is set up in this channel.", ephemeral=True)
        return

    if not _can_edit_notes(interaction, race):
        await interaction.response.send_message(
            "Only the host who opened this race, or an admin, can edit the notes.",
            ephemeral=True,
        )
        return

    if not notes.strip():
        await interaction.response.send_message("Notes can't be blank.", ephemeral=True)
        return

    await update_race_notes(interaction, race, notes.strip()[:NOTES_MAX_LENGTH])


bot.tree.add_command(duck_group)
bot.tree.add_command(create_group)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = "You don't have permission to use that command here."
    else:
        msg = f"Something went wrong running that command: `{error}`"
        log.exception("Slash command error", exc_info=error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


def main():
    if not TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and fill in your bot token."
        )
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
