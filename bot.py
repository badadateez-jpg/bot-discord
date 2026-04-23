import os
import json
import re
import discord
import random
import asyncio
from datetime import datetime, timedelta
from discord.ext import commands

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# ---------------- INTENTS ---------------- #

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

log_channel_id = None
action_cooldowns = {}
action_totals = {
    "kiss": {},
    "hug": {},
    "slap": {}
}

# ---------------- DATA ---------------- #

def load_data():
    try:
        with open("data.json", "r") as f:
            return json.load(f)
    except:
        return {"warns": {}, "sanctions": {}}

def save_data():
    with open("data.json", "w") as f:
        json.dump(data, f, indent=4)

data = load_data()

# ---------------- EMBED ---------------- #

def e(title, desc):
    return discord.Embed(
        title=title,
        description=desc,
        color=discord.Color.red()
    )

MODERATION_REASON_OPTIONS = [
    ("Propos raciste", "â›”"),
    ("Spam / Flood", "ðŸ“›"),
    ("Injures rÃ©pÃ©titives", "ðŸ¤¬"),
    ("HarcÃ¨lement", "ðŸš«"),
    ("Menaces", "âš ï¸"),
    ("Contenu interdit", "ðŸ”ž"),
    ("PublicitÃ© non autorisÃ©e", "ðŸ“£"),
    ("Contournement de sanction", "ðŸ”"),
    ("Autre...", "âœï¸")
]

class RulesView(discord.ui.View):
    def __init__(self, role_id):
        super().__init__(timeout=None)
        self.role_id = role_id

    @discord.ui.button(
        label="Accepter le rÃ¨glement",
        style=discord.ButtonStyle.success,
        custom_id="accept_rules_button"
    )
    async def accept_rules(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Cette action doit Ãªtre utilisÃ©e dans le serveur.",
                ephemeral=True
            )
            return

        role = interaction.guild.get_role(self.role_id)
        if role is None:
            await interaction.response.send_message(
                "Le rÃ´le associÃ© au rÃ¨glement est introuvable.",
                ephemeral=True
            )
            return

        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            await interaction.response.send_message(
                "Impossible de te retrouver sur le serveur.",
                ephemeral=True
            )
            return

        if role in member.roles:
            await interaction.response.send_message(
                f"Tu as dÃ©jÃ  le rÃ´le {role.mention}.",
                ephemeral=True
            )
            return

        try:
            await member.add_roles(role, reason="RÃ¨glement acceptÃ©")
        except discord.Forbidden:
            await interaction.response.send_message(
                "Je n'ai pas la permission de te donner ce rÃ´le.",
                ephemeral=True
            )
            return
        except Exception as exc:
            await interaction.response.send_message(
                f"Erreur lors de l'attribution du rÃ´le : {exc}",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Merci, tu as bien acceptÃ© le rÃ¨glement. Tu as reÃ§u le rÃ´le {role.mention}.",
            ephemeral=True
        )

async def execute_moderation_action(guild, moderator, target_member, action_name, reason, duration=None):
    if action_name == "ban":
        try:
            await target_member.send(embed=e("â›” Ban", f"Raison : {reason}"))
        except:
            pass
        await target_member.ban(reason=reason)
        await log(guild, "â›” Ban", f"{target_member} | {reason}")
        return e("Ban", f"{target_member.mention}\n{reason}")

    if action_name == "warn":
        await add_warn(guild, target_member, reason)
        return e("âš  Warn", f"{target_member.mention}\n{reason}")

    if action_name == "kick":
        try:
            await target_member.send(embed=e("ðŸ‘¢ Kick", f"Raison : {reason}"))
        except:
            pass
        await target_member.kick(reason=reason)
        await log(guild, "ðŸ‘¢ Kick", f"{target_member} | {reason}")
        return e("Kick", f"{target_member.mention}\n{reason}")

    if action_name in ("timeout", "mute"):
        if duration is None:
            raise ValueError("La durÃ©e du timeout est introuvable.")
        await target_member.timeout(duration, reason=reason)
        await log(guild, "â± Timeout", f"{target_member} | {reason}")
        title = "Mute" if action_name == "mute" else "Timeout"
        return e(title, f"{target_member.mention}\n{format_remaining(duration)}\n{reason}")

    raise ValueError("Action de modÃ©ration inconnue.")

class CustomReasonModal(discord.ui.Modal):
    def __init__(self, moderation_view):
        super().__init__(title="Autre raison")
        self.moderation_view = moderation_view
        self.reason_input = discord.ui.TextInput(
            label="Raison personnalisÃ©e",
            placeholder="Ã‰cris la raison ici",
            max_length=200
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.moderation_view.moderator.id:
            await interaction.response.send_message(
                "Seul le membre qui a lancÃ© la commande peut choisir cette raison.",
                ephemeral=True
            )
            return

        await self.moderation_view.perform_action(interaction, str(self.reason_input))

class ModerationReasonSelect(discord.ui.Select):
    def __init__(self, moderation_view):
        self.moderation_view = moderation_view
        options = [
            discord.SelectOption(label=label, value=label, emoji=emoji)
            for label, emoji in MODERATION_REASON_OPTIONS
        ]
        super().__init__(
            placeholder="Choisis la raison de la sanction",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.moderation_view.moderator.id:
            await interaction.response.send_message(
                "Seul le membre qui a lancÃ© la commande peut choisir cette raison.",
                ephemeral=True
            )
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
                interaction.guild,
                self.moderator,
                self.target_member,
                self.action_name,
                reason,
                self.duration
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "Je n'ai pas la permission d'appliquer cette sanction.",
                ephemeral=True
            )
            return
        except Exception as exc:
            await interaction.response.send_message(
                f"Erreur lors de la sanction : {exc}",
                ephemeral=True
            )
            return

        for item in self.children:
            item.disabled = True

        if self.message is not None:
            await self.message.edit(embed=result_embed, view=None)

        if interaction.response.is_done():
            await interaction.followup.send("Sanction appliquÃ©e.", ephemeral=True)
        else:
            await interaction.response.send_message("Sanction appliquÃ©e.", ephemeral=True)

        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except:
                pass

# ---------------- UTILS ---------------- #

def is_admin():
    async def predicate(ctx):
        return ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.manage_guild
    return commands.check(predicate)

def parse_duration(d):
    m = re.match(r"(\d+)([smhd])", d)
    if not m:
        return None

    v, u = int(m[1]), m[2]

    return {
        "s": timedelta(seconds=v),
        "m": timedelta(minutes=v),
        "h": timedelta(hours=v),
        "d": timedelta(days=v)
    }.get(u)

def format_remaining(td):
    days = td.days
    hours, remainder = divmod(td.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days > 0:
        return f"{days}j {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m {seconds}s"

def check_action_cooldown(author_id, target_id, action_name):
    now = datetime.now()
    cooldown_key = (author_id, target_id, action_name)
    expires_at = action_cooldowns.get(cooldown_key)

    if expires_at and expires_at > now:
        return expires_at - now

    action_cooldowns[cooldown_key] = now + timedelta(hours=2)
    return None

def increment_action_total(target_id, action_name):
    user_totals = action_totals.setdefault(action_name, {})
    user_totals[target_id] = user_totals.get(target_id, 0) + 1
    return user_totals[target_id]

# ---------------- LOGS ---------------- #

async def log(guild, title, desc):
    global log_channel_id
    if not log_channel_id:
        return

    ch = guild.get_channel(log_channel_id)
    if not ch:
        try:
            ch = await guild.fetch_channel(log_channel_id)
        except:
            return

    await ch.send(embed=e(title, desc))

# ---------------- WARN SYSTEM ---------------- #

async def add_warn(guild, member, reason):
    uid = str(member.id)

    data["warns"].setdefault(uid, [])
    data["sanctions"].setdefault(uid, [])

    data["warns"][uid].append(reason)
    data["sanctions"][uid].append(f"WARN: {reason}")

    save_data()

    count = len(data["warns"][uid])

    try:
        await member.send(embed=e("âš  Warn reÃ§u", f"Raison : {reason}"))
    except:
        pass

    if count == 3:
        await member.timeout(timedelta(hours=1), reason="3 warns")
        await log(guild, "â± Timeout", f"{member} â†’ 1h")

    elif count == 5:
        await member.timeout(timedelta(hours=72), reason="5 warns")
        await log(guild, "â± Timeout", f"{member} â†’ 72h")

    elif count >= 8:
        await member.ban(reason="8 warns")
        await log(guild, "â›” Ban auto", f"{member}")

# ---------------- EVENTS ---------------- #

@bot.event
async def on_ready():
    print(f"BOT CONNECTÃ‰ : {bot.user}")

@bot.event
async def on_member_join(member):
    count = len(member.guild.members)
    await member.guild.system_channel.send(f"Bienvenue {member.mention}, tu es le {count}Ã¨me membre")

@bot.event
async def on_member_remove(member):
    await log(member.guild, "ðŸ‘‹ Leave", f"{member}")

@bot.event
async def on_message_delete(message):
    if message.author.bot:
        return

    await log(
        message.guild,
        "ðŸ—‘ Message supprimÃ©",
        f"Auteur: {message.author}\nSalon: #{message.channel}\nContenu: {message.content}"
    )

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content.lower().strip()

    auto_replies = {
        "salut": [
            "salut à toi, quoi de neuf ?",
            "saluttt, tu vas bien ?",
            "yooo salut !"
        ],
        "yo": [
            "yooo",
            "yo ça dit quoi ?",
            "yoo, bien ou bien ?"
        ],
        "coucou": [
            "coucouuu",
            "coucou toi !",
            "cc hehe"
        ],
        "cc": [
            "cc",
            "cc ça va ?",
            "coucouuu"
        ],
        "bonjour": [
            "bonjourrr",
            "bonjour, j'espère que tu vas bien",
            "salut salut !"
        ],
        "wesh": [
            "weshhh",
            "wesh bien ou quoi ?",
            "weshh la forme ?"
        ],
        "re": [
            "rebienvenuee",
            "reeee",
            "re toi"
        ]
    }

    if content in auto_replies:
        await message.channel.send(random.choice(auto_replies[content]))
    elif re.search(r"(?:^|\s)quoi[?.!… ]*$", content):
        await message.channel.send("QUOICOUBEHH")
    elif re.search(r"(?:^|\s)hein[?.!… ]*$", content):
        await message.channel.send("APAGNANN")
    elif re.search(r"(?:^|\s)comment[?.!… ]*$", content):
        await message.channel.send("COMMANDANT DE BORDDD")

    if message.content.strip() == "!":
        await message.channel.send(embed=get_help_embed(message))
        return

    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("Vous n'avez pas les permissions d'utiliser cette commande.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Argument manquant. Utilisez !help pour voir l'usage.")
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Cette commande est en cooldown. RÃ©essayez dans {error.retry_after:.1f} secondes.")
    else:
        await ctx.send("Une erreur est survenue.")

@bot.command()
@commands.cooldown(1, 10, commands.BucketType.user)
async def pp(ctx, member: discord.Member):
    embed = discord.Embed(title=f"Photo de profil de {member}", color=discord.Color.blue())
    embed.set_image(url=member.avatar.url)
    await ctx.send(embed=embed)

@bot.command(name="8ball")
async def ball(ctx, *, question):
    responses = [
        "Clairement, oui ! Tu as toutes tes chances de ton cÃ´tÃ©.",
        "Je pense que oui.",
        "Potentiellement...",
        "Je ne sais pas.",
        "Pas trop..",
        "Non mdrr",
        "Pas dutout, continue a rÃªver ailleurs !"
    ]
    answer = random.choice(responses)
    await ctx.send(answer)

@bot.command()
async def kiss(ctx, member: discord.Member):
    remaining = check_action_cooldown(ctx.author.id, member.id, "kiss")
    if remaining:
        await ctx.send(
            f"Tu as deja utilise `!kiss` sur {member.mention}. "
            f"Reessaie dans {format_remaining(remaining)}."
        )
        return

    gifs = [
        "https://media.giphy.com/media/MQVpBqASxSlFu/giphy.gif",
        "https://media.giphy.com/media/zkppEMFvRX5FC/giphy.gif",
        "https://media.giphy.com/media/jR22gdcPiOLaE/giphy.gif",
        "https://media.giphy.com/media/QGc8RgRvMonFm/giphy.gif",
        "https://media.giphy.com/media/G3va31oEEnIkM/giphy.gif",
        "https://media.giphy.com/media/11rWoZNpAKw8w/giphy.gif",
        "https://media.giphy.com/media/OCQuZxeZ3OKXtG6Ouc/giphy.gif",
        "https://media.giphy.com/media/2fLX7xDEhleyubyBmv/giphy.gif"
    ]
    total_received = increment_action_total(member.id, "kiss")

    embed = discord.Embed(
        description=(
            f"â¤ï¸ {ctx.author.mention} vous embrasse trÃ¨s fort {member.mention} ! â¤ï¸\n"
            f"{member.mention} `a reÃ§u {total_received} kiss au total.`"
        ),
        color=discord.Color.from_rgb(255, 105, 180)
    )
    embed.set_image(url=random.choice(gifs))
    await ctx.send(embed=embed)

@bot.command()
async def hug(ctx, member: discord.Member):
    remaining = check_action_cooldown(ctx.author.id, member.id, "hug")
    if remaining:
        await ctx.send(
            f"Tu as deja utilise `!hug` sur {member.mention}. "
            f"Reessaie dans {format_remaining(remaining)}."
        )
        return

    gifs = [
        "https://media.giphy.com/media/u9BxQbM5bxvwY/giphy.gif",
        "https://media.giphy.com/media/qscdhWs5o3yb6/giphy.gif",
        "https://media.giphy.com/media/49mdjsMrH7oze/giphy.gif",
        "https://media.giphy.com/media/WynnqxhdFEPYY/giphy.gif",
        "https://media.giphy.com/media/svXXBgduBsJ1u/giphy.gif",
        "https://media.giphy.com/media/LIqFOpO9Qh0uA/giphy.gif",
        "https://media.giphy.com/media/Y8wCpaKI9PUBO/giphy.gif",
        "https://media.giphy.com/media/BXrwTdoho6hkQ/giphy.gif",
        "https://media.giphy.com/media/5eyhBKLvYhafu/giphy.gif"
    ]
    total_received = increment_action_total(member.id, "hug")

    embed = discord.Embed(
        description=(
            f"ðŸ«‚ {ctx.author.mention} vous sert fort dans ses bras {member.mention} ! ðŸ«‚\n"
            f"{member.mention} `a reÃ§u {total_received} hugs au total.`"
        ),
        color=discord.Color.from_rgb(255, 105, 180)
    )
    embed.set_image(url=random.choice(gifs))
    await ctx.send(embed=embed)

@bot.command()
async def slap(ctx, member: discord.Member):
    remaining = check_action_cooldown(ctx.author.id, member.id, "slap")
    if remaining:
        await ctx.send(
            f"Tu as deja utilise `!slap` sur {member.mention}. "
            f"Reessaie dans {format_remaining(remaining)}."
        )
        return

    gifs = [
        "https://media.giphy.com/media/Gf3AUz3eBNbTW/giphy.gif",
        "https://media.giphy.com/media/xUNd9HZq1itMkiK652/giphy.gif",
        "https://media.giphy.com/media/m6etefcEsTANa/giphy.gif",
        "https://media.giphy.com/media/k1uYB5LvlBZqU/giphy.gif",
        "https://media.giphy.com/media/tX29X2Dx3sAXS/giphy.gif",
        "https://media.giphy.com/media/6Fad0loHc6Cbe/giphy.gif",
        "https://media.giphy.com/media/z9e80pvHo1ZF8ew9es/giphy.gif",
        "https://media.giphy.com/media/AlsIdbTgxX0LC/giphy.gif"
    ]
    total_received = increment_action_total(member.id, "slap")

    embed = discord.Embed(
        description=(
            f"ðŸ‘‹ {ctx.author.mention} vous a collÃ© une claque {member.mention} ! ðŸ‘‹\n"
            f"{member.mention} `a reÃ§u {total_received} slaps au total.`"
        ),
        color=discord.Color.from_rgb(255, 105, 180)
    )
    embed.set_image(url=random.choice(gifs))
    await ctx.send(embed=embed)

# ---------------- LOG CHANNEL ---------------- #

@bot.command()
@is_admin()
async def setlogs(ctx):
    global log_channel_id
    log_channel_id = ctx.channel.id
    await ctx.send(embed=e("Logs", "Salon logs dÃ©fini"))

@bot.command()
@is_admin()
async def msg(ctx, *, content):
    if not ctx.message.channel_mentions:
        await ctx.send("Aucun salon mentionnÃ©.")
        return
    channel = ctx.message.channel_mentions[-1]
    message = content.replace(channel.mention, "").strip()
    if not message:
        await ctx.send("Message vide.")
        return
    await channel.send(message)
    await ctx.send(f"Message envoyÃ© dans {channel.mention}")

@bot.command()
@is_admin()
async def rule(ctx):
    if not ctx.message.channel_mentions or not ctx.message.role_mentions:
        await ctx.send("Mentionne un salon et un rÃ´le, par exemple `!rule #reglement @Membre`.")
        return

    channel = ctx.message.channel_mentions[0]
    role = ctx.message.role_mentions[0]
    embed = discord.Embed(
        title="RÃ¨glement de Kyoren ðŸŽ",
        description=(
            "Ce prÃ©sent rÃ¨glement doit Ãªtre respectÃ© dans ce serveur sous peine de sanction par le(s) modÃ©rateur(rice)(s).\n\n"
            "**RÃ¨gle 1**\n"
            "RÃ¨glement du serveur Kyoren #ðŸ‡µðŸ‡¸ðŸ‡¨ðŸ‡©\n\n"
            "**Respect**\n"
            "Le respect entre membres est obligatoire. Les insultes, le harcÃ¨lement, les menaces ou toute forme de discrimination sont interdits.\n\n"
            "**Spam et publicitÃ©**\n"
            "Le spam, le flood et la publicitÃ© sans autorisation du staff sont interdits.\n\n"
            "**Contenu**\n"
            "Les contenus NSFW, violents, choquants ou illÃ©gaux sont interdits. Ã‰vitez les dÃ©bats conflictuels (politique, religionâ€¦).\n\n"
            "Pour accÃ©der Ã  l'intÃ©gralitÃ© du serveur merci de lire le rÃ¨glement puis d'accepter en cliquant sur le bouton ci-dessous."
        ),
        color=discord.Color.from_rgb(255, 105, 180)
    )
    embed.add_field(name="RÃ´le donnÃ©", value=role.mention, inline=False)

    await channel.send(embed=embed, view=RulesView(role.id))
    await ctx.send(f"RÃ¨glement envoyÃ© dans {channel.mention} avec le rÃ´le {role.mention}.")

# ---------------- MODERATION ---------------- #

@bot.command()
async def ping(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong ! `{latency}ms`")

def get_help_embed(ctx):
    is_staff = (
        ctx.author.guild_permissions.administrator
        or ctx.author.guild_permissions.manage_guild
    )

    embed = discord.Embed(
        title="ðŸ“œ Help",
        description="Voici les commandes disponibles pour toi.",
        color=discord.Color.red()
    )

    embed.add_field(
        name="âš™ï¸ Utilitaires",
        value=(
            "**!help** â†’ affiche ce message\n"
            "**!ping** â†’ pong\n"
            "**!pp @user** â†’ affiche la photo de profil\n"
            "**!userinfo [@user]** â†’ affiche les infos d'un membre\n"
            "**!serverinfo** â†’ affiche les infos du serveur"
        ),
        inline=False
    )

    embed.add_field(
        name="ðŸŽ² Fun",
        value="**!8ball question** â†’ pose une question Ã  la boule magique",
        inline=False
    )

    embed.add_field(
        name="ðŸ’ž Interactions",
        value=(
            "**!kiss @user** â†’ embrasse un membre avec un gif\n"
            "**!hug @user** â†’ fait un calin Ã  un membre avec un gif\n"
            "**!slap @user** â†’ colle une claque Ã  un membre avec un gif"
        ),
        inline=False
    )

    if is_staff:
        embed.add_field(
            name="ðŸ›¡ï¸ ModÃ©ration",
            value=(
                "**!logs @user** â†’ affiche warns et sanctions\n"
                "**!ban @user** â†’ ouvre un choix de raison pour bannir\n"
                "**!unban pseudo_ou_id** â†’ dÃ©bannit un utilisateur\n"
                "**!warn @user** â†’ ouvre un choix de raison pour avertir\n"
                "**!kick @user** â†’ ouvre un choix de raison pour expulser\n"
                "**!mute @user [durÃ©e]** â†’ ouvre un choix de raison pour mute\n"
                "**!unmute @user** â†’ retire le mute d'un membre\n"
                "**!timeout @user durÃ©e** â†’ ouvre un choix de raison pour timeout\n"
                "**!clear 1-100** â†’ supprime des messages\n"
                "**!banlist** â†’ affiche les bannis"
            ),
            inline=False
        )

        embed.add_field(
            name="ðŸ”’ Gestion Serveur",
            value=(
                "**!lock** â†’ bloque le salon\n"
                "**!unlock** â†’ dÃ©bloque le salon\n"
                "**!slowmode temps** â†’ change le slowmode du salon\n"
                "**!lockdown** â†’ bloque tout le serveur\n"
                "**!unlockdown** â†’ dÃ©bloque tout le serveur\n"
                "**!setlogs** â†’ dÃ©finit le salon logs\n"
                "**!msg message #salon** â†’ envoie un message dans un salon\n"
                "**!rule #salon @role** â†’ envoie le rÃ¨glement et donne un rÃ´le\n"
                "**!giveaway** â†’ lance un giveaway interactif"
            ),
            inline=False
        )

    return embed

@bot.command()
async def help(ctx):
    await ctx.send(embed=get_help_embed(ctx))

@bot.command()
async def userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    roles = [role.mention for role in member.roles if role != ctx.guild.default_role]
    embed = discord.Embed(
        title=f"Infos de {member}",
        color=discord.Color.from_rgb(255, 105, 180)
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=str(member.id), inline=False)
    embed.add_field(name="Pseudo affichÃ©", value=member.display_name, inline=True)
    embed.add_field(name="Compte crÃ©Ã©", value=member.created_at.strftime("%d/%m/%Y %H:%M"), inline=True)
    embed.add_field(name="ArrivÃ© sur le serveur", value=member.joined_at.strftime("%d/%m/%Y %H:%M") if member.joined_at else "Inconnu", inline=False)
    embed.add_field(name="RÃ´les", value=", ".join(roles[-10:]) if roles else "Aucun", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def serverinfo(ctx):
    guild = ctx.guild
    embed = discord.Embed(
        title=f"Infos du serveur {guild.name}",
        color=discord.Color.from_rgb(255, 105, 180)
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="ID", value=str(guild.id), inline=False)
    embed.add_field(name="CrÃ©Ã© le", value=guild.created_at.strftime("%d/%m/%Y %H:%M"), inline=True)
    embed.add_field(name="PropriÃ©taire", value=str(guild.owner), inline=True)
    embed.add_field(name="Membres", value=str(guild.member_count), inline=True)
    embed.add_field(name="Salons textuels", value=str(len(guild.text_channels)), inline=True)
    embed.add_field(name="Salons vocaux", value=str(len(guild.voice_channels)), inline=True)
    embed.add_field(name="RÃ´les", value=str(len(guild.roles)), inline=True)
    embed.add_field(name="Boosts", value=str(guild.premium_subscription_count), inline=True)
    await ctx.send(embed=embed)

@bot.command()
@is_admin()
async def warn(ctx, member: discord.Member):
    embed = discord.Embed(
        title="âš  Warn",
        description=f"Pour quelle raison veux-tu avertir {member.mention} ?",
        color=discord.Color.from_rgb(255, 105, 180)
    )
    embed.set_footer(text="Choisis une raison dans le menu ci-dessous.")
    view = ModerationReasonView(member, ctx.author, "warn")
    view.message = await ctx.send(embed=embed, view=view)

@bot.command()
@is_admin()
async def logs(ctx, member: discord.Member):
    uid = str(member.id)

    warns = data.get("warns", {}).get(uid, [])
    sanc = data.get("sanctions", {}).get(uid, [])

    await ctx.send(embed=e(
        "ðŸ“œ Logs",
        f"Warns ({len(warns)}):\n" + ("\n".join(warns) if warns else "Aucun") +
        f"\n\nSanctions ({len(sanc)}):\n" + ("\n".join(sanc) if sanc else "Aucune")
    ))

@bot.command()
@is_admin()
async def ban(ctx, member: discord.Member):
    embed = discord.Embed(
        title="â›” Bannissement",
        description=f"Pour quelle raison veux-tu bannir {member.mention} ?",
        color=discord.Color.from_rgb(255, 105, 180)
    )
    embed.set_footer(text="Choisis une raison dans le menu ci-dessous.")
    view = ModerationReasonView(member, ctx.author, "ban")
    view.message = await ctx.send(embed=embed, view=view)

@bot.command()
@is_admin()
async def unban(ctx, *, user_query):
    try:
        bans = [entry async for entry in ctx.guild.bans(limit=2000)]
    except discord.Forbidden:
        await ctx.send("Le bot n'a pas la permission de voir les bans.")
        return
    except Exception as exc:
        await ctx.send(f"Erreur lors de la rÃ©cupÃ©ration des bans : {exc}")
        return

    user_query = user_query.strip()
    target_entry = None

    for entry in bans:
        banned_user = entry.user
        username = banned_user.name
        global_name = banned_user.global_name or ""
        legacy_tag = f"{banned_user.name}#{banned_user.discriminator}" if banned_user.discriminator != "0" else banned_user.name

        if (
            str(banned_user.id) == user_query
            or username.lower() == user_query.lower()
            or global_name.lower() == user_query.lower()
            or legacy_tag.lower() == user_query.lower()
        ):
            target_entry = entry
            break

    if target_entry is None:
        await ctx.send("Aucun utilisateur banni ne correspond Ã  ce pseudo ou cet ID.")
        return

    try:
        await ctx.guild.unban(target_entry.user, reason=f"Unban par {ctx.author}")
    except discord.Forbidden:
        await ctx.send("Le bot n'a pas la permission de dÃ©bannir cet utilisateur.")
        return
    except Exception as exc:
        await ctx.send(f"Erreur lors du dÃ©bannissement : {exc}")
        return

    await log(ctx.guild, "ðŸ”“ Unban", f"{target_entry.user} | Par {ctx.author}")
    await ctx.send(embed=e("Unban", f"{target_entry.user} a Ã©tÃ© dÃ©banni."))

@bot.command()
@is_admin()
async def kick(ctx, member: discord.Member):
    embed = discord.Embed(
        title="ðŸ‘¢ Expulsion",
        description=f"Pour quelle raison veux-tu expulser {member.mention} ?",
        color=discord.Color.from_rgb(255, 105, 180)
    )
    embed.set_footer(text="Choisis une raison dans le menu ci-dessous.")
    view = ModerationReasonView(member, ctx.author, "kick")
    view.message = await ctx.send(embed=embed, view=view)

@bot.command()
@is_admin()
async def mute(ctx, member: discord.Member, duration: str = "1h"):
    d = parse_duration(duration)
    if not d:
        return await ctx.send(embed=e("Erreur", "Format invalide (ex: 10m, 1h, 1d)"))

    embed = discord.Embed(
        title="ðŸ”‡ Mute",
        description=f"Pour quelle raison veux-tu mute {member.mention} pendant {duration} ?",
        color=discord.Color.from_rgb(255, 105, 180)
    )
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

    await log(ctx.guild, "ðŸ”Š Unmute", f"{member} | Par {ctx.author}")
    await ctx.send(embed=e("Unmute", f"{member.mention} n'est plus mute."))

@bot.command()
@is_admin()
async def timeout(ctx, member: discord.Member, duration: str):
    d = parse_duration(duration)
    if not d:
        return await ctx.send(embed=e("Erreur", "Format invalide (ex: 10m, 1h, 1d)"))

    embed = discord.Embed(
        title="â± Timeout",
        description=f"Pour quelle raison veux-tu timeout {member.mention} pendant {duration} ?",
        color=discord.Color.from_rgb(255, 105, 180)
    )
    embed.set_footer(text="Choisis une raison dans le menu ci-dessous.")
    view = ModerationReasonView(member, ctx.author, "timeout", d)
    view.message = await ctx.send(embed=embed, view=view)

@bot.command()
@is_admin()
async def clear(ctx, amount: int):
    if amount < 1 or amount > 100:
        return await ctx.send(embed=e("Erreur", "1-100 seulement"))

    deleted = await ctx.channel.purge(limit=amount)
    await log(ctx.guild, "ðŸ§¹ Clear", f"{len(deleted)} messages supprimÃ©s")
    await ctx.send(embed=e("Clear", f"{len(deleted)} messages supprimÃ©s"), delete_after=3)

@bot.command()
@is_admin()
async def lock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send(embed=e("Lock", "Salon bloquÃ©"))

@bot.command()
@is_admin()
async def unlock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=None)
    await ctx.send(embed=e("Unlock", "Salon dÃ©bloquÃ©"))

@bot.command()
@is_admin()
async def slowmode(ctx, duration: str):
    if duration == "0":
        seconds = 0
    else:
        delta = parse_duration(duration)
        if not delta:
            return await ctx.send(embed=e("Erreur", "Format invalide (ex: 10s, 30s, 1m, 5m)"))
        seconds = int(delta.total_seconds())

    if seconds > 21600:
        return await ctx.send(embed=e("Erreur", "Le slowmode maximum est de 6 heures."))

    try:
        await ctx.channel.edit(slowmode_delay=seconds)
    except discord.Forbidden:
        await ctx.send("Le bot n'a pas la permission de modifier le slowmode.")
        return
    except Exception as exc:
        await ctx.send(f"Erreur lors du changement du slowmode : {exc}")
        return

    label = "dÃ©sactivÃ©" if seconds == 0 else duration
    await ctx.send(embed=e("Slowmode", f"Slowmode du salon rÃ©glÃ© sur {label}."))

@bot.command()
@is_admin()
async def lockdown(ctx):
    for c in ctx.guild.text_channels:
        await c.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send(embed=e("Lockdown", "Serveur bloquÃ©"))

@bot.command()
@is_admin()
async def unlockdown(ctx):
    for c in ctx.guild.text_channels:
        await c.set_permissions(ctx.guild.default_role, send_messages=None)
    await ctx.send(embed=e("Unlockdown", "Serveur dÃ©bloquÃ©"))

@bot.command()
@is_admin()
async def banlist(ctx):
    try:
        bans = [entry async for entry in ctx.guild.bans(limit=2000)]
    except discord.Forbidden:
        await ctx.send("Le bot n'a pas la permission de voir les bans.")
        return
    except Exception as exc:
        await ctx.send(f"Erreur lors de la rÃ©cupÃ©ration des bans : {exc}")
        return

    if not bans:
        return await ctx.send(embed=e("Banlist", "Aucun ban"))

    bans_list = list(bans)[:20]
    desc = "\n".join([f"{b.user} - {b.reason or 'Aucune raison'}" for b in bans_list])
    if len(bans) > 20:
        desc += f"\n\nEt {len(bans) - 20} autres..."

    try:
        await ctx.send(embed=e("Banlist", desc))
    except Exception as exc:
        await ctx.send(f"Erreur lors de l'envoi de la liste des bans : {exc}")

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

        msg3 = await ctx.send("Quelle est la durÃ©e ? (ex: 10m, 1h, 1d)")
        messages_to_delete.append(msg3)
        dur_msg = await bot.wait_for("message", check=check, timeout=60)
        messages_to_delete.append(dur_msg)
        duration_str = dur_msg.content
        duration = parse_duration(duration_str)
        if not duration:
            await ctx.send("DurÃ©e invalide.")
            return

        msg4 = await ctx.send("Dans quel salon ? (mentionnez le salon)")
        messages_to_delete.append(msg4)
        chan_msg = await bot.wait_for("message", check=check, timeout=60)
        messages_to_delete.append(chan_msg)
        if not chan_msg.channel_mentions:
            await ctx.send("Salon invalide.")
            return
        channel = chan_msg.channel_mentions[0]

    except asyncio.TimeoutError:
        await ctx.send("Giveaway annulÃ© : temps Ã©coulÃ©.")
        return

    embed = e("ðŸŽ‰ GIVEAWAY ðŸŽ‰", f"**Lot :** {prize}\n**Description :** {description}\n**DurÃ©e :** {duration_str}\n**OrganisÃ© par :** {ctx.author.mention}\n\nRÃ©agissez avec ðŸŽ‰ pour participer !")
    giveaway_msg = await channel.send(embed=embed)
    await giveaway_msg.add_reaction("ðŸŽ‰")

    for msg in messages_to_delete:
        try:
            await msg.delete()
        except:
            pass

    end_time = datetime.now() + duration

    while datetime.now() < end_time:
        remaining = end_time - datetime.now()
        if remaining.total_seconds() <= 0:
            break
        embed.description = f"**Lot :** {prize}\n**Description :** {description}\n**Temps restant :** {format_remaining(remaining)}\n**OrganisÃ© par :** {ctx.author.mention}\n\nRÃ©agissez avec ðŸŽ‰ pour participer !"
        await giveaway_msg.edit(embed=embed)
        await asyncio.sleep(30 * 60)

    giveaway_msg = await channel.fetch_message(giveaway_msg.id)
    reaction = None
    for r in giveaway_msg.reactions:
        if str(r.emoji) == "ðŸŽ‰":
            reaction = r
            break

    if not reaction:
        await channel.send("Aucun participant.")
        return

    users = []
    async for user in reaction.users():
        if not user.bot:
            users.append(user)

    if not users:
        await channel.send("Aucun participant.")
        return

    winner = random.choice(users)
    await channel.send(f"ðŸŽ‰ FÃ©licitations {winner.mention} ! Tu as gagnÃ© **{prize}** !")
    try:
        await winner.send(embed=e("ðŸŽ‰ Giveaway gagnÃ© !", f"Tu as gagnÃ© : {prize}\nDescription : {description}"))
    except:
        pass

# ---------------- RUN ---------------- #

if not DISCORD_TOKEN:
    raise ValueError(
        "Le token Discord est introuvable. "
        "Ajoute une variable d'environnement `DISCORD_TOKEN` avec ton token."
    )

bot.run("DISCORD_TOKEN")
