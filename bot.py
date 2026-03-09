import json
import discord
from datetime import datetime, timezone

DISCORD_CATEGORY_MAX = 50

with open("config.json") as f:
    config = json.load(f)

guild_config: dict[str, dict] = config["guilds"]

# Track which categories are currently known to be at capacity
categories_at_capacity: set[int] = set()

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Logged in as {client.user} ({client.user.id})")
    print(f"Monitoring {len(guild_config)} guild(s)")


@client.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    guild_id = str(channel.guild.id)
    if guild_id not in guild_config:
        return

    cfg = guild_config[guild_id]
    category = channel.category
    if category is None or str(category.id) != cfg["overflow_category_id"]:
        return

    channel_count = len(category.channels)

    if channel_count >= DISCORD_CATEGORY_MAX and category.id not in categories_at_capacity:
        categories_at_capacity.add(category.id)

        log_channel = channel.guild.get_channel(int(cfg["log_channel_id"]))
        if log_channel is None:
            print(f"[{channel.guild.name}] Log channel not found: {cfg['log_channel_id']}")
            return

        embed = discord.Embed(
            title="Overflow Category Full",
            description=(
                f"The **{category.name}** category has reached the Discord channel limit.\n"
                "No new ticket channels can be created until existing ones are closed."
            ),
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Server", value=channel.guild.name, inline=True)
        embed.add_field(name="Category", value=category.name, inline=True)
        embed.add_field(name="Channels", value=f"{channel_count}/{DISCORD_CATEGORY_MAX}", inline=True)
        embed.set_footer(text="Overflow monitor")

        await log_channel.send(embed=embed)
        print(f"[{channel.guild.name}] Overflow category full: {category.name} ({channel_count}/{DISCORD_CATEGORY_MAX})")


@client.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    guild_id = str(channel.guild.id)
    if guild_id not in guild_config:
        return

    cfg = guild_config[guild_id]
    category = channel.category
    if category is None or str(category.id) != cfg["overflow_category_id"]:
        return

    # After deletion the channel is already removed, so count reflects the new total
    channel_count = len(category.channels)

    if category.id in categories_at_capacity and channel_count < DISCORD_CATEGORY_MAX:
        categories_at_capacity.discard(category.id)

        log_channel = channel.guild.get_channel(int(cfg["log_channel_id"]))
        if log_channel is None:
            return

        embed = discord.Embed(
            title="Overflow Category Has Space",
            description=(
                f"The **{category.name}** category is no longer full.\n"
                "New ticket channels can be created again."
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Server", value=channel.guild.name, inline=True)
        embed.add_field(name="Category", value=category.name, inline=True)
        embed.add_field(name="Channels", value=f"{channel_count}/{DISCORD_CATEGORY_MAX}", inline=True)
        embed.set_footer(text="Overflow monitor")

        await log_channel.send(embed=embed)
        print(f"[{channel.guild.name}] Overflow category freed: {category.name} ({channel_count}/{DISCORD_CATEGORY_MAX})")


client.run(config["token"])
