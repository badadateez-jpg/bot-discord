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

class RulesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Accepter le règlement",
        style=discord.ButtonStyle.success,
        custom_id="accept_rules_button"
    )
    async def accept_rules(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Merci, tu as bien accepté le règlement de Kyoren.",
            ephemeral=True
        )

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
        await member.send(embed=e("⚠ Warn reçu", f"Raison : {reason}"))
    except:
        pass

    if count == 3:
        await member.timeout(timedelta(hours=1), reason="3 warns")
        await log(guild, "⏱ Timeout", f"{member} → 1h")

    elif count == 5:
        await member.timeout(timedelta(hours=72), reason="5 warns")
        await log(guild, "⏱ Timeout", f"{member} → 72h")

    elif count >= 8:
        await member.ban(reason="8 warns")
        await log(guild, "⛔ Ban auto", f"{member}")

# ---------------- EVENTS ---------------- #

@bot.event
async def on_ready():
    bot.add_view(RulesView())
    print(f"BOT CONNECTÉ : {bot.user}")

@bot.event
async def on_member_join(member):
    count = len(member.guild.members)
    await member.guild.system_channel.send(f"Bienvenue {member.mention}, tu es le {count}ème membre")

@bot.event
async def on_member_remove(member):
    await log(member.guild, "👋 Leave", f"{member}")

@bot.event
async def on_message_delete(message):
    if message.author.bot:
        return

    await log(
        message.guild,
        "🗑 Message supprimé",
        f"Auteur: {message.author}\nSalon: #{message.channel}\nContenu: {message.content}"
    )

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.content.lower() == "salut":
        await message.channel.send("salut à toi, quoi de neuf ?")

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
        await ctx.send(f"Cette commande est en cooldown. Réessayez dans {error.retry_after:.1f} secondes.")
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
        "Clairement, oui ! Tu as toutes tes chances de ton côté.",
        "Je pense que oui.",
        "Potentiellement...",
        "Je ne sais pas.",
        "Pas trop..",
        "Non mdrr",
        "Pas dutout, continue a rêver ailleurs !"
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
            f"❤️ {ctx.author.mention} vous embrasse très fort {member.mention} ! ❤️\n"
            f"{member.mention} `a reçu {total_received} kiss au total.`"
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
            f"🫂 {ctx.author.mention} vous sert fort dans ses bras {member.mention} ! 🫂\n"
            f"{member.mention} `a reçu {total_received} hugs au total.`"
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
            f"👋 {ctx.author.mention} vous a collé une claque {member.mention} ! 👋\n"
            f"{member.mention} `a reçu {total_received} slaps au total.`"
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
    await ctx.send(embed=e("Logs", "Salon logs défini"))

@bot.command()
@is_admin()
async def msg(ctx, *, content):
    if not ctx.message.channel_mentions:
        await ctx.send("Aucun salon mentionné.")
        return
    channel = ctx.message.channel_mentions[-1]
    message = content.replace(channel.mention, "").strip()
    if not message:
        await ctx.send("Message vide.")
        return
    await channel.send(message)
    await ctx.send(f"Message envoyé dans {channel.mention}")

@bot.command()
@is_admin()
async def rule(ctx):
    if not ctx.message.channel_mentions:
        await ctx.send("Mentionne un salon, par exemple `!rule #reglement`.")
        return

    channel = ctx.message.channel_mentions[0]
    embed = discord.Embed(
        title="Règlement de Kyoren 🎐",
        description=(
            "Ce présent règlement doit être respecté dans ce serveur sous peine de sanction par le(s) modérateur(rice)(s).\n\n"
            "**Règle 1**\n"
            "Règlement du serveur Kyoren #🇵🇸🇨🇩\n\n"
            "**Respect**\n"
            "Le respect entre membres est obligatoire. Les insultes, le harcèlement, les menaces ou toute forme de discrimination sont interdits.\n\n"
            "**Spam et publicité**\n"
            "Le spam, le flood et la publicité sans autorisation du staff sont interdits.\n\n"
            "**Contenu**\n"
            "Les contenus NSFW, violents, choquants ou illégaux sont interdits. Évitez les débats conflictuels (politique, religion…).\n\n"
            "Pour accéder à l'intégralité du serveur merci de lire le règlement puis d'accepter en cliquant sur le bouton ci-dessous."
        ),
        color=discord.Color.from_rgb(255, 105, 180)
    )

    await channel.send(embed=embed, view=RulesView())
    await ctx.send(f"Règlement envoyé dans {channel.mention}.")

# ---------------- MODERATION ---------------- #

@bot.command()
async def ping(ctx):
    await ctx.send("pong")

def get_help_embed(ctx):
    if ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.manage_guild:
        desc = (
            "**!ping** → pong\n"
            "**!pp @user** → affiche la photo de profil\n"
            "**!8ball question** → pose une question à la boule magique\n"
            "**!kiss @user** → embrasse un membre avec un gif\n"
            "**!hug @user** → fait un calin à un membre avec un gif\n"
            "**!slap @user** → colle une claque à un membre avec un gif\n"
            "**!logs @user** → affiche warns et sanctions\n"
            "**!warn @user raison** → ajoute un warn\n"
            "**!ban @user raison** → ban un membre\n"
            "**!kick @user raison** → kick un membre\n"
            "**!timeout @user durée raison** → timeout\n"
            "**!clear 1-100** → supprimer des messages\n"
            "**!lock** → bloque le salon\n"
            "**!unlock** → débloque le salon\n"
            "**!lockdown** → bloque tout le serveur\n"
            "**!unlockdown** → débloque tout le serveur\n"
            "**!banlist** → affiche les bannis\n"
            "**!setlogs** → définit le salon logs\n"
            "**!giveaway** → lance un giveaway interactif\n"
            "**!msg message #salon** → envoie un message dans un salon\n"
            "**!rule #salon** → envoie le règlement dans un salon\n"
            "**!help** → affiche ce message"
        )
    else:
        desc = (
            "**!ping** → pong\n"
            "**!pp @user** → affiche la photo de profil\n"
            "**!8ball question** → pose une question à la boule magique\n"
            "**!kiss @user** → embrasse un membre avec un gif\n"
            "**!hug @user** → fait un calin à un membre avec un gif\n"
            "**!slap @user** → colle une claque à un membre avec un gif\n"
            "**!help** → affiche ce message"
        )
    return e("📜 Help", desc)

@bot.command()
async def help(ctx):
    await ctx.send(embed=get_help_embed(ctx))
    
@bot.command()
@is_admin()
async def warn(ctx, member: discord.Member, *, reason="Aucune raison"):
    await add_warn(ctx.guild, member, reason)
    await ctx.send(embed=e("⚠ Warn", f"{member.mention}\n{reason}"))

@bot.command()
@is_admin()
async def logs(ctx, member: discord.Member):
    uid = str(member.id)

    warns = data.get("warns", {}).get(uid, [])
    sanc = data.get("sanctions", {}).get(uid, [])

    await ctx.send(embed=e(
        "📜 Logs",
        f"Warns ({len(warns)}):\n" + ("\n".join(warns) if warns else "Aucun") +
        f"\n\nSanctions ({len(sanc)}):\n" + ("\n".join(sanc) if sanc else "Aucune")
    ))

@bot.command()
@is_admin()
async def ban(ctx, member: discord.Member, *, reason="Aucune raison"):
    try:
        await member.send(embed=e("⛔ Ban", f"Raison : {reason}"))
    except:
        pass
    await member.ban(reason=reason)
    await log(ctx.guild, "⛔ Ban", f"{member} | {reason}")
    await ctx.send(embed=e("Ban", f"{member.mention}\n{reason}"))

@bot.command()
@is_admin()
async def kick(ctx, member: discord.Member, *, reason="Aucune raison"):
    try:
        await member.send(embed=e("👢 Kick", f"Raison : {reason}"))
    except:
        pass
    await member.kick(reason=reason)
    await log(ctx.guild, "👢 Kick", f"{member} | {reason}")
    await ctx.send(embed=e("Kick", f"{member.mention}\n{reason}"))

@bot.command()
@is_admin()
async def timeout(ctx, member: discord.Member, duration: str, *, reason="Aucune raison"):
    d = parse_duration(duration)
    if not d:
        return await ctx.send(embed=e("Erreur", "Format invalide (ex: 10m, 1h, 1d)"))

    await member.timeout(d, reason=reason)
    await log(ctx.guild, "⏱ Timeout", f"{member} | {reason}")
    await ctx.send(embed=e("Timeout", f"{member.mention}\n{duration}\n{reason}"))

@bot.command()
@is_admin()
async def clear(ctx, amount: int):
    if amount < 1 or amount > 100:
        return await ctx.send(embed=e("Erreur", "1-100 seulement"))

    deleted = await ctx.channel.purge(limit=amount)
    await log(ctx.guild, "🧹 Clear", f"{len(deleted)} messages supprimés")
    await ctx.send(embed=e("Clear", f"{len(deleted)} messages supprimés"), delete_after=3)

@bot.command()
@is_admin()
async def lock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send(embed=e("Lock", "Salon bloqué"))

@bot.command()
@is_admin()
async def unlock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=None)
    await ctx.send(embed=e("Unlock", "Salon débloqué"))

@bot.command()
@is_admin()
async def lockdown(ctx):
    for c in ctx.guild.text_channels:
        await c.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send(embed=e("Lockdown", "Serveur bloqué"))

@bot.command()
@is_admin()
async def unlockdown(ctx):
    for c in ctx.guild.text_channels:
        await c.set_permissions(ctx.guild.default_role, send_messages=None)
    await ctx.send(embed=e("Unlockdown", "Serveur débloqué"))

@bot.command()
@is_admin()
async def banlist(ctx):
    try:
        bans = await ctx.guild.bans()
    except discord.Forbidden:
        await ctx.send("Le bot n'a pas la permission de voir les bans.")
        return
    except Exception as e:
        await ctx.send("Erreur lors de la récupération des bans.")
        return

    if not bans:
        return await ctx.send(embed=e("Banlist", "Aucun ban"))

    # Limit to 20 bans to avoid embed length limit
    bans_list = list(bans)[:20]
    desc = "\n".join([f"{b.user} - {b.reason or 'Aucune raison'}" for b in bans_list])
    if len(bans) > 20:
        desc += f"\n\nEt {len(bans) - 20} autres..."

    try:
        await ctx.send(embed=e("Banlist", desc))
    except Exception as e:
        await ctx.send("Erreur lors de l'envoi de la liste des bans.")

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

        msg4 = await ctx.send("Dans quel salon ? (mentionnez le salon)")
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

    embed = e("🎉 GIVEAWAY 🎉", f"**Lot :** {prize}\n**Description :** {description}\n**Durée :** {duration_str}\n**Organisé par :** {ctx.author.mention}\n\nRéagissez avec 🎉 pour participer !")
    giveaway_msg = await channel.send(embed=embed)
    await giveaway_msg.add_reaction("🎉")

    # Delete setup messages
    for msg in messages_to_delete:
        try:
            await msg.delete()
        except:
            pass

    end_time = datetime.now() + duration

    # Update embed every 30 minutes with remaining time
    while datetime.now() < end_time:
        remaining = end_time - datetime.now()
        if remaining.total_seconds() <= 0:
            break
        embed.description = f"**Lot :** {prize}\n**Description :** {description}\n**Temps restant :** {format_remaining(remaining)}\n**Organisé par :** {ctx.author.mention}\n\nRéagissez avec 🎉 pour participer !"
        await giveaway_msg.edit(embed=embed)
        await asyncio.sleep(30 * 60)  # 30 minutes

    # Now select winner
    giveaway_msg = await channel.fetch_message(giveaway_msg.id)
    reaction = None
    for r in giveaway_msg.reactions:
        if str(r.emoji) == "🎉":
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
    await channel.send(f"🎉 Félicitations {winner.mention} ! Tu as gagné **{prize}** !")
    try:
        await winner.send(embed=e("🎉 Giveaway gagné !", f"Tu as gagné : {prize}\nDescription : {description}"))
    except:
        pass

# ---------------- RUN ---------------- #

if not DISCORD_TOKEN:
    raise ValueError("Le token Discord est vide.")

bot.run(DISCORD_TOKEN)
