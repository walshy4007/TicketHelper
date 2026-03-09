import json
import os
import asyncpg
import discord
from datetime import datetime, timezone

DISCORD_CATEGORY_MAX = 50

with open("config.json") as f:
    config = json.load(f)

guild_config: dict[str, dict] = config["guilds"]

categories_at_capacity: set[int] = set()

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)

db_pool: asyncpg.Pool | None = None


async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ticket_events (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                guild_id TEXT NOT NULL,
                guild_name TEXT NOT NULL,
                category_id TEXT NOT NULL,
                category_name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                channel_count INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_timestamp ON ticket_events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_events_guild_category ON ticket_events(guild_id, category_id);
        """)
    print("Connected to database")


async def log_event(guild: discord.Guild, category: discord.CategoryChannel, event_type: str, channel_count: int):
    if db_pool is None:
        return
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ticket_events (guild_id, guild_name, category_id, category_name, event_type, channel_count)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            str(guild.id), guild.name, str(category.id), category.name, event_type, channel_count,
        )


async def snapshot_categories():
    count = 0
    for guild_id, cfg in guild_config.items():
        guild = client.get_guild(int(guild_id))
        if guild is None:
            continue
        for category_id in cfg["monitored_categories"]:
            category = guild.get_channel(int(category_id))
            if not isinstance(category, discord.CategoryChannel):
                continue
            await log_event(guild, category, "snapshot", len(category.channels))
            count += 1
    print(f"Snapshotted {count} categories")


@client.event
async def on_ready():
    await init_db()
    await snapshot_categories()
    print(f"Logged in as {client.user} ({client.user.id})")
    print(f"Monitoring {len(guild_config)} guild(s)")


@client.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    guild_id = str(channel.guild.id)
    if guild_id not in guild_config:
        return

    cfg = guild_config[guild_id]
    category = channel.category
    if category is None or str(category.id) not in cfg["monitored_categories"]:
        return

    channel_count = len(category.channels)

    await log_event(channel.guild, category, "open", channel_count)

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
    if category is None or str(category.id) not in cfg["monitored_categories"]:
        return

    channel_count = len(category.channels)

    await log_event(channel.guild, category, "close", channel_count)

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
