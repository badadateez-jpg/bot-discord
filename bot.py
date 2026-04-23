import asyncio
import json
import logging
import os
import random
import re
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands

try:
    import yt_dlp  # type: ignore
except ImportError:
    yt_dlp = None

# ---------------- LOGGING ---------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("kyoren")

# ---------------- CONFIG ---------------- #
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = "data.json"
MAX_TIMEOUT = timedelta(days=28)
PINK = discord.Color.from_rgb(255, 105, 180)
RED = discord.Color.red()
GREEN = discord.Color.green()

# ---------------- INTENTS ---------------- #
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ---------------- PERSISTANCE ---------------- #

def _default_guild_data():
    return {
        "log_channel_id": None,
        "welcome_channel_id": None,
        # warns : uid -> [ {reason, moderator_id, timestamp} ]
        "warns": {},
        # sanctions : uid -> [ {type, reason, moderator_id, timestamp, duration?} ]
        "sanctions": {},
        # totals : action -> uid -> int
        "action_totals": {"kiss": {}, "hug": {}, "slap": {}},
    }


def load_data():
    if not os.path.exists(DATA_FILE):
        return {"guilds": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:
        log.error("Erreur lors du chargement de %s : %s", DATA_FILE, exc)
        return {"guilds": {}}
    if "guilds" not in raw:
        # Ancien format détecté : on archive sans écraser, l'utilisateur peut migrer à la main.
        log.warning(
            "Ancien format de data.json détecté. Les warns/sanctions existants sont "
            "conservés sous la clé `_legacy`. Pour les réattribuer à un serveur, édite "
            "data.json et place-les sous `guilds.<guild_id>`."
        )
        return {"guilds": {}, "_legacy": raw}
    return raw


data = load_data()
_save_lock = asyncio.Lock()


def _save_data_sync():
    tmp = f"{DATA_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


async def save_data():
    async with _save_lock:
        await asyncio.to_thread(_save_data_sync)


def gdata(guild_id) -> dict:
    """Retourne (en le créant si besoin) le dict persistant pour un guild_id."""
    gid = str(guild_id)
    if gid not in data["guilds"]:
        data["guilds"][gid] = _default_guild_data()
    # s'assurer que les clés manquantes (après mise à jour) existent
    base = _default_guild_data()
    for k, v in base.items():
        data["guilds"][gid].setdefault(k, v)
    for k in base["action_totals"]:
        data["guilds"][gid]["action_totals"].setdefault(k, {})
    return data["guilds"][gid]


# ---------------- COOLDOWNS (en mémoire) ---------------- #

# (guild_id, author_id, target_id, action) -> datetime d'expiration
action_cooldowns: dict[tuple[int, int, int, str], datetime] = {}


def check_action_cooldown(guild_id, author_id, target_id, action_name,
                          duration=timedelta(hours=2)) -> Optional[timedelta]:
    now = datetime.now(timezone.utc)
    # nettoyage léger
    for key in list(action_cooldowns):
        if action_cooldowns[key] <= now:
            del action_cooldowns[key]
    key = (guild_id, author_id, target_id, action_name)
    expires = action_cooldowns.get(key)
    if expires and expires > now:
        return expires - now
    action_cooldowns[key] = now + duration
    return None


# ---------------- HELPERS EMBEDS ---------------- #

def embed_err(title, desc):
    return discord.Embed(title=title, description=desc, color=RED)


def embed_pink(title=None, desc=None):
    return discord.Embed(title=title, description=desc, color=PINK)


def embed_ok(title, desc):
    return discord.Embed(title=title, description=desc, color=GREEN)


def e(title, desc):  # rétrocompat nom court
    return embed_err(title, desc)


# ---------------- PARSING / FORMATS ---------------- #

def parse_duration(s: str) -> Optional[timedelta]:
    if not s:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", s)
    if not m:
        return None
    v, u = int(m[1]), m[2]
    return {
        "s": timedelta(seconds=v),
        "m": timedelta(minutes=v),
        "h": timedelta(hours=v),
        "d": timedelta(days=v),
    }[u]


def format_remaining(td: timedelta) -> str:
    total = max(int(td.total_seconds()), 0)
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days > 0:
        return f"{days}j {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def format_secs(seconds: int) -> str:
    if seconds is None or seconds <= 0:
        return "?:??"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ---------------- DECORATORS / CHECKS ---------------- #

def is_admin():
    async def predicate(ctx: commands.Context):
        if ctx.guild is None:
            return False
        p = ctx.author.guild_permissions
        return p.administrator or p.manage_guild
    return commands.check(predicate)


async def hierarchy_check(ctx: commands.Context, target: discord.Member) -> Optional[str]:
    """Retourne un message d'erreur si la sanction n'est pas possible, sinon None."""
    if target is None:
        return "Membre introuvable."
    if target == ctx.author:
        return "Tu ne peux pas te sanctionner toi-même."
    if target == ctx.guild.owner:
        return "Impossible de sanctionner le propriétaire du serveur."
    if target == ctx.guild.me:
        return "Je ne peux pas me sanctionner moi-même."
    if ctx.author != ctx.guild.owner and target.top_role >= ctx.author.top_role:
        return "Tu ne peux pas sanctionner un membre de rang supérieur ou égal au tien."
    if target.top_role >= ctx.guild.me.top_role:
        return "Je ne peux pas sanctionner ce membre : son rôle est au-dessus ou égal au mien."
    return None


# ---------------- LOGS (salon) ---------------- #

async def guild_log(guild: discord.Guild, title: str, desc: str):
    if guild is None:
        return
    gd = gdata(guild.id)
    ch_id = gd.get("log_channel_id")
    if not ch_id:
        return
    ch = guild.get_channel(ch_id)
    if ch is None:
        try:
            ch = await guild.fetch_channel(ch_id)
        except Exception as exc:
            log.warning("Impossible de récupérer le salon logs %s : %s", ch_id, exc)
            return
    try:
        await ch.send(embed=embed_err(title, desc[:4000]))
    except Exception as exc:
        log.warning("Impossible d'envoyer dans le salon logs : %s", exc)


# ---------------- WARN SYSTEM ---------------- #

async def add_warn(guild: discord.Guild, moderator: discord.Member,
                   member: discord.Member, reason: str):
    gd = gdata(guild.id)
    uid = str(member.id)
    gd["warns"].setdefault(uid, [])
    gd["sanctions"].setdefault(uid, [])
    now = datetime.now(timezone.utc).isoformat()
    entry = {"reason": reason, "moderator_id": moderator.id, "timestamp": now}
    gd["warns"][uid].append(entry)
    gd["sanctions"][uid].append({
        "type": "warn", "reason": reason,
        "moderator_id": moderator.id, "timestamp": now,
    })
    await save_data()

    count = len(gd["warns"][uid])
    try:
        await member.send(embed=embed_err("⚠ Warn reçu", f"Raison : {reason}"))
    except Exception:
        pass

    # Escalade automatique — protégée par try/except pour éviter de casser la sanction.
    try:
        if count == 3:
            await member.timeout(timedelta(hours=1), reason="3 warns")
            await guild_log(guild, "⏱ Timeout auto", f"{member} → 1h (3 warns)")
        elif count == 5:
            await member.timeout(timedelta(hours=72), reason="5 warns")
            await guild_log(guild, "⏱ Timeout auto", f"{member} → 72h (5 warns)")
        elif count >= 8:
            await member.ban(reason="8 warns")
            await guild_log(guild, "⛔ Ban auto", f"{member} (8 warns)")
    except discord.Forbidden:
        await guild_log(guild, "⚠ Escalade impossible",
                        f"Permissions insuffisantes pour escalader {member}.")
    except Exception as exc:
        log.error("Erreur escalade warns : %s", exc)


# ---------------- RULES VIEW ---------------- #

MODERATION_REASON_OPTIONS = [
    ("Propos raciste", "⛔"),
    ("Spam / Flood", "📛"),
    ("Injures répétitives", "🤬"),
    ("Harcèlement", "🚫"),
    ("Menaces", "⚠️"),
    ("Contenu interdit", "🔞"),
    ("Publicité non autorisée", "📣"),
    ("Contournement de sanction", "🔁"),
    ("Autre...", "✏️"),
]


class RulesView(discord.ui.View):
    def __init__(self, role_id: int):
        super().__init__(timeout=None)
        self.role_id = role_id

    @discord.ui.button(label="Accepter le règlement",
                       style=discord.ButtonStyle.success,
                       custom_id="accept_rules_button")
    async def accept_rules(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Cette action doit être utilisée dans le serveur.", ephemeral=True)
            return
        role = interaction.guild.get_role(self.role_id)
        if role is None:
            await interaction.response.send_message(
                "Le rôle associé au règlement est introuvable.", ephemeral=True)
            return
        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            await interaction.response.send_message(
                "Impossible de te retrouver sur le serveur.", ephemeral=True)
            return
        if role in member.roles:
            await interaction.response.send_message(
                f"Tu as déjà le rôle {role.mention}.", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason="Règlement accepté")
        except discord.Forbidden:
            await interaction.response.send_message(
                "Je n'ai pas la permission de te donner ce rôle.", ephemeral=True)
            return
        except Exception as exc:
            await interaction.response.send_message(
                f"Erreur lors de l'attribution du rôle : {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Merci, tu as bien accepté le règlement. Tu as reçu le rôle {role.mention}.",
            ephemeral=True)


# ---------------- MODERATION UI ---------------- #

async def execute_moderation_action(guild, moderator, target_member, action_name,
                                    reason, duration=None):
    gd = gdata(guild.id)
    now_iso = datetime.now(timezone.utc).isoformat()

    if action_name == "ban":
        try:
            await target_member.send(embed=embed_err("⛔ Ban", f"Raison : {reason}"))
        except Exception:
            pass
        await target_member.ban(reason=reason)
        gd["sanctions"].setdefault(str(target_member.id), []).append({
            "type": "ban", "reason": reason,
            "moderator_id": moderator.id, "timestamp": now_iso,
        })
        await save_data()
        await guild_log(guild, "⛔ Ban", f"{target_member} | {reason}")
        return embed_err("Ban", f"{target_member.mention}\n{reason}")

    if action_name == "warn":
        await add_warn(guild, moderator, target_member, reason)
        return embed_err("⚠ Warn", f"{target_member.mention}\n{reason}")

    if action_name == "kick":
        try:
            await target_member.send(embed=embed_err("👢 Kick", f"Raison : {reason}"))
        except Exception:
            pass
        await target_member.kick(reason=reason)
        gd["sanctions"].setdefault(str(target_member.id), []).append({
            "type": "kick", "reason": reason,
            "moderator_id": moderator.id, "timestamp": now_iso,
        })
        await save_data()
        await guild_log(guild, "👢 Kick", f"{target_member} | {reason}")
        return embed_err("Kick", f"{target_member.mention}\n{reason}")

    if action_name in ("timeout", "mute"):
        if duration is None:
            raise ValueError("La durée du timeout est introuvable.")
        if duration > MAX_TIMEOUT:
            raise ValueError("Discord limite les timeouts à 28 jours maximum.")
        await target_member.timeout(duration, reason=reason)
        gd["sanctions"].setdefault(str(target_member.id), []).append({
            "type": action_name, "reason": reason,
            "moderator_id": moderator.id, "timestamp": now_iso,
            "duration_seconds": int(duration.total_seconds()),
        })
        await save_data()
        await guild_log(guild, "⏱ Timeout",
                        f"{target_member} | {format_remaining(duration)} | {reason}")
        title = "Mute" if action_name == "mute" else "Timeout"
        return embed_err(title, f"{target_member.mention}\n{format_remaining(duration)}\n{reason}")

    raise ValueError("Action de modération inconnue.")


class CustomReasonModal(discord.ui.Modal):
    def __init__(self, moderation_view):
        super().__init__(title="Autre raison")
        self.moderation_view = moderation_view
        self.reason_input = discord.ui.TextInput(
            label="Raison personnalisée",
            placeholder="Écris la raison ici",
            max_length=200,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.moderation_view.moderator.id:
            await interaction.response.send_message(
                "Seul le membre qui a lancé la commande peut choisir cette raison.",
                ephemeral=True)
            return
        await self.moderation_view.perform_action(interaction, str(self.reason_input))


class ModerationReasonSelect(discord.ui.Select):
    def __init__(self, moderation_view):
        self.moderation_view = moderation_view
        options = [discord.SelectOption(label=label, value=label, emoji=emoji)
                   for label, emoji in MODERATION_REASON_OPTIONS]
        super().__init__(placeholder="Choisis la raison de la sanction",
                         min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.moderation_view.moderator.id:
            await interaction.response.send_message(
                "Seul le membre qui a lancé la commande peut choisir cette raison.",
                ephemeral=True)
            return
        reason = self.values[0]
        if reason == "Autre...":
            await interaction.response.send_modal(CustomReasonModal(self.moderation_view))
            return
        await self.moderation_view.perform_action(interaction, reason)


class ModerationReasonView(discord.ui.View):
    def __init__(self, target_member, moderator, action_name, duration=None):
        super().__init__(timeout=120)
        self.target_member = target_member
        self.moderator = moderator
        self.action_name = action_name
        self.duration = duration
        self.message = None
        self.add_item(ModerationReasonSelect(self))

    async def perform_action(self, interaction: discord.Interaction, reason: str):
        try:
            result_embed = await execute_moderation_action(
                interaction.guild, self.moderator, self.target_member,
                self.action_name, reason, self.duration)
        except discord.Forbidden:
            await interaction.response.send_message(
                "Je n'ai pas la permission d'appliquer cette sanction.", ephemeral=True)
            return
        except Exception as exc:
            await interaction.response.send_message(
                f"Erreur lors de la sanction : {exc}", ephemeral=True)
            return

        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(embed=result_embed, view=None)
            except Exception:
                pass
        if interaction.response.is_done():
            await interaction.followup.send("Sanction appliquée.", ephemeral=True)
        else:
            await interaction.response.send_message("Sanction appliquée.", ephemeral=True)
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


# ==================================================================== #
#                              MUSIQUE                                 #
# ==================================================================== #

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "default_search": "ytsearch",
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "source_address": "0.0.0.0",
    "cookiefile": "cookies.txt",
}

FFMPEG_BEFORE = ("-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
                 "-nostdin -loglevel warning")
FFMPEG_OPTS = "-vn"


class MusicTrack:
    __slots__ = ("title", "stream_url", "webpage_url", "duration", "requester_id",
                 "requester_mention")

    def __init__(self, title, stream_url, webpage_url, duration,
                 requester_id, requester_mention):
        self.title = title
        self.stream_url = stream_url
        self.webpage_url = webpage_url
        self.duration = duration
        self.requester_id = requester_id
        self.requester_mention = requester_mention


async def extract_track(query: str, requester: discord.Member) -> MusicTrack:
    """Extraction yt-dlp non-bloquante, retourne la première entrée trouvée."""
    if yt_dlp is None:
        raise RuntimeError("yt-dlp n'est pas installé. Lance : pip install -U yt-dlp")

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
            if info is None:
                raise RuntimeError("Aucun résultat trouvé.")
            if "entries" in info:
                entries = [e for e in info["entries"] if e]
                if not entries:
                    raise RuntimeError("Aucun résultat trouvé.")
                info = entries[0]
            return info

    info = await asyncio.to_thread(_extract)
    return MusicTrack(
        title=info.get("title") or "Sans titre",
        stream_url=info["url"],
        webpage_url=info.get("webpage_url") or info.get("original_url") or query,
        duration=info.get("duration"),
        requester_id=requester.id,
        requester_mention=requester.mention,
    )


class GuildMusic:
    """État musical pour un serveur donné."""

    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: list[MusicTrack] = []
        self.current: Optional[MusicTrack] = None
        self.voice: Optional[discord.VoiceClient] = None
        self.text_channel: Optional[discord.abc.Messageable] = None
        self.next_event = asyncio.Event()
        self.player_task: Optional[asyncio.Task] = None

    def enqueue(self, track: MusicTrack):
        self.queue.append(track)

    def start(self):
        if self.player_task is None or self.player_task.done():
            self.player_task = asyncio.create_task(self._player_loop())

    def _after_play(self, error):
        if error:
            log.error("Erreur lecture musique : %s", error)
        # Doit être thread-safe : on pousse l'event sur la loop principale.
        try:
            bot.loop.call_soon_threadsafe(self.next_event.set)
        except Exception:
            pass

    async def _player_loop(self):
        try:
            while True:
                self.next_event.clear()
                if not self.queue:
                    # Attend 5 minutes, puis déconnexion auto si rien de nouveau.
                    try:
                        await asyncio.wait_for(self.next_event.wait(), timeout=300)
                    except asyncio.TimeoutError:
                        await self.disconnect()
                        return
                    continue

                track = self.queue.pop(0)
                self.current = track

                if self.voice is None or not self.voice.is_connected():
                    self.current = None
                    return

                try:
                    source = discord.FFmpegPCMAudio(
                        track.stream_url,
                        before_options=FFMPEG_BEFORE,
                        options=FFMPEG_OPTS,
                    )
                    self.voice.play(source, after=self._after_play)
                except Exception as exc:
                    log.error("Impossible de démarrer la lecture : %s", exc)
                    if self.text_channel:
                        try:
                            await self.text_channel.send(
                                embed=embed_err("🎵 Erreur lecture",
                                                f"Impossible de lire **{track.title}** : {exc}"))
                        except Exception:
                            pass
                    self.current = None
                    continue

                if self.text_channel:
                    dur = format_secs(track.duration) if track.duration else "?:??"
                    emb = embed_ok(
                        "🎵 En lecture",
                        f"**[{track.title}]({track.webpage_url})**\n"
                        f"Durée : `{dur}` — Demandé par {track.requester_mention}",
                    )
                    try:
                        await self.text_channel.send(embed=emb)
                    except Exception:
                        pass

                await self.next_event.wait()
                self.current = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Crash du player_loop : %s", exc)

    async def disconnect(self):
        self.queue.clear()
        self.current = None
        if self.voice is not None:
            try:
                if self.voice.is_playing() or self.voice.is_paused():
                    self.voice.stop()
                await self.voice.disconnect(force=False)
            except Exception:
                pass
        self.voice = None
        if self.player_task is not None and not self.player_task.done():
            self.player_task.cancel()
        self.player_task = None


# guild_id -> GuildMusic
_music_state: dict[int, GuildMusic] = {}


def get_music(guild: discord.Guild) -> GuildMusic:
    state = _music_state.get(guild.id)
    if state is None:
        state = GuildMusic(guild)
        _music_state[guild.id] = state
    return state


async def ensure_voice(ctx: commands.Context) -> Optional[GuildMusic]:
    """Connecte le bot au salon vocal de l'auteur, ou retourne None si impossible."""
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.send(embed=embed_err("Musique", "Rejoins d'abord un salon vocal."))
        return None
    channel = ctx.author.voice.channel
    state = get_music(ctx.guild)
    state.text_channel = ctx.channel
    if state.voice is None or not state.voice.is_connected():
        try:
            state.voice = await channel.connect(reconnect=True)
        except discord.ClientException:
            # Déjà connecté ailleurs : tente move
            if ctx.guild.voice_client is not None:
                try:
                    await ctx.guild.voice_client.move_to(channel)
                    state.voice = ctx.guild.voice_client
                except Exception as exc:
                    await ctx.send(embed=embed_err("Musique",
                                                  f"Impossible de rejoindre le vocal : {exc}"))
                    return None
            else:
                return None
        except Exception as exc:
            await ctx.send(embed=embed_err("Musique",
                                           f"Impossible de rejoindre le vocal : {exc}"))
            return None
    elif state.voice.channel != channel:
        try:
            await state.voice.move_to(channel)
        except Exception as exc:
            await ctx.send(embed=embed_err("Musique",
                                           f"Impossible de changer de salon : {exc}"))
            return None
    return state


# ==================================================================== #
#                              EVENTS                                  #
# ==================================================================== #

@bot.event
async def on_ready():
    log.info("BOT CONNECTÉ : %s (id=%s)", bot.user, bot.user.id)
    # Ré-enregistre la view persistante des règlements (sans connaître les role_ids précis,
    # Discord ré-appelle le callback sur n'importe quel button avec ce custom_id ; on ignore.)


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    gd = gdata(guild.id)
    ch_id = gd.get("welcome_channel_id") or (guild.system_channel.id if guild.system_channel else None)
    if ch_id is None:
        return
    ch = guild.get_channel(ch_id)
    if ch is None:
        return
    count = guild.member_count or len(guild.members)
    try:
        await ch.send(f"Bienvenue {member.mention}, tu es le {count}ᵉ membre !")
    except Exception as exc:
        log.warning("Impossible d'envoyer le message de bienvenue : %s", exc)


@bot.event
async def on_member_remove(member: discord.Member):
    await guild_log(member.guild, "👋 Leave", f"{member}")


@bot.event
async def on_message_delete(message: discord.Message):
    if message.guild is None or message.author.bot:
        return
    content = message.content or "(pas de contenu texte)"
    if len(content) > 1500:
        content = content[:1500] + "…"
    await guild_log(
        message.guild, "🗑 Message supprimé",
        f"Auteur : {message.author}\nSalon : {message.channel.mention}\nContenu : {content}",
    )


AUTO_REPLIES = {
    "salut": ["salut à toi, quoi de neuf ?", "saluttt, tu vas bien ?", "yooo salut !"],
    "yo": ["yooo", "yo ça dit quoi ?", "yoo, bien ou bien ?"],
    "coucou": ["coucouuu", "coucou toi !", "cc hehe"],
    "cc": ["cc", "cc ça va ?", "coucouuu"],
    "bonjour": ["bonjourrr", "bonjour, j'espère que tu vas bien", "salut salut !"],
    "wesh": ["weshhh", "wesh bien ou quoi ?", "weshh la forme ?"],
    "re": ["rebienvenuee", "reeee", "re toi"],
}


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    content = (message.content or "").lower().strip()

    if content in AUTO_REPLIES:
        try:
            await message.channel.send(random.choice(AUTO_REPLIES[content]))
        except Exception:
            pass
    elif re.search(r"(?:^|\s)quoi[?.!… ]*$", content):
        await message.channel.send("QUOICOUBEHH")
    elif re.search(r"(?:^|\s)hein[?.!… ]*$", content):
        await message.channel.send("APAGNANN")
    elif re.search(r"(?:^|\s)comment[?.!… ]*$", content):
        await message.channel.send("COMMANDANT DE BORDDD")

    if message.content.strip() == "!":
        await message.channel.send(embed=get_help_embed(message.author))
        return

    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send("Tu n'as pas les permissions d'utiliser cette commande.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Argument manquant. Utilise `!help` pour voir l'usage.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f"Argument invalide : {error}")
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Cette commande est en cooldown. Réessaye dans {error.retry_after:.1f} s.")
        return
    # Erreur inconnue : log stacktrace complète pour debug, message générique à l'utilisateur.
    log.error("Erreur dans la commande %s :", getattr(ctx.command, "name", "?"))
    traceback.print_exception(type(error), error, error.__traceback__)
    try:
        await ctx.send("Une erreur est survenue. Les devs ont été notifiés dans les logs.")
    except Exception:
        pass


# ==================================================================== #
#                              FUN / UTIL                              #
# ==================================================================== #

@bot.command()
async def ping(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong ! `{latency}ms`")


@bot.command()
@commands.cooldown(1, 10, commands.BucketType.user)
async def pp(ctx, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"Photo de profil de {member}", color=discord.Color.blue())
    embed.set_image(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="8ball")
async def ball(ctx, *, question: str):
    responses = [
        "Clairement, oui ! Tu as toutes tes chances de ton côté.",
        "Je pense que oui.",
        "Potentiellement...",
        "Je ne sais pas.",
        "Pas trop...",
        "Non mdrr",
        "Pas du tout, continue à rêver ailleurs !",
    ]
    await ctx.send(random.choice(responses))


# --- Kiss / Hug / Slap factorisé ---

ACTIONS = {
    "kiss": {
        "emoji": "❤️",
        "phrase": "{author} vous embrasse très fort {target} !",
        "label_plural": "kiss",
        "gifs": [
            "https://media.giphy.com/media/MQVpBqASxSlFu/giphy.gif",
            "https://media.giphy.com/media/zkppEMFvRX5FC/giphy.gif",
            "https://media.giphy.com/media/jR22gdcPiOLaE/giphy.gif",
            "https://media.giphy.com/media/QGc8RgRvMonFm/giphy.gif",
            "https://media.giphy.com/media/G3va31oEEnIkM/giphy.gif",
            "https://media.giphy.com/media/11rWoZNpAKw8w/giphy.gif",
            "https://media.giphy.com/media/OCQuZxeZ3OKXtG6Ouc/giphy.gif",
            "https://media.giphy.com/media/2fLX7xDEhleyubyBmv/giphy.gif",
        ],
    },
    "hug": {
        "emoji": "🫂",
        "phrase": "{author} vous serre fort dans ses bras {target} !",
        "label_plural": "hugs",
        "gifs": [
            "https://media.giphy.com/media/u9BxQbM5bxvwY/giphy.gif",
            "https://media.giphy.com/media/qscdhWs5o3yb6/giphy.gif",
            "https://media.giphy.com/media/49mdjsMrH7oze/giphy.gif",
            "https://media.giphy.com/media/WynnqxhdFEPYY/giphy.gif",
            "https://media.giphy.com/media/svXXBgduBsJ1u/giphy.gif",
            "https://media.giphy.com/media/LIqFOpO9Qh0uA/giphy.gif",
            "https://media.giphy.com/media/Y8wCpaKI9PUBO/giphy.gif",
            "https://media.giphy.com/media/BXrwTdoho6hkQ/giphy.gif",
            "https://media.giphy.com/media/5eyhBKLvYhafu/giphy.gif",
        ],
    },
    "slap": {
        "emoji": "👋",
        "phrase": "{author} a collé une claque à {target} !",
        "label_plural": "slaps",
        "gifs": [
            "https://media.giphy.com/media/Gf3AUz3eBNbTW/giphy.gif",
            "https://media.giphy.com/media/xUNd9HZq1itMkiK652/giphy.gif",
            "https://media.giphy.com/media/m6etefcEsTANa/giphy.gif",
            "https://media.giphy.com/media/k1uYB5LvlBZqU/giphy.gif",
            "https://media.giphy.com/media/tX29X2Dx3sAXS/giphy.gif",
            "https://media.giphy.com/media/6Fad0loHc6Cbe/giphy.gif",
            "https://media.giphy.com/media/z9e80pvHo1ZF8ew9es/giphy.gif",
            "https://media.giphy.com/media/AlsIdbTgxX0LC/giphy.gif",
        ],
    },
}


async def _do_interaction_action(ctx: commands.Context, member: discord.Member, action: str):
    if member == ctx.author:
        await ctx.send(f"Tu ne peux pas te `!{action}` toi-même 😅")
        return
    if member.bot:
        await ctx.send("Les bots n'ont pas de sentiments 😢")
        return

    remaining = check_action_cooldown(ctx.guild.id, ctx.author.id, member.id, action)
    if remaining:
        await ctx.send(
            f"Tu as déjà utilisé `!{action}` sur {member.mention}. "
            f"Réessaie dans {format_remaining(remaining)}."
        )
        return

    cfg = ACTIONS[action]
    gd = gdata(ctx.guild.id)
    totals = gd["action_totals"].setdefault(action, {})
    uid = str(member.id)
    totals[uid] = totals.get(uid, 0) + 1
    total = totals[uid]
    await save_data()

    phrase = cfg["phrase"].format(author=ctx.author.mention, target=member.mention)
    embed = discord.Embed(
        description=(
            f"{cfg['emoji']} {phrase} {cfg['emoji']}\n"
            f"{member.mention} `a reçu {total} {cfg['label_plural']} au total.`"
        ),
        color=PINK,
    )
    embed.set_image(url=random.choice(cfg["gifs"]))
    await ctx.send(embed=embed)


@bot.command()
async def kiss(ctx, member: discord.Member):
    await _do_interaction_action(ctx, member, "kiss")


@bot.command()
async def hug(ctx, member: discord.Member):
    await _do_interaction_action(ctx, member, "hug")


@bot.command()
async def slap(ctx, member: discord.Member):
    await _do_interaction_action(ctx, member, "slap")


@bot.command()
async def leaderboard(ctx, action: str = "kiss"):
    action = action.lower()
    if action not in ACTIONS:
        await ctx.send(f"Action inconnue. Choix : {', '.join(ACTIONS)}.")
        return
    gd = gdata(ctx.guild.id)
    totals = gd["action_totals"].get(action, {})
    if not totals:
        await ctx.send(embed=embed_err("Leaderboard", f"Aucun `{action}` pour l'instant."))
        return
    top = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:10]
    lines = []
    for i, (uid, count) in enumerate(top, start=1):
        member = ctx.guild.get_member(int(uid))
        label = member.mention if member else f"(utilisateur {uid})"
        lines.append(f"**{i}.** {label} — `{count}`")
    embed = embed_pink(f"🏆 Leaderboard {action}", "\n".join(lines))
    await ctx.send(embed=embed)


# ---------------- HELP ---------------- #

def get_help_embed(author: discord.Member) -> discord.Embed:
    is_staff = isinstance(author, discord.Member) and (
        author.guild_permissions.administrator or author.guild_permissions.manage_guild
    )
    embed = discord.Embed(
        title="📜 Help",
        description="Voici les commandes disponibles pour toi.",
        color=RED,
    )
    embed.add_field(
        name="⚙️ Utilitaires",
        value=(
            "**!help** → affiche ce message\n"
            "**!ping** → pong\n"
            "**!pp [@user]** → affiche la photo de profil\n"
            "**!userinfo [@user]** → affiche les infos d'un membre\n"
            "**!serverinfo** → affiche les infos du serveur"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎲 Fun",
        value=(
            "**!8ball question** → pose une question à la boule magique\n"
            "**!leaderboard [kiss|hug|slap]** → classement des interactions"
        ),
        inline=False,
    )
    embed.add_field(
        name="💞 Interactions",
        value=(
            "**!kiss @user** → embrasse un membre avec un gif\n"
            "**!hug @user** → fait un câlin à un membre avec un gif\n"
            "**!slap @user** → colle une claque à un membre avec un gif"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎵 Musique",
        value=(
            "**!play <lien YouTube ou recherche>** → lance ou ajoute à la file\n"
            "**!pause** → met la musique en pause\n"
            "**!resume** → reprend la lecture\n"
            "**!skip** → passe à la musique suivante\n"
            "**!queue** → affiche la file d'attente\n"
            "**!nowplaying** → affiche ce qui joue actuellement\n"
            "**!stop** → arrête tout et vide la file\n"
            "**!leave** → déconnecte le bot du vocal"
        ),
        inline=False,
    )

    if is_staff:
        embed.add_field(
            name="🛡️ Modération",
            value=(
                "**!warns @user** → affiche les warns d'un membre\n"
                "**!warn @user** → ouvre un choix de raison pour avertir\n"
                "**!unwarn @user [index]** → retire le dernier warn (ou celui d'index N)\n"
                "**!clearwarns @user** → supprime tous les warns d'un membre\n"
                "**!sanctions @user** → historique complet des sanctions\n"
                "**!ban @user** → ouvre un choix de raison pour bannir\n"
                "**!unban pseudo_ou_id** → débannit un utilisateur\n"
                "**!kick @user** → ouvre un choix de raison pour expulser\n"
                "**!mute @user [durée]** → ouvre un choix de raison pour mute\n"
                "**!unmute @user** → retire le mute d'un membre\n"
                "**!timeout @user durée** → ouvre un choix de raison pour timeout\n"
                "**!clear 1-100** → supprime des messages\n"
                "**!banlist** → affiche les bannis"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔒 Gestion Serveur",
            value=(
                "**!lock** → bloque le salon\n"
                "**!unlock** → débloque le salon\n"
                "**!slowmode temps** → change le slowmode du salon\n"
                "**!lockdown** → bloque tout le serveur\n"
                "**!unlockdown** → débloque tout le serveur\n"
                "**!setlogs** → définit le salon logs courant\n"
                "**!setwelcome** → définit le salon de bienvenue courant\n"
                "**!msg #salon message** → envoie un message dans un salon\n"
                "**!rule #salon @role** → envoie le règlement et donne un rôle\n"
                "**!giveaway** → lance un giveaway interactif"
            ),
            inline=False,
        )
    return embed


@bot.command()
async def help(ctx):
    await ctx.send(embed=get_help_embed(ctx.author))


@bot.command()
async def userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    roles = [role.mention for role in reversed(member.roles) if role != ctx.guild.default_role]
    embed = discord.Embed(title=f"Infos de {member}", color=PINK)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=str(member.id), inline=False)
    embed.add_field(name="Pseudo affiché", value=member.display_name, inline=True)
    embed.add_field(name="Compte créé",
                    value=discord.utils.format_dt(member.created_at, "D"), inline=True)
    embed.add_field(name="Arrivé sur le serveur",
                    value=discord.utils.format_dt(member.joined_at, "D") if member.joined_at else "Inconnu",
                    inline=False)
    embed.add_field(name=f"Rôles ({len(roles)})",
                    value=", ".join(roles[:10]) if roles else "Aucun", inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def serverinfo(ctx):
    guild = ctx.guild
    embed = discord.Embed(title=f"Infos du serveur {guild.name}", color=PINK)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="ID", value=str(guild.id), inline=False)
    embed.add_field(name="Créé le",
                    value=discord.utils.format_dt(guild.created_at, "D"), inline=True)
    embed.add_field(name="Propriétaire", value=str(guild.owner), inline=True)
    embed.add_field(name="Membres", value=str(guild.member_count), inline=True)
    embed.add_field(name="Salons textuels", value=str(len(guild.text_channels)), inline=True)
    embed.add_field(name="Salons vocaux", value=str(len(guild.voice_channels)), inline=True)
    embed.add_field(name="Rôles", value=str(len(guild.roles)), inline=True)
    embed.add_field(name="Boosts", value=str(guild.premium_subscription_count), inline=True)
    await ctx.send(embed=embed)


# ==================================================================== #
#                          MODÉRATION                                  #
# ==================================================================== #

@bot.command()
@is_admin()
async def warn(ctx, member: discord.Member):
    err = await hierarchy_check(ctx, member)
    if err:
        await ctx.send(embed=embed_err("Warn", err))
        return
    embed = embed_pink("⚠ Warn",
                      f"Pour quelle raison veux-tu avertir {member.mention} ?")
    embed.set_footer(text="Choisis une raison dans le menu ci-dessous.")
    view = ModerationReasonView(member, ctx.author, "warn")
    view.message = await ctx.send(embed=embed, view=view)


@bot.command()
@is_admin()
async def warns(ctx, member: discord.Member):
    gd = gdata(ctx.guild.id)
    uid = str(member.id)
    entries = gd["warns"].get(uid, [])
    if not entries:
        await ctx.send(embed=embed_ok("Warns", f"{member.mention} n'a aucun warn."))
        return
    lines = []
    for i, w in enumerate(entries, start=1):
        mod = ctx.guild.get_member(w.get("moderator_id"))
        mod_label = mod.mention if mod else f"<@{w.get('moderator_id')}>"
        ts = w.get("timestamp")
        when = ""
        if ts:
            try:
                when = f" — {discord.utils.format_dt(datetime.fromisoformat(ts), 'R')}"
            except Exception:
                pass
        lines.append(f"**{i}.** {w.get('reason', '(sans raison)')} — par {mod_label}{when}")
    embed = embed_err(f"Warns de {member} ({len(entries)})", "\n".join(lines)[:4000])
    await ctx.send(embed=embed)


# alias pour rétro-compat avec l'ancienne commande !logs
@bot.command(name="logs")
@is_admin()
async def logs_cmd(ctx, member: discord.Member):
    await sanctions(ctx, member)


@bot.command()
@is_admin()
async def sanctions(ctx, member: discord.Member):
    gd = gdata(ctx.guild.id)
    uid = str(member.id)
    entries = gd["sanctions"].get(uid, [])
    if not entries:
        await ctx.send(embed=embed_ok("Sanctions", f"{member.mention} n'a aucune sanction."))
        return
    lines = []
    for i, s in enumerate(entries, start=1):
        mod = ctx.guild.get_member(s.get("moderator_id"))
        mod_label = mod.mention if mod else f"<@{s.get('moderator_id')}>"
        ts = s.get("timestamp")
        when = ""
        if ts:
            try:
                when = f" — {discord.utils.format_dt(datetime.fromisoformat(ts), 'R')}"
            except Exception:
                pass
        stype = s.get("type", "?").upper()
        lines.append(f"**{i}.** `{stype}` — {s.get('reason', '(sans raison)')} — {mod_label}{when}")
    await ctx.send(embed=embed_err(f"Sanctions de {member} ({len(entries)})",
                                   "\n".join(lines)[:4000]))


@bot.command()
@is_admin()
async def unwarn(ctx, member: discord.Member, index: int = None):
    gd = gdata(ctx.guild.id)
    uid = str(member.id)
    entries = gd["warns"].get(uid, [])
    if not entries:
        await ctx.send(embed=embed_err("Unwarn", f"{member.mention} n'a aucun warn."))
        return
    if index is None:
        removed = entries.pop()
    else:
        if index < 1 or index > len(entries):
            await ctx.send(embed=embed_err("Unwarn",
                                           f"Index invalide. Choisis entre 1 et {len(entries)}."))
            return
        removed = entries.pop(index - 1)
    await save_data()
    await guild_log(ctx.guild, "♻ Unwarn",
                    f"{member} | par {ctx.author} | warn retiré : {removed.get('reason')}")
    await ctx.send(embed=embed_ok("Unwarn",
                                  f"Warn retiré pour {member.mention} : {removed.get('reason')}"))


@bot.command()
@is_admin()
async def clearwarns(ctx, member: discord.Member):
    gd = gdata(ctx.guild.id)
    uid = str(member.id)
    before = len(gd["warns"].get(uid, []))
    gd["warns"][uid] = []
    await save_data()
    await guild_log(ctx.guild, "♻ Clearwarns", f"{member} | par {ctx.author} | {before} warns effacés")
    await ctx.send(embed=embed_ok("Clearwarns",
                                  f"{before} warn(s) effacé(s) pour {member.mention}."))


@bot.command()
@is_admin()
async def ban(ctx, member: discord.Member):
    err = await hierarchy_check(ctx, member)
    if err:
        await ctx.send(embed=embed_err("Ban", err))
        return
    embed = embed_pink("⛔ Bannissement",
                      f"Pour quelle raison veux-tu bannir {member.mention} ?")
    embed.set_footer(text="Choisis une raison dans le menu ci-dessous.")
    view = ModerationReasonView(member, ctx.author, "ban")
    view.message = await ctx.send(embed=embed, view=view)


@bot.command()
@is_admin()
async def unban(ctx, *, user_query: str):
    try:
        bans = [entry async for entry in ctx.guild.bans(limit=2000)]
    except discord.Forbidden:
        await ctx.send("Le bot n'a pas la permission de voir les bans.")
        return
    except Exception as exc:
        await ctx.send(f"Erreur lors de la récupération des bans : {exc}")
        return

    user_query = user_query.strip()
    target_entry = None
    q_low = user_query.lower()
    for entry in bans:
        u = entry.user
        username = u.name.lower()
        global_name = (u.global_name or "").lower()
        legacy = f"{u.name}#{u.discriminator}".lower() if u.discriminator != "0" else username
        if str(u.id) == user_query or q_low in (username, global_name, legacy):
            target_entry = entry
            break

    if target_entry is None:
        await ctx.send("Aucun utilisateur banni ne correspond à ce pseudo ou cet ID.")
        return

    try:
        await ctx.guild.unban(target_entry.user, reason=f"Unban par {ctx.author}")
    except discord.Forbidden:
        await ctx.send("Le bot n'a pas la permission de débannir cet utilisateur.")
        return
    except Exception as exc:
        await ctx.send(f"Erreur lors du débannissement : {exc}")
        return

    await guild_log(ctx.guild, "🔓 Unban", f"{target_entry.user} | par {ctx.author}")
    await ctx.send(embed=embed_ok("Unban", f"{target_entry.user} a été débanni."))


@bot.command()
@is_admin()
async def kick(ctx, member: discord.Member):
    err = await hierarchy_check(ctx, member)
    if err:
        await ctx.send(embed=embed_err("Kick", err))
        return
    embed = embed_pink("👢 Expulsion",
                      f"Pour quelle raison veux-tu expulser {member.mention} ?")
    embed.set_footer(text="Choisis une raison dans le menu ci-dessous.")
    view = ModerationReasonView(member, ctx.author, "kick")
    view.message = await ctx.send(embed=embed, view=view)


@bot.command()
@is_admin()
async def mute(ctx, member: discord.Member, duration: str = "1h"):
    err = await hierarchy_check(ctx, member)
    if err:
        await ctx.send(embed=embed_err("Mute", err))
        return
    d = parse_duration(duration)
    if not d:
        await ctx.send(embed=embed_err("Erreur", "Format invalide (ex: 10m, 1h, 1d)."))
        return
    if d > MAX_TIMEOUT:
        await ctx.send(embed=embed_err("Erreur", "Discord limite les mutes à 28 jours."))
        return
    embed = embed_pink("🔇 Mute",
                      f"Pour quelle raison veux-tu mute {member.mention} pendant {duration} ?")
    embed.set_footer(text="Choisis une raison dans le menu ci-dessous.")
    view = ModerationReasonView(member, ctx.author, "mute", d)
    view.message = await ctx.send(embed=embed, view=view)


@bot.command()
@is_admin()
async def unmute(ctx, member: discord.Member):
    try:
        await member.timeout(None, reason=f"Unmute par {ctx.author}")
    except discord.Forbidden:
        await ctx.send("Le bot n'a pas la permission de retirer le mute.")
        return
    except Exception as exc:
        await ctx.send(f"Erreur lors du unmute : {exc}")
        return
    await guild_log(ctx.guild, "🔊 Unmute", f"{member} | par {ctx.author}")
    await ctx.send(embed=embed_ok("Unmute", f"{member.mention} n'est plus mute."))


@bot.command()
@is_admin()
async def timeout(ctx, member: discord.Member, duration: str):
    err = await hierarchy_check(ctx, member)
    if err:
        await ctx.send(embed=embed_err("Timeout", err))
        return
    d = parse_duration(duration)
    if not d:
        await ctx.send(embed=embed_err("Erreur", "Format invalide (ex: 10m, 1h, 1d)."))
        return
    if d > MAX_TIMEOUT:
        await ctx.send(embed=embed_err("Erreur", "Discord limite les timeouts à 28 jours."))
        return
    embed = embed_pink("⏱ Timeout",
                      f"Pour quelle raison veux-tu timeout {member.mention} pendant {duration} ?")
    embed.set_footer(text="Choisis une raison dans le menu ci-dessous.")
    view = ModerationReasonView(member, ctx.author, "timeout", d)
    view.message = await ctx.send(embed=embed, view=view)


@bot.command()
@is_admin()
async def clear(ctx, amount: int):
    if amount < 1 or amount > 100:
        await ctx.send(embed=embed_err("Erreur", "1-100 seulement."))
        return
    try:
        deleted = await ctx.channel.purge(limit=amount)
    except discord.Forbidden:
        await ctx.send("Le bot n'a pas la permission de supprimer ces messages.")
        return
    await guild_log(ctx.guild, "🧹 Clear",
                    f"{len(deleted)} messages supprimés dans {ctx.channel.mention} par {ctx.author}")
    await ctx.send(embed=embed_ok("Clear", f"{len(deleted)} messages supprimés."),
                   delete_after=3)


@bot.command()
@is_admin()
async def lock(ctx):
    try:
        await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    except discord.Forbidden:
        await ctx.send("Permissions insuffisantes pour lock ce salon.")
        return
    await ctx.send(embed=embed_ok("Lock", "Salon bloqué."))


@bot.command()
@is_admin()
async def unlock(ctx):
    try:
        await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=None)
    except discord.Forbidden:
        await ctx.send("Permissions insuffisantes pour unlock ce salon.")
        return
    await ctx.send(embed=embed_ok("Unlock", "Salon débloqué."))


@bot.command()
@is_admin()
async def slowmode(ctx, duration: str):
    if duration == "0":
        seconds = 0
    else:
        delta = parse_duration(duration)
        if not delta:
            await ctx.send(embed=embed_err("Erreur", "Format invalide (ex: 10s, 30s, 1m, 5m)."))
            return
        seconds = int(delta.total_seconds())
    if seconds > 21600:
        await ctx.send(embed=embed_err("Erreur", "Le slowmode maximum est de 6 heures."))
        return
    try:
        await ctx.channel.edit(slowmode_delay=seconds)
    except discord.Forbidden:
        await ctx.send("Le bot n'a pas la permission de modifier le slowmode.")
        return
    except Exception as exc:
        await ctx.send(f"Erreur lors du changement du slowmode : {exc}")
        return
    label = "désactivé" if seconds == 0 else duration
    await ctx.send(embed=embed_ok("Slowmode", f"Slowmode réglé sur {label}."))


async def _apply_lockdown(guild: discord.Guild, value: Optional[bool]):
    async def _set(c):
        try:
            await c.set_permissions(guild.default_role, send_messages=value)
            return True
        except Exception as exc:
            log.warning("Impossible de lock/unlock %s : %s", c, exc)
            return False
    results = await asyncio.gather(*(_set(c) for c in guild.text_channels))
    return sum(1 for r in results if r), len(results)


@bot.command()
@is_admin()
async def lockdown(ctx):
    ok, total = await _apply_lockdown(ctx.guild, False)
    await ctx.send(embed=embed_ok("Lockdown", f"{ok}/{total} salons bloqués."))


@bot.command()
@is_admin()
async def unlockdown(ctx):
    ok, total = await _apply_lockdown(ctx.guild, None)
    await ctx.send(embed=embed_ok("Unlockdown", f"{ok}/{total} salons débloqués."))


@bot.command()
@is_admin()
async def banlist(ctx):
    try:
        bans = [entry async for entry in ctx.guild.bans(limit=2000)]
    except discord.Forbidden:
        await ctx.send("Le bot n'a pas la permission de voir les bans.")
        return
    except Exception as exc:
        await ctx.send(f"Erreur lors de la récupération des bans : {exc}")
        return
    if not bans:
        await ctx.send(embed=embed_ok("Banlist", "Aucun ban."))
        return
    bans_list = bans[:20]
    desc = "\n".join(f"{b.user} — {b.reason or 'Aucune raison'}" for b in bans_list)
    if len(bans) > 20:
        desc += f"\n\nEt {len(bans) - 20} autres..."
    await ctx.send(embed=embed_err("Banlist", desc[:4000]))


# ==================================================================== #
#                       GESTION SERVEUR                                #
# ==================================================================== #

@bot.command()
@is_admin()
async def setlogs(ctx):
    gd = gdata(ctx.guild.id)
    gd["log_channel_id"] = ctx.channel.id
    await save_data()
    await ctx.send(embed=embed_ok("Logs", f"Salon logs défini sur {ctx.channel.mention}."))


@bot.command()
@is_admin()
async def setwelcome(ctx):
    gd = gdata(ctx.guild.id)
    gd["welcome_channel_id"] = ctx.channel.id
    await save_data()
    await ctx.send(embed=embed_ok("Bienvenue",
                                  f"Salon de bienvenue défini sur {ctx.channel.mention}."))


@bot.command()
@is_admin()
async def msg(ctx, *, content: str):
    if not ctx.message.channel_mentions:
        await ctx.send("Aucun salon mentionné.")
        return
    channel = ctx.message.channel_mentions[0]
    message_text = content.replace(channel.mention, "").strip()
    if not message_text:
        await ctx.send("Message vide.")
        return
    try:
        await channel.send(message_text)
    except discord.Forbidden:
        await ctx.send("Je n'ai pas la permission d'envoyer dans ce salon.")
        return
    await ctx.send(f"Message envoyé dans {channel.mention}.")


@bot.command()
@is_admin()
async def rule(ctx):
    if not ctx.message.channel_mentions or not ctx.message.role_mentions:
        await ctx.send("Mentionne un salon et un rôle, par exemple `!rule #reglement @Membre`.")
        return
    channel = ctx.message.channel_mentions[0]
    role = ctx.message.role_mentions[0]
    embed = discord.Embed(
        title="Règlement de Kyoren 🎐",
        description=(
            "Ce présent règlement doit être respecté dans ce serveur sous peine de sanction "
            "par le(s) modérateur(rice)(s).\n\n"
            "**Règle 1**\n"
            "Règlement du serveur Kyoren #🇵🇸🇨🇩\n\n"
            "**Respect**\n"
            "Le respect entre membres est obligatoire. Les insultes, le harcèlement, les menaces "
            "ou toute forme de discrimination sont interdits.\n\n"
            "**Spam et publicité**\n"
            "Le spam, le flood et la publicité sans autorisation du staff sont interdits.\n\n"
            "**Contenu**\n"
            "Les contenus NSFW, violents, choquants ou illégaux sont interdits. Évitez les débats "
            "conflictuels (politique, religion...).\n\n"
            "Pour accéder à l'intégralité du serveur, merci de lire le règlement puis d'accepter "
            "en cliquant sur le bouton ci-dessous."
        ),
        color=PINK,
    )
    embed.add_field(name="Rôle donné", value=role.mention, inline=False)
    try:
        await channel.send(embed=embed, view=RulesView(role.id))
    except discord.Forbidden:
        await ctx.send("Je n'ai pas la permission d'envoyer le règlement dans ce salon.")
        return
    await ctx.send(f"Règlement envoyé dans {channel.mention} avec le rôle {role.mention}.")


@bot.command()
@is_admin()
async def giveaway(ctx):
    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    messages_to_delete = []
    try:
        msg1 = await ctx.send("Quel est le lot du giveaway ?")
        messages_to_delete.append(msg1)
        prize_msg = await bot.wait_for("message", check=check, timeout=60)
        messages_to_delete.append(prize_msg)
        prize = prize_msg.content

        msg2 = await ctx.send("Quelle est la description ?")
        messages_to_delete.append(msg2)
        desc_msg = await bot.wait_for("message", check=check, timeout=60)
        messages_to_delete.append(desc_msg)
        description = desc_msg.content

        msg3 = await ctx.send("Quelle est la durée ? (ex: 10m, 1h, 1d)")
        messages_to_delete.append(msg3)
        dur_msg = await bot.wait_for("message", check=check, timeout=60)
        messages_to_delete.append(dur_msg)
        duration_str = dur_msg.content
        duration = parse_duration(duration_str)
        if not duration:
            await ctx.send("Durée invalide.")
            return

        msg4 = await ctx.send("Dans quel salon ? (mentionne le salon)")
        messages_to_delete.append(msg4)
        chan_msg = await bot.wait_for("message", check=check, timeout=60)
        messages_to_delete.append(chan_msg)
        if not chan_msg.channel_mentions:
            await ctx.send("Salon invalide.")
            return
        channel = chan_msg.channel_mentions[0]
    except asyncio.TimeoutError:
        await ctx.send("Giveaway annulé : temps écoulé.")
        return

    end_time = datetime.now(timezone.utc) + duration
    end_fmt = discord.utils.format_dt(end_time, "R")

    def build_embed(remaining_label=None):
        return discord.Embed(
            title="🎉 GIVEAWAY 🎉",
            description=(
                f"**Lot :** {prize}\n"
                f"**Description :** {description}\n"
                f"**Fin :** {remaining_label or end_fmt}\n"
                f"**Organisé par :** {ctx.author.mention}\n\n"
                f"Réagissez avec 🎉 pour participer !"
            ),
            color=GREEN,
        )

    giveaway_msg = await channel.send(embed=build_embed())
    await giveaway_msg.add_reaction("🎉")

    for m in messages_to_delete:
        try:
            await m.delete()
        except Exception:
            pass

    # On laisse Discord gérer l'affichage du timestamp relatif. On attend juste la fin.
    try:
        await asyncio.sleep(max(0, (end_time - datetime.now(timezone.utc)).total_seconds()))
    except asyncio.CancelledError:
        return

    try:
        giveaway_msg = await channel.fetch_message(giveaway_msg.id)
    except Exception:
        return

    reaction = next((r for r in giveaway_msg.reactions if str(r.emoji) == "🎉"), None)
    if reaction is None:
        await channel.send("Aucun participant.")
        return

    users = [u async for u in reaction.users() if not u.bot]
    if not users:
        await channel.send("Aucun participant.")
        return

    winner = random.choice(users)
    await channel.send(f"🎉 Félicitations {winner.mention} ! Tu as gagné **{prize}** !")
    try:
        await winner.send(embed=embed_ok("🎉 Giveaway gagné !",
                                         f"Tu as gagné : {prize}\nDescription : {description}"))
    except Exception:
        pass


# ==================================================================== #
#                          COMMANDES MUSIQUE                           #
# ==================================================================== #

@bot.command()
async def play(ctx, *, query: str):
    if yt_dlp is None:
        await ctx.send(embed=embed_err(
            "Musique", "`yt-dlp` n'est pas installé sur le serveur du bot.\n"
                      "Installe-le avec : `pip install -U yt-dlp`"))
        return

    state = await ensure_voice(ctx)
    if state is None:
        return

    loading = await ctx.send(embed=embed_ok("🔎 Recherche", f"`{query}`"))
    try:
        track = await extract_track(query, ctx.author)
    except Exception as exc:
        await loading.edit(embed=embed_err("Musique",
                                           f"Impossible de récupérer la piste : {exc}"))
        return

    state.enqueue(track)
    state.text_channel = ctx.channel
    state.start()

    if state.current is None and len(state.queue) == 1:
        # Va démarrer immédiatement.
        emb = embed_ok("🎵 Ajouté",
                       f"**[{track.title}]({track.webpage_url})** — démarrage imminent.")
    else:
        position = len(state.queue)
        emb = embed_ok("🎵 Ajouté à la file",
                       f"**[{track.title}]({track.webpage_url})** — position #{position}")

    try:
        await loading.edit(embed=emb)
    except Exception:
        await ctx.send(embed=emb)


@bot.command()
async def pause(ctx):
    state = _music_state.get(ctx.guild.id)
    if not state or state.voice is None or not state.voice.is_playing():
        await ctx.send(embed=embed_err("Musique", "Rien n'est en lecture."))
        return
    state.voice.pause()
    await ctx.send(embed=embed_ok("⏸ Pause", "Lecture mise en pause."))


@bot.command()
async def resume(ctx):
    state = _music_state.get(ctx.guild.id)
    if not state or state.voice is None or not state.voice.is_paused():
        await ctx.send(embed=embed_err("Musique", "Rien n'est en pause."))
        return
    state.voice.resume()
    await ctx.send(embed=embed_ok("▶ Reprise", "Lecture reprise."))


@bot.command()
async def skip(ctx):
    state = _music_state.get(ctx.guild.id)
    if not state or state.voice is None or not (state.voice.is_playing() or state.voice.is_paused()):
        await ctx.send(embed=embed_err("Musique", "Rien à skip."))
        return
    state.voice.stop()  # déclenche `after` -> next_event
    await ctx.send(embed=embed_ok("⏭ Skip", "Passage à la musique suivante."))


@bot.command(name="queue", aliases=["q"])
async def queue_cmd(ctx):
    state = _music_state.get(ctx.guild.id)
    if not state or (state.current is None and not state.queue):
        await ctx.send(embed=embed_ok("🎵 File d'attente", "La file est vide."))
        return

    lines = []
    if state.current:
        dur = format_secs(state.current.duration) if state.current.duration else "?:??"
        lines.append(
            f"**En lecture :** [{state.current.title}]({state.current.webpage_url}) "
            f"— `{dur}` — {state.current.requester_mention}"
        )
    if state.queue:
        lines.append("")
        lines.append("**File d'attente :**")
        total = 0
        for i, t in enumerate(state.queue[:15], start=1):
            dur = format_secs(t.duration) if t.duration else "?:??"
            total += t.duration or 0
            lines.append(f"**{i}.** [{t.title}]({t.webpage_url}) — `{dur}` — {t.requester_mention}")
        if len(state.queue) > 15:
            lines.append(f"… et **{len(state.queue) - 15}** autres.")
        if total > 0:
            lines.append("")
            lines.append(f"Durée totale approximative : `{format_secs(total)}`")

    embed = embed_ok("🎵 File d'attente", "\n".join(lines)[:4000])
    await ctx.send(embed=embed)


@bot.command(name="nowplaying", aliases=["np"])
async def nowplaying(ctx):
    state = _music_state.get(ctx.guild.id)
    if not state or state.current is None:
        await ctx.send(embed=embed_err("Musique", "Rien en lecture."))
        return
    t = state.current
    dur = format_secs(t.duration) if t.duration else "?:??"
    await ctx.send(embed=embed_ok(
        "🎵 En lecture",
        f"**[{t.title}]({t.webpage_url})**\n"
        f"Durée : `{dur}` — Demandé par {t.requester_mention}",
    ))


@bot.command()
async def stop(ctx):
    state = _music_state.get(ctx.guild.id)
    if not state:
        await ctx.send(embed=embed_err("Musique", "Je ne joue rien."))
        return
    state.queue.clear()
    if state.voice and (state.voice.is_playing() or state.voice.is_paused()):
        state.voice.stop()
    await ctx.send(embed=embed_ok("⏹ Stop", "Lecture arrêtée et file vidée."))


@bot.command(aliases=["disconnect", "dc"])
async def leave(ctx):
    state = _music_state.get(ctx.guild.id)
    if not state or state.voice is None:
        await ctx.send(embed=embed_err("Musique", "Je ne suis connecté à aucun vocal."))
        return
    await state.disconnect()
    await ctx.send(embed=embed_ok("👋 Leave", "Déconnecté du vocal."))


@bot.event
async def on_voice_state_update(member, before, after):
    """Déconnecte le bot si le salon vocal devient vide."""
    if member.bot:
        return
    state = _music_state.get(member.guild.id)
    if not state or state.voice is None:
        return
    channel = state.voice.channel
    if channel is None:
        return
    humans = [m for m in channel.members if not m.bot]
    if not humans:
        await asyncio.sleep(60)  # grâce d'une minute
        # revérifier
        state = _music_state.get(member.guild.id)
        if state is None or state.voice is None:
            return
        channel = state.voice.channel
        if channel is None:
            return
        humans = [m for m in channel.members if not m.bot]
        if not humans:
            await state.disconnect()


# ==================================================================== #
#                              RUN                                     #
# ==================================================================== #

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
