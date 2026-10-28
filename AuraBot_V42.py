import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import re
import random
import asyncio
from datetime import timedelta
import aiohttp
import sqlite3
import json
from datetime import datetime, timedelta
from threading import Lock
from functools import wraps

# =========================================================
# CONFIGURATION
# =========================================================
# AuraBot V42 — Hébergement cloud sécurisé (token via variable d’environnement)

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aura_bot.db")

OWNER_ID = 1346162677629714507

ADMIN_ROLES = {
    1522618483035213954,
    1551151199485698058,
    1522694529986334841
}

ANIMATEUR_ROLE = 1539387114821652551

DUEL_TIMEOUT = 300
MEMBER_BATTLE_MAX_PLAYERS = 20

db_lock = Lock()

SQLITE_TIMEOUT = 30

# =========================================================
# INTENTS / BOT
# =========================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# =========================================================
# V32/V33 — PROTECTION GLOBALE DES INTERACTIONS DISCORD
# =========================================================

async def _aurabot_auto_defer(interaction):
    """Dernier filet de sécurité : on ne defer qu'après ~2.2 s.

    IMPORTANT : il ne faut surtout pas defer immédiatement : les callbacks qui
    doivent ouvrir un modal (`send_modal`) ou répondre par `send_message`
    doivent conserver la première réponse Discord.
    """
    try:
        await asyncio.sleep(2.2)
        if not interaction.response.is_done() and not interaction.is_expired():
            await interaction.response.defer()
    except (asyncio.CancelledError, discord.NotFound, discord.HTTPException):
        pass
    except Exception as e:
        print(f"⚠️ Auto-defer interaction impossible : {e}")


def _aurabot_get_interaction(args, kwargs):
    interaction = kwargs.get("interaction")
    if interaction is not None and hasattr(interaction, "response"):
        return interaction
    for arg in args:
        if hasattr(arg, "response") and hasattr(arg, "followup"):
            return arg
    return None


def auto_interaction_response(func):
    """Évite les timeouts d'interactions longues sans modifier leur logique."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        interaction = _aurabot_get_interaction(args, kwargs)
        watchdog = None
        if interaction is not None:
            watchdog = asyncio.create_task(_aurabot_auto_defer(interaction))
        try:
            return await func(*args, **kwargs)
        finally:
            if watchdog is not None:
                watchdog.cancel()
    return wrapper


# Si le watchdog a déjà envoyé le defer, les anciens appels
# interaction.response.send_message/edit_message de la V31 doivent continuer
# à fonctionner au lieu de lever InteractionResponded.
_aurabot_original_send_message = discord.InteractionResponse.send_message
_aurabot_original_edit_message = discord.InteractionResponse.edit_message
_aurabot_original_defer = discord.InteractionResponse.defer


async def _aurabot_safe_send_message(self, *args, **kwargs):
    if self.is_done():
        interaction = getattr(self, "_parent", None)
        if interaction is not None:
            return await interaction.followup.send(*args, **kwargs)
    return await _aurabot_original_send_message(self, *args, **kwargs)


async def _aurabot_safe_edit_message(self, *args, **kwargs):
    if self.is_done():
        interaction = getattr(self, "_parent", None)
        if interaction is not None:
            return await interaction.edit_original_response(*args, **kwargs)
    return await _aurabot_original_edit_message(self, *args, **kwargs)


async def _aurabot_safe_defer(self, *args, **kwargs):
    if self.is_done():
        return None
    return await _aurabot_original_defer(self, *args, **kwargs)


discord.InteractionResponse.send_message = _aurabot_safe_send_message
discord.InteractionResponse.edit_message = _aurabot_safe_edit_message
discord.InteractionResponse.defer = _aurabot_safe_defer

# =========================================================
# ANIMATIONS INDÉPENDANTES DES MEMBER BATTLES
# =========================================================
active_animation_lobbies = {}
persistent_views_registered = False
commands_synced_once = False
active_lg_games = {}
active_bomb_games = {}
active_undercover_games = {}

BOMB_SYLLABLES = [
    "ar", "an", "ba", "be", "bi", "bo", "ca", "che", "co",
    "de", "di", "do", "en", "er", "es", "eu", "la", "le",
    "li", "lo", "ma", "me", "mi", "mo", "na", "ne", "ni",
    "no", "on", "ou", "pa", "pe", "pi", "po", "ra", "re",
    "ri", "ro", "sa", "se", "si", "so", "ta", "te", "ti",
    "to", "un", "ve", "vi"
]

class AnimationLobby:
    def __init__(self, channel, game_type, min_players, max_players):
        self.channel = channel
        self.channel_id = channel.id
        self.game_type = game_type
        self.min_players = min_players
        self.max_players = max_players
        self.players = {}
        self.message = None
        self.running = False
        self.finished = False
        self.started_by = None

class LGGame:
    def __init__(self, lobby):
        self.channel = lobby.channel
        self.channel_id = lobby.channel_id
        self.players = dict(lobby.players)
        self.alive = set(self.players.keys())
        self.roles = {}
        self.actions = {"kill": {}, "heal": {}, "inspect": {}, "vote": {}}

    def winner(self):
        wolves = sum(1 for uid in self.alive if self.roles.get(uid) == "Loup-Garou")
        villagers = sum(1 for uid in self.alive if self.roles.get(uid) != "Loup-Garou")
        if wolves == 0:
            return "village"
        if wolves >= villagers:
            return "wolves"
        return None

def reward_animation_winner(user_id, amount=10):
    try:
        data = get_user(user_id)
        if data is not None:
            data["aura"] = int(data.get("aura", 0)) + int(amount)
            save_user(data)
    except Exception as e:
        print("⚠️ Récompense animation impossible :", e)

class AnimationLobbyView(discord.ui.View):
    def __init__(self, lobby):
        super().__init__(timeout=600)
        self.lobby = lobby

    @auto_interaction_response
    async def refresh(self, interaction):
        if self.lobby.finished:
            return
        title = {
            "lg": "🐺 Loup-Garou",
            "bomb": "💣 BombParty",
            "undercover": "🕵️ Undercover"
        }.get(self.lobby.game_type, "🎮 Animation")
        names = [u.mention for u in self.lobby.players.values()]
        desc = (
            f"**Joueurs : {len(names)}/{self.lobby.max_players}**\n"
            f"Minimum : **{self.lobby.min_players}**\n\n"
            + (", ".join(names) if names else "Aucun joueur pour le moment")
        )
        embed = discord.Embed(title=title, description=desc, color=discord.Color.blurple())
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Participer", style=discord.ButtonStyle.success, emoji="🎮")
    @auto_interaction_response
    async def join(self, interaction, button):
        if self.lobby.running:
            return await interaction.response.send_message("❌ La partie a déjà commencé.", ephemeral=True)
        if interaction.user.id in self.lobby.players:
            return await interaction.response.send_message("❌ Tu participes déjà.", ephemeral=True)
        if len(self.lobby.players) >= self.lobby.max_players:
            return await interaction.response.send_message("❌ Le lobby est complet.", ephemeral=True)
        self.lobby.players[interaction.user.id] = interaction.user
        await self.refresh(interaction)

    @discord.ui.button(label="Quitter", style=discord.ButtonStyle.danger, emoji="🚪")
    @auto_interaction_response
    async def leave(self, interaction, button):
        if self.lobby.running:
            return await interaction.response.send_message("❌ La partie a déjà commencé.", ephemeral=True)
        if interaction.user.id not in self.lobby.players:
            return await interaction.response.send_message("❌ Tu ne participes pas.", ephemeral=True)
        del self.lobby.players[interaction.user.id]
        await self.refresh(interaction)

    @discord.ui.button(label="Lancer", style=discord.ButtonStyle.primary, emoji="🚀")
    @auto_interaction_response
    async def start(self, interaction, button):
        if not can_memberbattle(interaction.user):
            return await interaction.response.send_message("❌ Seuls les administrateurs et animateurs peuvent lancer l'animation.", ephemeral=True)
        if self.lobby.running:
            return await interaction.response.send_message("❌ La partie est déjà lancée.", ephemeral=True)
        if len(self.lobby.players) < self.lobby.min_players:
            return await interaction.response.send_message(f"❌ Il faut au moins **{self.lobby.min_players} joueurs**.", ephemeral=True)
        self.lobby.running = True
        self.lobby.started_by = interaction.user.id
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="", embed=discord.Embed(title="🎮 Partie en cours", description="L'animation démarre...", color=discord.Color.green()), view=self)
        active_animation_lobbies.pop(self.lobby.channel_id, None)
        if self.lobby.game_type == "lg":
            asyncio.create_task(run_loup_garou(self.lobby))
        elif self.lobby.game_type == "bomb":
            asyncio.create_task(run_bombparty(self.lobby))
        else:
            asyncio.create_task(run_undercover(self.lobby))


# =========================================================
# DATABASE
# =========================================================

def column_exists(cursor, table, column):
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def add_column_if_missing(cursor, table, column, definition):
    if not column_exists(cursor, table, column):
        cursor.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def db_connect():
    """Connexion SQLite avec attente automatique si la base est momentanément verrouillée."""
    conn = sqlite3.connect(DB_FILE, timeout=SQLITE_TIMEOUT)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    try:
        with db_lock:
            conn = db_connect()
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    coins INTEGER DEFAULT 100,
                    aura INTEGER DEFAULT 0,
                    xp INTEGER DEFAULT 0,
                    level INTEGER DEFAULT 1,
                    elo INTEGER DEFAULT 1000,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    kills INTEGER DEFAULT 0,
                    deaths INTEGER DEFAULT 0,
                    potions INTEGER DEFAULT 2,
                    sword INTEGER DEFAULT 1,
                    armor INTEGER DEFAULT 1,
                    last_daily TEXT,
                    last_guess TEXT,
                    created_at TEXT
                )
            """)

            # Colonnes ajoutées pour la nouvelle économie.
            new_columns = {
                "reputation": "INTEGER DEFAULT 0",
                "last_rep": "TEXT",
                "last_weekly": "TEXT",
                "last_monthly": "TEXT",
                "daily_streak": "INTEGER DEFAULT 0",
                "weekly_streak": "INTEGER DEFAULT 0",
                "monthly_streak": "INTEGER DEFAULT 0",
                "last_daily_claim": "TEXT",
                "last_weekly_claim": "TEXT",
                "last_monthly_claim": "TEXT",
                "tickets": "INTEGER DEFAULT 0",
                "prestige": "INTEGER DEFAULT 0",
                "missions_completed": "INTEGER DEFAULT 0",
                "perfect_used": "INTEGER DEFAULT 0",
                "chaos_used": "INTEGER DEFAULT 0",
                "joker_used": "INTEGER DEFAULT 0",
                "destin_used": "INTEGER DEFAULT 0",
                "timeback_used": "INTEGER DEFAULT 0",
                "prediction_used": "INTEGER DEFAULT 0",
                "influence_used": "INTEGER DEFAULT 0",
                "last_chaos": "TEXT",
                "last_perfect": "TEXT",
                "last_destin": "TEXT",
                "last_timeback": "TEXT",
                "last_prediction": "TEXT",
                "last_wheel": "TEXT",
                "last_reward": "TEXT",
                "last_reward_type": "TEXT",
                "tutorial_done": "INTEGER DEFAULT 0"
            }

            for name, definition in new_columns.items():
                add_column_if_missing(
                    cursor,
                    "users",
                    name,
                    definition
                )

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS duels (
                    duel_id TEXT PRIMARY KEY,
                    challenger_id TEXT,
                    target_id TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT,
                    started_at TEXT,
                    finished_at TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reputation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    giver_id TEXT,
                    receiver_id TEXT,
                    created_at TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS talents (
                    user_id TEXT,
                    talent TEXT,
                    purchased_at TEXT,
                    PRIMARY KEY(user_id, talent)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS missions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    mission_key TEXT,
                    progress INTEGER DEFAULT 0,
                    target INTEGER DEFAULT 1,
                    expires_at TEXT,
                    completed INTEGER DEFAULT 0,
                    reward_coins INTEGER DEFAULT 0,
                    reward_rep INTEGER DEFAULT 0,
                    reward_tickets INTEGER DEFAULT 0
                )
            """)


            # Statistiques des Member Battles : conservées même lors d'un /reseteco.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS member_battle_wins (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    wins INTEGER DEFAULT 0,
                    PRIMARY KEY(guild_id, user_id)
                )
            """)

            # Confessions : l'auteur reste caché publiquement mais est conservé
            # côté bot pour les reports administrateurs.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS server_members (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    xp INTEGER DEFAULT 0,
                    level INTEGER DEFAULT 1,
                    money INTEGER DEFAULT 0,
                    last_xp_message TEXT,
                    last_money_message TEXT,
                    PRIMARY KEY(guild_id, user_id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS custom_shop_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    price INTEGER DEFAULT 0,
                    reward_type TEXT NOT NULL,
                    reward_value TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    shop_id INTEGER,
                    stock INTEGER DEFAULT -1,
                    max_per_user INTEGER DEFAULT 0,
                    min_level INTEGER DEFAULT 0,
                    required_role_id TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS custom_commands (
                    guild_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    response TEXT NOT NULL,
                    created_at TEXT,
                    PRIMARY KEY(guild_id, name)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS confessions (
                    message_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    thread_id TEXT,
                    created_at TEXT
                )
            """)

            # Empêche un double message arrivée/départ si deux instances du bot
            # reçoivent le même événement dans un court délai.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS member_event_dedupe (
                    event_key TEXT PRIMARY KEY,
                    created_at REAL NOT NULL
                )
            """)

            # Configuration serveur façon panneau DraftBot.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS guild_config (
                    guild_id TEXT PRIMARY KEY,
                    welcome_channel_id TEXT,
                    welcome_message TEXT,
                    leave_channel_id TEXT,
                    leave_message TEXT,
                    autorole_id TEXT,
                    log_channel_id TEXT,
                    modlog_channel_id TEXT,
                    suggestions_channel_id TEXT,
                    confession_channel_id TEXT,
                    confession_report_channel_id TEXT,
                    ticket_category_id TEXT,
                    ticket_log_channel_id TEXT,
                    ticket_staff_role_id TEXT,
                    ticket_staff_role_ids TEXT,
                    ticket_panel_channel_id TEXT,
                    ticket_panel_message_id TEXT,
                    ticket_categories TEXT,
                    temp_voice_enabled INTEGER DEFAULT 0,
                    temp_voice_create_channel_id TEXT,
                    temp_voice_category_id TEXT,
                    bot_server_name TEXT,
                    bot_server_avatar_url TEXT,
                    bot_server_description TEXT,
                    bot_server_bio TEXT,
                    bot_server_banner_url TEXT,
                    bot_server_color TEXT,
                    bot_server_footer TEXT,
                    anti_links INTEGER DEFAULT 0,
                    anti_spam INTEGER DEFAULT 0,
                    commands_channel_id TEXT,
                    welcome_enabled INTEGER DEFAULT 1,
                    leave_enabled INTEGER DEFAULT 1,
                    ticket_panel_title TEXT,
                    ticket_panel_description TEXT,
                    ticket_button_label TEXT,
                    ticket_open_title TEXT,
                    ticket_open_description TEXT,
                    ticket_close_label TEXT
                )
            """)

            # Migration des réglages ajoutés après la première version du /config.
            for name, definition in {
                "giveaway_reward_bot": "TEXT",
                "giveaway_reward_custom": "TEXT",
                "confession_channel_id": "TEXT",
                "confession_report_channel_id": "TEXT",
                "confession_panel_message_id": "TEXT",
                "ticket_category_id": "TEXT",
                "ticket_log_channel_id": "TEXT",
                "ticket_staff_role_id": "TEXT",
                "ticket_staff_role_ids": "TEXT",
                "ticket_panel_channel_id": "TEXT",
                "ticket_panel_message_id": "TEXT",
                "ticket_categories": "TEXT",
                "temp_voice_enabled": "INTEGER DEFAULT 0",
                "temp_voice_create_channel_id": "TEXT",
                "temp_voice_category_id": "TEXT",
                "bot_server_name": "TEXT",
                "bot_server_avatar_url": "TEXT",
                "bot_server_description": "TEXT",
                "bot_server_bio": "TEXT",
                "bot_server_banner_url": "TEXT",
                "bot_server_color": "TEXT",
                "bot_server_footer": "TEXT",
                "welcome_enabled": "INTEGER DEFAULT 1",
                "leave_enabled": "INTEGER DEFAULT 1",
                "ticket_panel_title": "TEXT",
                "ticket_panel_description": "TEXT",
                "ticket_button_label": "TEXT",
                "ticket_open_title": "TEXT",
                "ticket_open_description": "TEXT",
                "ticket_close_label": "TEXT",
                "animations_enabled": "INTEGER DEFAULT 1",
                "animations_channel_id": "TEXT",
                "animation_max_players": "INTEGER DEFAULT 20",
                "lg_wolves_percent": "INTEGER DEFAULT 25",
                "lg_night_seconds": "INTEGER DEFAULT 45",
                "lg_vote_seconds": "INTEGER DEFAULT 45",
                "lg_doctor_enabled": "INTEGER DEFAULT 1",
                "lg_seer_enabled": "INTEGER DEFAULT 1",
                "bomb_timer": "INTEGER DEFAULT 15",
                "bomb_lives": "INTEGER DEFAULT 3",
                "bomb_syllables": "TEXT",
                "suggestion_up_reaction": "TEXT DEFAULT '👍'",
                "suggestion_down_reaction": "TEXT DEFAULT '👎'",
                "counter_correct_reaction": "TEXT DEFAULT '✅'",
                "counter_wrong_reaction": "TEXT DEFAULT '❌'",
                "bomb_correct_reaction": "TEXT DEFAULT '✅'",
                "bomb_wrong_reaction": "TEXT DEFAULT '❌'",
                "undercover_white_enabled": "INTEGER DEFAULT 1",
                "undercover_pairs": "TEXT",
                "counter_channel_id": "TEXT",
                "counter_enabled": "INTEGER DEFAULT 0",
                "counter_count": "INTEGER DEFAULT 0",
                "counter_last_user_id": "TEXT",
                "counter_mode": "TEXT DEFAULT 'every'",
                "counter_interval": "INTEGER DEFAULT 10",
                "counter_exact": "INTEGER DEFAULT 0",
                "counter_reward_type": "TEXT DEFAULT 'money'",
                "counter_reward_amount": "INTEGER DEFAULT 0",
                "counter_wrong_action": "TEXT DEFAULT 'none'",
                "counter_wrong_mute_seconds": "INTEGER DEFAULT 60",
                "counter_wrong_fine": "INTEGER DEFAULT 0",
                "level_enabled": "INTEGER DEFAULT 1",
                "level_xp_per_message": "INTEGER DEFAULT 5",
                "level_xp_cooldown": "INTEGER DEFAULT 60",
                "level_money_per_level": "INTEGER DEFAULT 100",
                "level_reward_type": "TEXT DEFAULT 'none'",
                "level_reward_amount": "INTEGER DEFAULT 0",
                "level_xp_base": "INTEGER DEFAULT 100",
                "server_currency_name": "TEXT DEFAULT 'Argent'",
                "server_currency_emoji": "TEXT DEFAULT '💵'" ,
                "custom_shop_items": "TEXT DEFAULT '[]'",
                "money_enabled": "INTEGER DEFAULT 1",
                "money_min": "INTEGER DEFAULT 15",
                "money_max": "INTEGER DEFAULT 25",
                "money_cooldown": "INTEGER DEFAULT 30",
                "money_long_message_enabled": "INTEGER DEFAULT 0",
                "money_long_message_length": "INTEGER DEFAULT 250",
                "money_long_message_multiplier": "REAL DEFAULT 2.0",
                "last_money_message": "TEXT",
            }.items():
                add_column_if_missing(cursor, "guild_config", name, definition)

            # Migration de la colonne de cooldown argent pour les bases existantes.
            add_column_if_missing(cursor, "server_members", "last_money_message", "TEXT")

            # Boutique avancée : plusieurs boutiques, stock, conditions et achats.
            for name, definition in {
                "shop_id": "INTEGER",
                "stock": "INTEGER DEFAULT -1",
                "max_per_user": "INTEGER DEFAULT 0",
                "min_level": "INTEGER DEFAULT 0",
                "required_role_id": "TEXT",
            }.items():
                add_column_if_missing(cursor, "custom_shop_items", name, definition)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS custom_shops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    emoji TEXT DEFAULT '🛒',
                    style TEXT DEFAULT 'classic',
                    banner_url TEXT DEFAULT '',
                    sort_mode TEXT DEFAULT 'newest',
                    required_role_id TEXT,
                    forbidden_role_id TEXT,
                    min_level INTEGER DEFAULT 0,
                    enabled INTEGER DEFAULT 1
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS shop_purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    shop_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    quantity INTEGER DEFAULT 1,
                    purchased_at TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS temp_voice_channels (
                    channel_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    created_at TEXT,
                    control_message_id TEXT
                )
            """)

            # Serveurs ayant ajouté AuraBot : utilisé par /serveur (propriétaire uniquement).
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bot_servers (
                    guild_id TEXT PRIMARY KEY,
                    guild_name TEXT,
                    added_by_id TEXT,
                    added_by_name TEXT,
                    joined_at TEXT
                )
            """)
            for name, definition in {
                "temp_voice_control_channel_id": "TEXT",
            }.items():
                add_column_if_missing(cursor, "temp_voice_channels", name, definition)

            # Progression complémentaire V9 : séries, titres, succès et quêtes.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS player_progress (
                    user_id TEXT PRIMARY KEY,
                    duel_streak INTEGER DEFAULT 0,
                    best_duel_streak INTEGER DEFAULT 0,
                    elo_max INTEGER DEFAULT 1000,
                    selected_title TEXT DEFAULT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS achievements (
                    user_id TEXT NOT NULL,
                    achievement TEXT NOT NULL,
                    unlocked_at TEXT,
                    PRIMARY KEY(user_id, achievement)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS quest_progress (
                    user_id TEXT NOT NULL,
                    quest_date TEXT NOT NULL,
                    quest_key TEXT NOT NULL,
                    progress INTEGER DEFAULT 0,
                    claimed INTEGER DEFAULT 0,
                    PRIMARY KEY(user_id, quest_date, quest_key)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS giveaways (
                    giveaway_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, message_id TEXT,
                    title TEXT NOT NULL, reward TEXT NOT NULL, ends_at TEXT NOT NULL,
                    winners_count INTEGER DEFAULT 1, required_role_id TEXT, forbidden_role_id TEXT,
                    min_level INTEGER DEFAULT 0, max_level INTEGER, status TEXT DEFAULT 'active',
                    winner_ids TEXT, created_by TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS giveaway_entries (
                    giveaway_id INTEGER NOT NULL, user_id TEXT NOT NULL, joined_at TEXT,
                    PRIMARY KEY(giveaway_id, user_id)
                )
            """)

            conn.commit()
            conn.close()

        print("✅ Base de données initialisée.")

    except Exception as e:
        print(f"❌ Erreur initialisation DB : {e}")


def get_user(user_id):
    user_id = str(user_id)

    try:
        with db_lock:
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,)
            )
            row = cursor.fetchone()

            if row is None:
                now = datetime.now().isoformat()

                cursor.execute("""
                    INSERT INTO users (
                        user_id, coins, aura, xp, level, elo,
                        wins, losses, kills, deaths, potions,
                        sword, armor, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    user_id, 100, 0, 0, 1, 1000,
                    0, 0, 0, 0, 2, 1, 1, now
                ))
                conn.commit()

                # Récupère ensuite la ligne complète, y compris
                # les nouvelles colonnes.
                cursor.execute(
                    "SELECT * FROM users WHERE user_id = ?",
                    (user_id,)
                )
                row = cursor.fetchone()

            columns = [d[0] for d in cursor.description]
            user = dict(zip(columns, row))
            user = normalize_user_data(user)

            conn.close()
            return user

    except Exception as e:
        print(f"❌ Erreur get_user : {e}")
        return None


def normalize_user_data(user):
    """Garantit que les ressources économie ont toujours une valeur numérique."""
    if user is None:
        return None

    numeric_defaults = {
        "coins": 100, "aura": 0, "xp": 0, "level": 1, "elo": 1000,
        "wins": 0, "losses": 0, "kills": 0, "deaths": 0, "potions": 0,
        "sword": 1, "armor": 1, "reputation": 0, "tickets": 0,
        "prestige": 0, "daily_streak": 0, "weekly_streak": 0,
        "monthly_streak": 0, "missions_completed": 0,
        "perfect_used": 0, "chaos_used": 0, "joker_used": 0,
        "destin_used": 0, "timeback_used": 0, "prediction_used": 0,
        "influence_used": 0
    }
    for key, default in numeric_defaults.items():
        value = user.get(key)
        try:
            user[key] = int(value) if value is not None else default
        except (TypeError, ValueError):
            user[key] = default
        if key in {
            "coins", "aura", "xp", "elo", "wins", "losses", "kills",
            "deaths", "potions", "sword", "armor", "reputation", "tickets",
            "prestige", "daily_streak", "weekly_streak", "monthly_streak",
            "missions_completed", "perfect_used", "chaos_used", "joker_used",
            "destin_used", "timeback_used", "prediction_used", "influence_used"
        }:
            user[key] = max(0, user[key])
    user["level"] = max(1, user["level"])
    user["tutorial_done"] = 1 if int(user.get("tutorial_done", 0) or 0) else 0
    return user


def tutorial_done(user_id):
    user = get_user(user_id)
    return bool(user and int(user.get("tutorial_done", 0)) == 1)


@auto_interaction_response
async def require_tutorial(interaction):
    """Bloque les commandes économie tant que /tuto n'est pas terminé."""
    user = get_user(interaction.user.id)
    if user is None:
        await interaction.response.send_message(
            "❌ Impossible de récupérer ton profil.", ephemeral=True
        )
        return False

    if int(user.get("tutorial_done", 0)) == 1:
        return True

    await interaction.response.send_message(
        "📖 **Avant d'utiliser l'économie d'AuraBot, termine le tutoriel !**\n\n"
        "Utilise **/tuto**, puis appuie sur **J'ai terminé**.\n"
        "🔓 Une fois terminé, tes commandes d'économie seront débloquées.",
        ephemeral=True
    )
    return False


def save_user(user):
    if user is None:
        return False

    user = normalize_user_data(user)

    try:
        with db_lock:
            conn = db_connect()
            cursor = conn.cursor()

            # On ne met à jour que les colonnes connues par le bot.
            cursor.execute("""
                UPDATE users SET
                    coins=?,
                    aura=?,
                    xp=?,
                    level=?,
                    elo=?,
                    wins=?,
                    losses=?,
                    kills=?,
                    deaths=?,
                    potions=?,
                    sword=?,
                    armor=?,
                    last_daily=?,
                    last_guess=?,
                    reputation=?,
                    last_rep=?,
                    last_weekly=?,
                    last_monthly=?,
                    daily_streak=?,
                    weekly_streak=?,
                    monthly_streak=?,
                    last_daily_claim=?,
                    last_weekly_claim=?,
                    last_monthly_claim=?,
                    tickets=?,
                    prestige=?,
                    missions_completed=?,
                    perfect_used=?,
                    chaos_used=?,
                    joker_used=?,
                    destin_used=?,
                    timeback_used=?,
                    prediction_used=?,
                    influence_used=?,
                    last_chaos=?,
                    last_perfect=?,
                    last_destin=?,
                    last_timeback=?,
                    last_prediction=?,
                    last_wheel=?,
                    last_reward=?,
                    last_reward_type=?,
                    tutorial_done=?
                WHERE user_id=?
            """, (
                user["coins"],
                user["aura"],
                user["xp"],
                user["level"],
                user["elo"],
                user["wins"],
                user["losses"],
                user["kills"],
                user["deaths"],
                user["potions"],
                user["sword"],
                user["armor"],
                user.get("last_daily"),
                user.get("last_guess"),
                user.get("reputation", 0),
                user.get("last_rep"),
                user.get("last_weekly"),
                user.get("last_monthly"),
                user.get("daily_streak", 0),
                user.get("weekly_streak", 0),
                user.get("monthly_streak", 0),
                user.get("last_daily_claim"),
                user.get("last_weekly_claim"),
                user.get("last_monthly_claim"),
                user.get("tickets", 0),
                user.get("prestige", 0),
                user.get("missions_completed", 0),
                user.get("perfect_used", 0),
                user.get("chaos_used", 0),
                user.get("joker_used", 0),
                user.get("destin_used", 0),
                user.get("timeback_used", 0),
                user.get("prediction_used", 0),
                user.get("influence_used", 0),
                user.get("last_chaos"),
                user.get("last_perfect"),
                user.get("last_destin"),
                user.get("last_timeback"),
                user.get("last_prediction"),
                user.get("last_wheel"),
                user.get("last_reward"),
                user.get("last_reward_type"),
                user.get("tutorial_done", 0),
                user["user_id"]
            ))

            conn.commit()
            conn.close()

        return True

    except Exception as e:
        print(f"❌ Erreur save_user : {e}")
        return False


# =========================================================
# XP
# =========================================================

def add_xp_to_user(user, amount):
    if user is None:
        return 0
    user["xp"] = int(user.get("xp", 0)) + int(amount)
    levels_gained = 0
    while user["xp"] >= max(1, int(user.get("level", 1)) * 100):
        user["xp"] -= max(1, int(user.get("level", 1)) * 100)
        user["level"] += 1
        levels_gained += 1
    return levels_gained


def add_xp(user_id, amount):
    user = get_user(user_id)
    if user is None:
        return 0
    levels = add_xp_to_user(user, amount)
    save_user(user)
    return levels


def get_server_member(guild_id, user_id):
    guild_id, user_id = str(guild_id), str(user_id)
    with db_lock:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT guild_id,user_id,xp,level,money,last_xp_message,last_money_message FROM server_members WHERE guild_id=? AND user_id=?", (guild_id,user_id))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO server_members(guild_id,user_id,xp,level,money,last_xp_message,last_money_message) VALUES(?,?,?,?,?,?,?)", (guild_id,user_id,0,1,0,None,None))
            conn.commit()
            row = (guild_id,user_id,0,1,0,None,None)
        conn.close()
    return dict(zip(["guild_id","user_id","xp","level","money","last_xp_message","last_money_message"], row))


def save_server_member(data):
    with db_lock:
        conn = db_connect()
        conn.execute("UPDATE server_members SET xp=?,level=?,money=?,last_xp_message=?,last_money_message=? WHERE guild_id=? AND user_id=?", (int(data.get("xp",0)),int(data.get("level",1)),int(data.get("money",0)),data.get("last_xp_message"),data.get("last_money_message"),str(data["guild_id"]),str(data["user_id"])))
        conn.commit(); conn.close()


def server_currency(cfg):
    return (cfg.get("server_currency_emoji") or "💵") + " " + (cfg.get("server_currency_name") or "Argent")


def level_reward_label(guild_id, user_id, cfg):
    rtype = str(cfg.get("level_reward_type") or "none").lower()
    amount = max(0, int(cfg.get("level_reward_amount", 0) or 0))
    if amount <= 0 or rtype == "none":
        return ""
    if rtype == "argent":
        return f"+{amount} {server_currency(cfg)}"
    if rtype == "aura":
        return f"+{amount} Aura"
    if rtype == "jetons":
        return f"+{amount} Jetons"
    return ""


def apply_level_reward(guild_id, user_id, cfg):
    rtype = str(cfg.get("level_reward_type") or "none").lower()
    amount = max(0, int(cfg.get("level_reward_amount", 0) or 0))
    if amount <= 0 or rtype == "none":
        return ""
    if rtype == "argent":
        member = get_server_member(guild_id, user_id)
        member["money"] += amount
        save_server_member(member)
        return f"+{amount} {server_currency(cfg)}"
    user = get_user(user_id)
    if user is None:
        return ""
    if rtype == "aura":
        user["aura"] += amount
        save_user(user)
        return f"+{amount} Aura"
    if rtype == "jetons":
        user["coins"] += amount
        save_user(user)
        return f"+{amount} Jetons"
    return ""


def add_server_message_xp(guild_id, user_id, cfg):
    if not cfg.get("level_enabled", 1):
        return 0, 0, []
    member = get_server_member(guild_id, user_id)
    now = datetime.now()
    cooldown = max(0, int(cfg.get("level_xp_cooldown", 60) or 0))
    last = member.get("last_xp_message")
    if last and cooldown:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < cooldown:
                return 0, member["level"], []
        except Exception:
            pass
    member["last_xp_message"] = now.isoformat()
    member["xp"] += max(0, int(cfg.get("level_xp_per_message", 5) or 0))
    gained = 0
    rewards = []
    while member["xp"] >= max(1, int(cfg.get("level_xp_base", 100) or 100) * member["level"]):
        member["xp"] -= max(1, int(cfg.get("level_xp_base", 100) or 100) * member["level"])
        member["level"] += 1
        gained += 1
        # Ancien réglage conservé pour compatibilité : si aucune nouvelle
        # récompense n'est configurée, l'ancien gain d'argent par niveau reste actif.
        if str(cfg.get("level_reward_type") or "none").lower() == "none":
            old_money = max(0, int(cfg.get("level_money_per_level", 100) or 0))
            if old_money:
                member["money"] += old_money
                rewards.append(f"+{old_money} {server_currency(cfg)}")
        else:
            rtype = str(cfg.get("level_reward_type") or "none").lower()
            amount = max(0, int(cfg.get("level_reward_amount", 0) or 0))
            if amount > 0:
                if rtype == "argent":
                    member["money"] += amount
                    rewards.append(f"+{amount} {server_currency(cfg)}")
                elif rtype in {"aura", "jetons"}:
                    user = get_user(user_id)
                    if user is not None:
                        if rtype == "aura":
                            user["aura"] += amount
                            rewards.append(f"+{amount} Aura")
                        else:
                            user["coins"] += amount
                            rewards.append(f"+{amount} Jetons")
                        save_user(user)
    save_server_member(member)
    return gained, member["level"], rewards


def add_server_message_money(guild_id, user_id, cfg, message_length=0):
    """Verse l'argent par message avec cooldown, plage aléatoire et option messages longs."""
    if not cfg.get("money_enabled", 1):
        return 0
    member = get_server_member(guild_id, user_id)
    now = datetime.now()
    cooldown = max(0, int(cfg.get("money_cooldown", 30) or 0))
    last = member.get("last_money_message")
    if last and cooldown:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < cooldown:
                return 0
        except Exception:
            pass
    low = max(0, int(cfg.get("money_min", 15) or 0))
    high = max(low, int(cfg.get("money_max", 25) or low))
    amount = random.randint(low, high) if high >= low else low
    if cfg.get("money_long_message_enabled") and message_length >= max(1, int(cfg.get("money_long_message_length",250) or 250)):
        amount = int(round(amount * float(cfg.get("money_long_message_multiplier",2.0) or 2.0)))
    member["money"] += max(0, amount)
    member["last_money_message"] = now.isoformat()
    save_server_member(member)
    return max(0, amount)


# =========================================================
# PERMISSIONS
# =========================================================

def is_admin(member):
    if member.id == OWNER_ID:
        return True

    if member.guild_permissions.administrator:
        return True

    return any(role.id in ADMIN_ROLES for role in member.roles)


def can_memberbattle(member):
    if is_admin(member):
        return True

    return any(
        role.id == ANIMATEUR_ROLE
        for role in member.roles
    )


# =========================================================
# TIME / STREAK HELPERS
# =========================================================

def cooldown_remaining(last_value, cooldown):
    if not last_value:
        return None

    try:
        last = datetime.fromisoformat(last_value)
    except Exception:
        return None

    remaining = cooldown - (datetime.now() - last)

    if remaining.total_seconds() <= 0:
        return None

    return remaining


def format_remaining(delta):
    seconds = max(0, int(delta.total_seconds()))
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60

    parts = []
    if days:
        parts.append(f"{days}j")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}min")

    return " ".join(parts)


def start_of_week(dt):
    d = dt - timedelta(days=dt.weekday())
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def start_of_month(dt):
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def update_claim_streak(user, kind):
    now = datetime.now()

    if kind == "daily":
        last = user.get("last_daily_claim")
        streak_key = "daily_streak"
        if last:
            try:
                last_date = datetime.fromisoformat(last).date()
                delta_days = (now.date() - last_date).days
                if delta_days == 1:
                    user[streak_key] += 1
                elif delta_days != 0:
                    user[streak_key] = 1
            except Exception:
                user[streak_key] = 1
        else:
            user[streak_key] = 1

        user["last_daily_claim"] = now.isoformat()

    elif kind == "weekly":
        last = user.get("last_weekly_claim")
        streak_key = "weekly_streak"
        current_week = start_of_week(now)

        if last:
            try:
                last_week = start_of_week(datetime.fromisoformat(last))
                diff = (current_week - last_week).days // 7
                if diff == 1:
                    user[streak_key] += 1
                elif diff != 0:
                    user[streak_key] = 1
            except Exception:
                user[streak_key] = 1
        else:
            user[streak_key] = 1

        user["last_weekly_claim"] = now.isoformat()

    elif kind == "monthly":
        last = user.get("last_monthly_claim")

        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                current_index = now.year * 12 + now.month
                last_index = last_dt.year * 12 + last_dt.month
                diff = current_index - last_index

                if diff == 1:
                    user["monthly_streak"] += 1
                elif diff != 0:
                    user["monthly_streak"] = 1
            except Exception:
                user["monthly_streak"] = 1
        else:
            user["monthly_streak"] = 1

        user["last_monthly_claim"] = now.isoformat()


# =========================================================
# TALENTS
# =========================================================

TALENTS = {
    "main_chanceuse": {
        "name": "🍀 Main Chanceuse",
        "cost": 15,
        "category": "🍀 Chance",
        "tier": 1,
        "requires": None,
        "description": (
            "Une fois par jour, une récompense quotidienne, "
            "hebdomadaire ou mensuelle peut déclencher une "
            "roue bonus spéciale."
        )
    },
    "double_ou_rien": {
        "name": "🎰 Double ou Rien",
        "cost": 30,
        "category": "🍀 Chance",
        "tier": 2,
        "requires": "main_chanceuse",
        "description": (
            "Après certaines récompenses, le bot peut te proposer "
            "un risque : réussir double la récompense bonus, "
            "échouer fait perdre uniquement le bonus risqué."
        )
    },
    "joker": {
        "name": "🃏 Joker",
        "cost": 60,
        "category": "🍀 Chance",
        "tier": 3,
        "requires": "double_ou_rien",
        "description": (
            "Une fois par semaine, transforme une récompense "
            "récente éligible en récompense aléatoire supérieure."
        )
    },
    "destin": {
        "name": "🎲 Destin",
        "cost": 100,
        "category": "🍀 Chance",
        "tier": 4,
        "requires": "joker",
        "description": (
            "Une fois par semaine, choisis 1 coffre parmi 3 "
            "sans connaître leur contenu."
        )
    },
    "defi_parfait": {
        "name": "🎯 Défi Parfait",
        "cost": 50,
        "category": "🎮 Mini-jeux",
        "tier": 1,
        "requires": None,
        "description": (
            "Débloque une tentative spéciale de mini-jeu "
            "avec une condition parfaite et une récompense élevée."
        )
    },
    "mode_chaos": {
        "name": "👾 Mode Chaos",
        "cost": 75,
        "category": "🎮 Mini-jeux",
        "tier": 2,
        "requires": "defi_parfait",
        "description": (
            "Débloque une variante rare et imprévisible des "
            "mini-jeux avec des règles différentes."
        )
    },
    "remonter_le_temps": {
        "name": "⏳ Remonter le Temps",
        "cost": 100,
        "category": "🤝 Réputation",
        "tier": 3,
        "requires": "reconnaissance",
        "description": (
            "Une fois par mois, permet d'annuler une action "
            "économique récente éligible."
        )
    },
    "prediction": {
        "name": "🔮 Prédiction",
        "cost": 250,
        "category": "🍀 Chance",
        "tier": 5,
        "requires": "destin",
        "description": (
            "Une fois par semaine, révèle une information "
            "partielle sur plusieurs récompenses avant ton choix."
        )
    },
    "influence": {
        "name": "🤝 Influence",
        "cost": 40,
        "category": "🤝 Réputation",
        "tier": 1,
        "requires": None,
        "description": (
            "Quand quelqu'un t'envoie de la réputation, "
            "un événement spécial peut parfois se déclencher."
        )
    },
    "reconnaissance": {
        "name": "📜 Reconnaissance",
        "cost": 80,
        "category": "🤝 Réputation",
        "tier": 2,
        "requires": "influence",
        "description": (
            "Débloque l'historique détaillé des personnes "
            "qui t'ont donné de la réputation."
        )
    }
}


def talent_is_available(user_id, talent_key):
    talent = TALENTS.get(talent_key)
    if not talent:
        return False
    requirement = talent.get("requires")
    return requirement is None or has_talent(user_id, requirement)


def talent_requirement_text(talent_key):
    talent = TALENTS.get(talent_key)
    requirement = talent.get("requires") if talent else None
    if requirement is None:
        return "🌱 Talent de départ"
    required = TALENTS.get(requirement)
    return f"🔒 Nécessite **{required['name']}**" if required else "🔒 Prérequis manquant"


def has_talent(user_id, talent_key):
    try:
        with db_lock:
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM talents WHERE user_id=? AND talent=?",
                (str(user_id), talent_key)
            )
            result = cursor.fetchone()
            conn.close()
            return result is not None
    except Exception as e:
        print(f"❌ Erreur has_talent : {e}")
        return False


def get_talents(user_id):
    try:
        with db_lock:
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT talent FROM talents WHERE user_id=?",
                (str(user_id),)
            )
            rows = cursor.fetchall()
            conn.close()
            return {row[0] for row in rows}
    except Exception as e:
        print(f"❌ Erreur get_talents : {e}")
        return set()


def buy_talent_for_user(user, talent_key):
    talent = TALENTS[talent_key]

    if has_talent(user["user_id"], talent_key):
        return False, "Tu possèdes déjà ce talent."

    requirement = talent.get("requires")
    if requirement and not has_talent(user["user_id"], requirement):
        required = TALENTS.get(requirement)
        return False, f"🔒 Tu dois d'abord débloquer **{required['name']}** pour accéder à **{talent['name']}**."

    if user["reputation"] < talent["cost"]:
        return False, (
            f"Il te faut **{talent['cost']} ⭐ de réputation**. "
            f"Tu en as **{user['reputation']} ⭐**."
        )

    user["reputation"] -= talent["cost"]

    try:
        with db_lock:
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO talents(user_id, talent, purchased_at)
                VALUES (?, ?, ?)
                """,
                (
                    str(user["user_id"]),
                    talent_key,
                    datetime.now().isoformat()
                )
            )
            conn.commit()
            conn.close()
    except Exception as e:
        user["reputation"] += talent["cost"]
        print(f"❌ Erreur achat talent : {e}")
        return False, "Erreur lors de l'achat du talent."

    save_user(user)
    return True, f"Tu as débloqué **{talent['name']}** !"


# =========================================================
# ECONOMY REWARDS / TALENT EFFECTS
# =========================================================

@auto_interaction_response
async def send_reward_risk_view(
    interaction,
    base_coins,
    base_rep=0,
    title="🎰 Double ou Rien"
):
    class RiskView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=20)
            self.done = False

        @discord.ui.button(
            label="Tenter le double",
            style=discord.ButtonStyle.green,
            emoji="🎰"
        )
        @auto_interaction_response
        async def gamble(self, button_interaction, button):
            if button_interaction.user.id != interaction.user.id:
                await button_interaction.response.send_message(
                    "❌ Cette décision ne t'appartient pas.",
                    ephemeral=True
                )
                return

            if self.done:
                return

            self.done = True

            success = random.random() < 0.5

            for child in self.children:
                child.disabled = True

            if success:
                coins = base_coins * 2
                rep = base_rep * 2

                text = (
                    "🎉 **RÉUSSI !**\n"
                    f"🪙 +{coins} jetons"
                )
                if rep:
                    text += f"\n⭐ +{rep} réputation"
            else:
                coins = 0
                rep = 0
                text = (
                    "💥 **RATÉ !**\n"
                    "Tu perds uniquement le bonus que tu "
                    "avais choisi de risquer."
                )

            user = get_user(interaction.user.id)
            if user:
                user["coins"] += coins
                user["reputation"] += rep
            save_user(user)

            await button_interaction.response.edit_message(
                content=f"{title}\n\n{text}",
                view=self
            )
            self.stop()

        @discord.ui.button(
            label="Garder",
            style=discord.ButtonStyle.secondary,
            emoji="🛡️"
        )
        @auto_interaction_response
        async def keep(self, button_interaction, button):
            if button_interaction.user.id != interaction.user.id:
                await button_interaction.response.send_message(
                    "❌ Cette décision ne t'appartient pas.",
                    ephemeral=True
                )
                return

            if self.done:
                return

            self.done = True

            for child in self.children:
                child.disabled = True

            user = get_user(interaction.user.id)
            if user:
                user["coins"] += base_coins
                user["reputation"] += base_rep
                save_user(user)

            text = f"🛡️ Récompense conservée : **+{base_coins} 🪙**"
            if base_rep:
                text += f"\n⭐ +{base_rep} réputation"

            await button_interaction.response.edit_message(
                content=f"{title}\n\n{text}",
                view=self
            )
            self.stop()

        async def on_timeout(self):
            if self.done:
                return

            self.done = True
            for child in self.children:
                child.disabled = True

            user = get_user(interaction.user.id)
            if user:
                user["coins"] += base_coins
                user["reputation"] += base_rep
                save_user(user)

            try:
                await interaction.edit_original_response(
                    content=(
                        f"{title}\n\n"
                        "⏰ Temps écoulé : récompense conservée.\n"
                        f"🪙 +{base_coins} jetons"
                    ),
                    view=self
                )
            except Exception:
                pass

    view = RiskView()
    await interaction.response.send_message(
        f"{title}\n\n"
        f"Tu peux garder **{base_coins} 🪙**"
        + (f" + **{base_rep} ⭐**" if base_rep else "")
        + "\n\nOu tenter de doubler cette récompense.\n"
        "⚠️ En cas d'échec, seul le bonus risqué est perdu.",
        view=view
    )


@auto_interaction_response
async def maybe_double_or_nothing(
    interaction,
    coins,
    rep=0,
):
    user = get_user(interaction.user.id)

    if not user or not has_talent(user["user_id"], "double_ou_rien"):
        user = get_user(interaction.user.id)
        if user:
            user["coins"] += coins
            user["reputation"] += rep
            save_user(user)

        await interaction.response.send_message(
            f"🎁 Récompense : **+{coins} 🪙**"
            + (f" • ⭐ +{rep}" if rep else "")
        )
        return

    await send_reward_risk_view(
        interaction,
        coins,
        rep,
    )


def chance_bonus():
    return random.choice([
        0, 0, 0, 25, 50, 75, 100, 150
    ])


async def maybe_lucky_wheel(
    interaction,
    user,
    base_coins,
    base_rep=0,
):
    if not has_talent(user["user_id"], "main_chanceuse"):
        return False

    # 1 déclenchement maximum par jour.
    today = datetime.now().date().isoformat()
    last = user.get("last_wheel")

    if last == today:
        return False

    if random.random() > 0.30:
        return False

    user["last_wheel"] = today
    bonus = chance_bonus()

    if bonus <= 0:
        await interaction.followup.send(
            "🍀 **Main Chanceuse !**\n"
            "La roue bonus tombe sur **rien cette fois** 😅"
        )
        save_user(user)
        return True

    user["coins"] += bonus
    save_user(user)

    await interaction.followup.send(
        "🍀 **Main Chanceuse !**\n"
        f"🎡 La roue bonus te donne **+{bonus} 🪙** !"
    )
    return True


# =========================================================
# DUELS
# =========================================================

active_duels = {}


def player_in_duel(user_id):
    user_id = int(user_id)

    for duel_data in active_duels.values():
        if duel_data["challenger"] == user_id:
            return True
        if duel_data["target"] == user_id:
            return True

    return False


def duel_key(challenger_id, target_id):
    return int(challenger_id), int(target_id)


async def finish_duel_cleanup(key):
    active_duels.pop(key, None)


async def start_duel(channel, challenger_id, target_id, key):
    try:
        challenger = bot.get_user(challenger_id) or await bot.fetch_user(challenger_id)
        target = bot.get_user(target_id) or await bot.fetch_user(target_id)

        challenger_data = get_user(challenger_id)
        target_data = get_user(target_id)

        if challenger_data is None or target_data is None:
            await channel.send("❌ Impossible de récupérer les données des joueurs.")
            return

        challenger_hp = 100 + (challenger_data["armor"] - 1) * 10
        target_hp = 100 + (target_data["armor"] - 1) * 10
        turn = random.choice(["challenger", "target"])

        message = await channel.send(
            "⚔️ **DUEL !**\n\n"
            f"👤 **{challenger.display_name}** : ❤️ {challenger_hp} PV\n"
            f"👤 **{target.display_name}** : ❤️ {target_hp} PV\n\n"
            "🔥 **Le combat commence !**"
        )

        await asyncio.sleep(1)
        round_count = 0

        while challenger_hp > 0 and target_hp > 0:
            round_count += 1

            if turn == "challenger":
                damage = random.randint(12, 20) + (challenger_data["sword"] - 1) * 3
                target_hp = max(0, target_hp - damage)
                action_text = (
                    f"🗡️ **{challenger.display_name}** "
                    f"inflige **{damage} dégâts** !"
                )
                turn = "target"
            else:
                damage = random.randint(12, 20) + (target_data["sword"] - 1) * 3
                challenger_hp = max(0, challenger_hp - damage)
                action_text = (
                    f"🗡️ **{target.display_name}** "
                    f"inflige **{damage} dégâts** !"
                )
                turn = "challenger"

            try:
                await message.edit(
                    content=(
                        "⚔️ **DUEL !**\n\n"
                        f"👤 **{challenger.display_name}** : ❤️ {challenger_hp} PV\n"
                        f"👤 **{target.display_name}** : ❤️ {target_hp} PV\n\n"
                        f"**Tour {round_count}**\n{action_text}"
                    )
                )
            except (discord.NotFound, discord.Forbidden):
                break

            await asyncio.sleep(1.5)

        if challenger_hp <= 0 and target_hp <= 0:
            await channel.send(
                "🤝 **Égalité !**\n"
                "Les deux joueurs sont tombés en même temps."
            )
            return

        if challenger_hp > 0:
            winner, loser = challenger, target
            winner_data, loser_data = challenger_data, target_data
        else:
            winner, loser = target, challenger
            winner_data, loser_data = target_data, challenger_data

        winner_data["wins"] += 1
        winner_data["kills"] += 1
        winner_data["elo"] += 25
        winner_data["aura"] += 10
        winner_data["coins"] += 50

        loser_data["losses"] += 1
        loser_data["deaths"] += 1
        loser_data["elo"] = max(0, loser_data["elo"] - 20)
        loser_data["coins"] += 10

        save_user(winner_data)
        save_user(loser_data)
        update_duel_progress(winner.id, True, winner_data["elo"])
        update_duel_progress(loser.id, False, loser_data["elo"])
        wp=get_progress(winner.id)
        progress_quests(winner.id, duel_played=True, duel_won=True, streak=wp["duel_streak"])
        progress_quests(loser.id, duel_played=True, duel_won=False, streak=0)

        revenge_view = DuelRevengeView(winner.id, loser.id)
        await channel.send(
            "🏆 **DUEL TERMINÉ !**\n\n"
            f"🥇 **{winner.display_name}** gagne !\n"
            f"💀 **{loser.display_name}** perd !\n\n"
            "🎁 **Gagnant**\n"
            "✨ +10 Aura\n🏆 +25 ELO\n🪙 +50 jetons\n\n"
            "🎁 **Perdant**\n"
            "📉 -20 ELO\n🪙 +10 jetons",
            view=revenge_view
        )

    except Exception as e:
        print("❌ ERREUR GÉNÉRALE DUEL :", repr(e))
        try:
            await channel.send(
                "❌ Une erreur est survenue pendant le duel.\n"
                "Le duel est annulé."
            )
        except Exception:
            pass
    finally:
        await finish_duel_cleanup(key)


class DuelView(discord.ui.View):
    def __init__(self, challenger_id, target_id, key):
        super().__init__(timeout=DUEL_TIMEOUT)
        self.challenger_id = challenger_id
        self.target_id = target_id
        self.key = key
        self.finished = False
        self.message = None

    async def disable_buttons(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    @discord.ui.button(
        label="Accepter",
        style=discord.ButtonStyle.green,
        emoji="⚔️"
    )
    @auto_interaction_response
    async def accept(self, interaction, button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "❌ Ce duel ne t'est pas destiné.",
                ephemeral=True
            )
            return

        if self.finished:
            await interaction.response.send_message(
                "❌ Ce duel est déjà terminé.",
                ephemeral=True
            )
            return

        self.finished = True
        await self.disable_buttons()

        await interaction.response.edit_message(
            content="⚔️ **Duel accepté !**\n🔥 Le combat commence...",
            view=self
        )

        await start_duel(
            interaction.channel,
            self.challenger_id,
            self.target_id,
            self.key
        )
        self.stop()

    @discord.ui.button(
        label="Refuser",
        style=discord.ButtonStyle.red,
        emoji="❌"
    )
    @auto_interaction_response
    async def refuse(self, interaction, button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "❌ Ce duel ne t'est pas destiné.",
                ephemeral=True
            )
            return

        if self.finished:
            await interaction.response.send_message(
                "❌ Ce duel est déjà terminé.",
                ephemeral=True
            )
            return

        self.finished = True
        await finish_duel_cleanup(self.key)

        await interaction.response.edit_message(
            content="❌ **Duel refusé.**",
            view=None
        )
        self.stop()

    async def on_timeout(self):
        if self.finished:
            return

        self.finished = True
        await finish_duel_cleanup(self.key)

        if self.message:
            try:
                await self.message.edit(
                    content="⏰ **Duel expiré.**",
                    view=None
                )
            except Exception:
                pass



# =========================================================
# /TALENT
# =========================================================

class TalentSelect(discord.ui.Select):
    def __init__(self, user_id):
        self.user_id = user_id
        options = []
        for key, talent in TALENTS.items():
            owned = has_talent(user_id, key)
            available = talent_is_available(user_id, key)
            if owned:
                status, emoji = "✅ Débloqué", "✅"
            elif available:
                status, emoji = f"⭐ {talent['cost']} • Disponible", "🌱" if talent.get("tier", 1) == 1 else "🔓"
            else:
                status, emoji = "🔒 Prérequis", "🔒"
            desc = f"T{talent.get('tier', 1)} • {status} • {talent_requirement_text(key)}"[:100]
            options.append(discord.SelectOption(label=talent["name"][:100], description=desc, value=key, emoji=emoji))
        super().__init__(placeholder="🌳 Choisis un talent...", min_values=1, max_values=1, options=options)

    @auto_interaction_response
    async def callback(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        user = get_user(self.user_id)
        if user is None:
            await interaction.followup.send("❌ Profil introuvable.", ephemeral=True)
            return
        success, message = buy_talent_for_user(user, self.values[0])
        await interaction.followup.send(("✅ " if success else "❌ ") + message, ephemeral=True)
        try:
            fresh = get_user(self.user_id)
            if fresh:
                embed = discord.Embed(
                    title="🌳 ARBRE DE TALENTS — AURA VIBE",
                    description="🟢 Débloqué • 🟡 Achetable • ⚫ Verrouillé\nLes prérequis doivent être débloqués dans l'ordre.",
                    color=discord.Color.purple()
                )
                branches = [
                    ("🍀 BRANCHE CHANCE", ["main_chanceuse", "double_ou_rien", "joker", "destin", "prediction"]),
                    ("🎮 BRANCHE MINI-JEUX", ["defi_parfait", "mode_chaos"]),
                    ("🤝 BRANCHE RÉPUTATION", ["influence", "reconnaissance", "remonter_le_temps"])
                ]
                for title, keys in branches:
                    embed.add_field(
                        name=title,
                        value="🌱 **Départ**\n│\n" + "\n│\n".join(talent_tree_line(self.user_id, k) for k in keys),
                        inline=False
                    )
                owned_count = len(get_talents(self.user_id))
                embed.add_field(
                    name="📊 Progression",
                    value=f"🌳 **{owned_count}/{len(TALENTS)}** talents débloqués\n⭐ REP restante : **{fresh['reputation']}**",
                    inline=False
                )
                await interaction.message.edit(embed=embed, view=TalentView(self.user_id))
        except Exception as e:
            print("⚠️ Actualisation arbre talents impossible :", repr(e))


class TalentView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=180)
        self.add_item(TalentSelect(user_id))


def talent_tree_line(user_id, key):
    data = TALENTS[key]
    owned = has_talent(user_id, key)
    available = talent_is_available(user_id, key)
    if owned:
        icon, state = "🟢", "DÉBLOQUÉ"
    elif available:
        icon, state = "🟡", f"ACHETABLE • ⭐ {data['cost']}"
    else:
        icon, state = "⚫", "VERROUILLÉ"
    req = data.get("requires")
    if req and not owned:
        return f"{icon} **{data['name']}** — {state}\n　└─ 🔒 Requiert : **{TALENTS[req]['name']}**"
    return f"{icon} **{data['name']}** — {state}"



# =========================================================
# DAILY / WEEKLY / MONTHLY
# =========================================================

@auto_interaction_response
async def reward_command(interaction, kind):
    """Daily uniquement : jetons destinées à la boutique des duels, avec streak."""
    if kind != "daily":
        return await interaction.response.send_message("❌ Cette récompense n'est plus disponible.", ephemeral=True)

    user = get_user(interaction.user.id)
    if user is None:
        return await interaction.response.send_message("❌ Profil introuvable.", ephemeral=True)

    now = datetime.now()
    last_raw = user.get("last_daily_claim")
    if last_raw:
        try:
            last_date = datetime.fromisoformat(str(last_raw)).date()
        except (TypeError, ValueError):
            last_date = None
        if last_date == now.date():
            streak = int(user.get("daily_streak", 0) or 0)
            return await interaction.response.send_message(
                f"⏳ Tu as déjà récupéré ton **/daily** aujourd'hui.\n"
                f"🔥 Streak : **{streak} jour{'s' if streak > 1 else ''}**",
                ephemeral=True
            )

    update_claim_streak(user, "daily")
    streak = int(user.get("daily_streak", 1) or 1)
    coins = 100
    user["coins"] = int(user.get("coins", 0) or 0) + coins
    save_user(user)

    await interaction.response.send_message(
        "🎁 **DAILY RÉCUPÉRÉ !**\n\n"
        f"🔥 Streak : **{streak} jour{'s' if streak > 1 else ''}**\n"
        f"🪙 **+{coins} jetons**\n\n"
        "Utilise tes 💠 **Jetons** dans **/shop_duel** pour améliorer ton équipement de duel."
    )

class DuelShopView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=180)
        self.user_id = user_id

    @auto_interaction_response
    async def check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Cette boutique ne t'appartient pas.", ephemeral=True)
            return None
        user = get_user(self.user_id)
        if user is None:
            await interaction.response.send_message("❌ Profil introuvable.", ephemeral=True)
            return None
        return user

    @discord.ui.button(label="Améliorer l'épée", style=discord.ButtonStyle.danger, emoji="⚔️")
    @auto_interaction_response
    async def sword(self, interaction, button):
        user = await self.check(interaction)
        if user is None:
            return
        level = int(user.get("sword", 1))
        cost = 100 * level
        if user["coins"] < cost:
            await interaction.response.send_message(
                f"❌ Il te faut **{cost} 💠**. Tu as **{user['coins']} 💠**.", ephemeral=True
            )
            return
        user["coins"] -= cost
        user["sword"] = level + 1
        save_user(user)
        await interaction.response.edit_message(embed=duel_shop_embed(user), view=self)

    @discord.ui.button(label="Améliorer l'armure", style=discord.ButtonStyle.primary, emoji="🛡️")
    @auto_interaction_response
    async def armor(self, interaction, button):
        user = await self.check(interaction)
        if user is None:
            return
        level = int(user.get("armor", 1))
        cost = 120 * level
        if user["coins"] < cost:
            await interaction.response.send_message(
                f"❌ Il te faut **{cost} 💠**. Tu as **{user['coins']} 💠**.", ephemeral=True
            )
            return
        user["coins"] -= cost
        user["armor"] = level + 1
        save_user(user)
        await interaction.response.edit_message(embed=duel_shop_embed(user), view=self)


def duel_shop_embed(user):
    sword_level = int(user.get("sword", 1))
    armor_level = int(user.get("armor", 1))
    return discord.Embed(
        title="🛒 Boutique des duels",
        description=(
            f"💠 Jetons : **{user.get('coins', 0)}**\n\n"
            f"⚔️ Épée : **niveau {sword_level}** — prochain niveau : **{100 * sword_level} 💠**\n"
            f"🛡️ Armure : **niveau {armor_level}** — prochain niveau : **{120 * armor_level} 💠**\n\n"
            "Les améliorations sont utilisées uniquement dans les duels."
        ),
        color=discord.Color.blurple()
    )

@bot.tree.command(name="daily", description="Récupère ta récompense quotidienne avec une streak.")
async def daily(interaction):
    await reward_command(interaction, "daily")


@bot.tree.command(name="shop_duel", description="Ouvre la boutique dédiée aux duels.")
@auto_interaction_response
async def shop(interaction):
    user = get_user(interaction.user.id)
    if user is None:
        await interaction.response.send_message("❌ Impossible de récupérer ton profil.", ephemeral=True)
        return
    await interaction.response.send_message(embed=duel_shop_embed(user), view=DuelShopView(interaction.user.id), ephemeral=True)


class ShopPurchaseConfirmView(discord.ui.View):
    def __init__(self, guild_id, shop_id, item_id, quantity, user_id, total_price):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.shop_id = shop_id
        self.item_id = item_id
        self.quantity = quantity
        self.user_id = user_id
        self.total_price = total_price

    @auto_interaction_response
    async def check_user(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Cette confirmation ne t'appartient pas.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirmer l'achat", style=discord.ButtonStyle.success, emoji="✅")
    @auto_interaction_response
    async def confirm(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.defer(ephemeral=True)
        with db_lock:
            conn=db_connect()
            row=conn.execute("SELECT id,name,description,price,reward_type,reward_value,enabled,stock,max_per_user,min_level,required_role_id,shop_id FROM custom_shop_items WHERE id=? AND guild_id=?",(self.item_id,str(self.guild_id))).fetchone()
            conn.close()
        if not row or not row[6]:
            return await interaction.edit_original_response(content="❌ Article indisponible.", view=None)
        member_data=get_server_member(self.guild_id,interaction.user.id)
        guild=interaction.guild
        required_role_id=row[10]
        if row[9] and int(member_data["level"]) < int(row[9]):
            return await interaction.edit_original_response(content=f"❌ Niveau minimum : **{row[9]}**.", view=None)
        if required_role_id:
            role=guild.get_role(int(required_role_id))
            if role and role not in interaction.user.roles:
                return await interaction.edit_original_response(content=f"❌ Il faut le rôle **{role.name}** pour acheter cet article.", view=None)
        stock=int(row[7] if row[7] is not None else -1)
        if stock >= 0 and stock < self.quantity:
            return await interaction.edit_original_response(content="❌ Stock insuffisant.", view=None)
        with db_lock:
            conn=db_connect()
            bought=conn.execute("SELECT COALESCE(SUM(quantity),0) FROM shop_purchases WHERE guild_id=? AND shop_id=? AND item_id=? AND user_id=?",(str(self.guild_id),int(row[11] or self.shop_id),self.item_id,str(interaction.user.id))).fetchone()[0]
            conn.close()
        max_per=int(row[8] or 0)
        if max_per and bought + self.quantity > max_per:
            return await interaction.edit_original_response(content=f"❌ Limite personnelle atteinte : **{max_per}** exemplaire(s).", view=None)
        total=int(row[3])*self.quantity
        if member_data["money"] < total:
            return await interaction.edit_original_response(content=f"❌ Solde insuffisant. Prix : **{total} {server_currency(get_guild_config(self.guild_id))}** ; solde : **{member_data['money']} {server_currency(get_guild_config(self.guild_id))}**.", view=None)
        reward_type=str(row[4]).lower()
        reward_value=str(row[5])
        # Vérifie la récompense avant de retirer l'argent.
        if reward_type == "role":
            role=guild.get_role(int(reward_value)) if reward_value.isdigit() else None
            if role is None:
                return await interaction.edit_original_response(content="❌ Le rôle récompense n'existe plus.", view=None)
            if role >= guild.me.top_role:
                return await interaction.edit_original_response(content="❌ AuraBot ne peut pas donner ce rôle : il est au-dessus de son rôle le plus haut.", view=None)
        elif reward_type in {"aura","money","xp"}:
            try: int(float(reward_value))
            except ValueError: return await interaction.edit_original_response(content="❌ Récompense mal configurée.", view=None)
        else:
            return await interaction.edit_original_response(content="❌ Type de récompense inconnu.", view=None)
        member_data["money"] -= total
        save_server_member(member_data)
        if reward_type == "aura":
            user=get_user(interaction.user.id); user["aura"] += int(reward_value)*self.quantity; save_user(user)
            result=f"+{int(reward_value)*self.quantity} Aura"
        elif reward_type == "money":
            member_data["money"] += int(reward_value)*self.quantity; save_server_member(member_data)
            result=f"+{int(reward_value)*self.quantity} {server_currency(get_guild_config(self.guild_id))}"
        elif reward_type == "xp":
            gained=add_server_xp_direct(self.guild_id,interaction.user.id,int(reward_value)*self.quantity)
            result=f"+{int(reward_value)*self.quantity} XP" + (f" • +{gained} niveau(x)" if gained else "")
        else:
            role=guild.get_role(int(reward_value)); await interaction.user.add_roles(role,reason="Achat boutique AuraBot"); result=f"rôle {role.mention} reçu"
        with db_lock:
            conn=db_connect()
            if stock >= 0:
                conn.execute("UPDATE custom_shop_items SET stock=stock-? WHERE id=? AND stock>=?",(self.quantity,self.item_id,self.quantity))
            conn.execute("INSERT INTO shop_purchases(guild_id,shop_id,item_id,user_id,quantity,purchased_at) VALUES(?,?,?,?,?,?)",(str(self.guild_id),int(row[11] or self.shop_id),self.item_id,str(interaction.user.id),self.quantity,datetime.now().isoformat()))
            conn.commit(); conn.close()
        cfg=get_guild_config(self.guild_id)
        await interaction.edit_original_response(content=f"✅ Achat confirmé : **{row[1]}** ×{self.quantity}.\n🎁 Récompense : **{result}**\n💰 Solde restant : **{member_data['money']} {server_currency(cfg)}**", view=None)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary, emoji="✖️")
    @auto_interaction_response
    async def cancel(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.edit_message(content="❌ Achat annulé.", view=None)


def ensure_default_shop(guild_id):
    with db_lock:
        conn=db_connect()
        row=conn.execute("SELECT id FROM custom_shops WHERE guild_id=? ORDER BY id LIMIT 1",(str(guild_id),)).fetchone()
        if row:
            sid=int(row[0])
        else:
            cur=conn.execute("INSERT INTO custom_shops(guild_id,name,description,emoji,style,sort_mode,enabled) VALUES(?,?,?,?,?,?,1)",(str(guild_id),"Boutique","Boutique du serveur","🛒","classic","newest"))
            sid=int(cur.lastrowid)
            conn.commit()
        conn.execute("UPDATE custom_shop_items SET shop_id=? WHERE guild_id=? AND shop_id IS NULL",(sid,str(guild_id)))
        conn.commit(); conn.close()
    return sid


def add_server_xp_direct(guild_id,user_id,amount):
    member=get_server_member(guild_id,user_id); member["xp"] += max(0,int(amount)); gained=0
    cfg=get_guild_config(guild_id); base=max(1,int(cfg.get("level_xp_base",100) or 100))
    while member["xp"] >= base*member["level"]:
        member["xp"] -= base*member["level"]; member["level"] += 1; gained += 1
    save_server_member(member); return gained


class CustomShopView(discord.ui.View):
    def __init__(self,guild_id,shop_id): super().__init__(timeout=180); self.guild_id=guild_id; self.shop_id=shop_id
    @auto_interaction_response
    async def buy(self, interaction, item_id):
        await interaction.response.defer(ephemeral=True)
        with db_lock:
            conn=db_connect(); row=conn.execute("SELECT id,name,description,price,reward_type,reward_value,enabled,stock,max_per_user,min_level,required_role_id,shop_id FROM custom_shop_items WHERE id=? AND guild_id=?",(item_id,str(self.guild_id))).fetchone(); conn.close()
        if not row or not row[6]: return await interaction.edit_original_response(content="❌ Article indisponible.")
        qty=1
        stock=int(row[7] if row[7] is not None else -1)
        if stock == 0: return await interaction.edit_original_response(content="❌ Article en rupture de stock.")
        member=get_server_member(self.guild_id,interaction.user.id); cfg=get_guild_config(self.guild_id)
        total=int(row[3])*qty
        reward_label={"aura":f"{row[5]} Aura","money":f"{row[5]} {server_currency(cfg)}","xp":f"{row[5]} XP","role":"un rôle"}.get(str(row[4]).lower(),"une récompense")
        embed=discord.Embed(title="🛒 Confirmer l'achat",description=f"**{row[1]}**\n{row[2] or 'Sans description'}\n\nPrix : **{total} {server_currency(cfg)}**\nSolde : **{member['money']} {server_currency(cfg)}**\nRécompense : **{reward_label}**",color=discord.Color.blurple())
        await interaction.edit_original_response(embed=embed,view=ShopPurchaseConfirmView(self.guild_id,self.shop_id,item_id,qty,interaction.user.id,total))


@bot.tree.command(name="shop", description="Ouvre la boutique personnalisée du serveur.")
@auto_interaction_response
async def shop(interaction):
    cfg=get_guild_config(interaction.guild.id); shop_id=ensure_default_shop(interaction.guild.id)
    with db_lock:
        conn=db_connect(); shoprow=conn.execute("SELECT id,name,description,emoji,style,banner_url,sort_mode,required_role_id,forbidden_role_id,min_level FROM custom_shops WHERE id=? AND guild_id=? AND enabled=1",(shop_id,str(interaction.guild.id))).fetchone()
        rows=conn.execute("SELECT id,name,description,price,reward_type,reward_value,stock,max_per_user,min_level,required_role_id FROM custom_shop_items WHERE guild_id=? AND shop_id=? AND enabled=1 ORDER BY id DESC",(str(interaction.guild.id),shop_id)).fetchall(); conn.close()
    member=get_server_member(interaction.guild.id,interaction.user.id)
    if shoprow:
        if shoprow[7] and (r:=interaction.guild.get_role(int(shoprow[7]))) and r not in interaction.user.roles: return await interaction.response.send_message(f"❌ Cette boutique nécessite le rôle **{r.name}**.",ephemeral=True)
        if shoprow[8] and (r:=interaction.guild.get_role(int(shoprow[8]))) and r in interaction.user.roles: return await interaction.response.send_message("❌ Tu ne peux pas accéder à cette boutique.",ephemeral=True)
        if shoprow[9] and member["level"] < int(shoprow[9]): return await interaction.response.send_message(f"❌ Niveau minimum : **{shoprow[9]}**.",ephemeral=True)
    title=f"{shoprow[3] if shoprow else '🛒'} {shoprow[1] if shoprow else 'Boutique'}"
    embed=discord.Embed(title=title,description=f"{(shoprow[2] if shoprow else '')}\n\n💰 Solde : **{member['money']} {server_currency(cfg)}**",color=discord.Color.blurple())
    if shoprow and shoprow[5]: embed.set_image(url=shoprow[5])
    if not rows: embed.add_field(name="Aucun article",value="La boutique ne contient encore aucun article.",inline=False)
    view=CustomShopView(interaction.guild.id,shop_id)
    for row in rows[:25]:
        stock_txt="∞" if row[6] is None or int(row[6])<0 else str(row[6])
        req=[]
        if row[8]: req.append(f"niveau {row[8]}")
        if row[9] and interaction.guild.get_role(int(row[9])): req.append(f"rôle {interaction.guild.get_role(int(row[9])).name}")
        value=f"{row[2] or 'Sans description'}\nPrix : **{row[3]} {server_currency(cfg)}**\nStock : **{stock_txt}**"
        if req: value += "\nConditions : " + ", ".join(req)
        embed.add_field(name=row[1],value=value,inline=False)
        button=discord.ui.Button(label=f"Acheter • {row[1][:65]}",style=discord.ButtonStyle.success)
        async def cb(i,item_id=row[0]): await view.buy(i,item_id)
        button.callback=cb; view.add_item(button)
    await interaction.response.send_message(embed=embed,view=view,ephemeral=True)

# =========================================================
# TALENTS ACTIFS
# =========================================================





# =========================================================
# UI AURABOT — EMOJIS PERSONNALISÉS
# =========================================================

AURA_EMOJI_NAMES = {
    "coins": "coins", "aura": "aura", "rep": "rep",
    "xp": "xp", "daily": "daily", "weekly": "weekly", "monthly": "monthly",
    "shop": "shop", "missions": "missions", "games": "games", "talents": "talents",
    "inventory": "inventory", "profile": "profile", "win": "win", "lose": "lose",
}

def aura_emoji(guild, key, fallback):
    if guild is None:
        return fallback
    emoji = discord.utils.get(guild.emojis, name=AURA_EMOJI_NAMES.get(key, key))
    return str(emoji) if emoji else fallback

def economy_bar(current, maximum, length=12):
    if maximum <= 0:
        return "░" * length
    filled = round(max(0, min(1, current / maximum)) * length)
    return "█" * filled + "░" * (length - filled)


# =========================================================
# MINI-JEUX ÉCONOMIQUES
# =========================================================

MINIGAMES = {
    "pileface": {
        "name": "🪙 Pile ou Face",
        "description": "50% de chance de gagner des jetons.",
    },
    "de": {
        "name": "🎲 Dé",
        "description": "Lance un dé et gagne selon ton résultat.",
    },
    "nombre": {
        "name": "🎯 Nombre Mystère",
        "description": "Devine un nombre et tente un gros gain.",
    },
    "risque": {
        "name": "💣 Risque",
        "description": "Choisis un multiplicateur avec un risque réel.",
    }
}



# =========================================================
# /ADMIN_GIVE
# =========================================================


# =========================================================
# /RESET_ECO
# =========================================================


# =========================================================
# /ADMIN_RESET
# =========================================================


# =========================================================
# /ADMIN_AURA
# =========================================================


# =========================================================
# /ADMIN_REP
# =========================================================


# =========================================================
# /ANNONCE
# =========================================================


# =========================================================
# /TUTO & /HELP
# =========================================================

class DuelRevengeView(discord.ui.View):
    def __init__(self, winner_id, loser_id):
        super().__init__(timeout=120)
        self.winner_id=winner_id; self.loser_id=loser_id; self.used=False

    @discord.ui.button(label="Revanche",style=discord.ButtonStyle.danger,emoji="⚔️")
    @auto_interaction_response
    async def revenge(self,interaction,button):
        if interaction.user.id not in (self.winner_id,self.loser_id):
            return await interaction.response.send_message("❌ Seuls les deux joueurs du duel peuvent demander une revanche.",ephemeral=True)
        if self.used: return await interaction.response.send_message("❌ La revanche a déjà été lancée.",ephemeral=True)
        other_id=self.loser_id if interaction.user.id==self.winner_id else self.winner_id
        other=interaction.guild.get_member(other_id)
        if other is None: return await interaction.response.send_message("❌ Joueur introuvable.",ephemeral=True)
        key=duel_key(interaction.user.id,other.id)
        if player_in_duel(interaction.user.id) or player_in_duel(other.id):
            return await interaction.response.send_message("❌ Un des joueurs est déjà dans un duel.",ephemeral=True)
        self.used=True; button.disabled=True
        active_duels[key]={"challenger":interaction.user.id,"target":other.id,"created_at":datetime.now().isoformat(),"status":"pending"}
        view=DuelView(interaction.user.id,other.id,key)
        await interaction.response.send_message(f"⚔️ {other.mention}, **{interaction.user.display_name}** demande une revanche !",view=view)
        view.message=await interaction.original_response()


@bot.tree.command(name="duel", description="Défie un autre joueur.")
@app_commands.describe(membre="Le joueur que tu veux défier")
@auto_interaction_response
async def duel(interaction, membre: discord.Member):
    if membre.bot:
        await interaction.response.send_message(
            "❌ Tu ne peux pas défier un bot.",
            ephemeral=True
        )
        return

    if membre.id == interaction.user.id:
        await interaction.response.send_message(
            "❌ Tu ne peux pas te défier toi-même.",
            ephemeral=True
        )
        return

    if player_in_duel(interaction.user.id):
        await interaction.response.send_message(
            "❌ Tu es déjà dans un duel.",
            ephemeral=True
        )
        return

    if player_in_duel(membre.id):
        await interaction.response.send_message(
            f"❌ {membre.mention} est déjà dans un duel.",
            ephemeral=True
        )
        return

    key = duel_key(interaction.user.id, membre.id)

    view = DuelView(
        interaction.user.id,
        membre.id,
        key
    )

    active_duels[key] = {
        "challenger": interaction.user.id,
        "target": membre.id,
        "created_at": datetime.now().isoformat(),
        "status": "pending"
    }

    try:
        await interaction.response.send_message(
            f"⚔️ **{membre.mention}**, "
            f"**{interaction.user.display_name}** te défie !\n\n"
            "🟢 **Accepter** pour commencer\n"
            "🔴 **Refuser** pour annuler\n\n"
            "⏰ Le défi expire dans **5 minutes**.",
            view=view
        )
        view.message = await interaction.original_response()
    except Exception as e:
        await finish_duel_cleanup(key)
        print("❌ ERREUR CRÉATION DUEL :", repr(e))

# MEMBER BATTLE
# =========================================================

active_member_battles = {}


class MemberBattle:
    def __init__(self, channel_id):
        self.channel_id = channel_id
        self.players = {}
        self.running = False
        self.starting = False
        self.finished = False
        self.message = None
        self.created_at = datetime.now()
        self.lock = asyncio.Lock()
        self.speed_words_used = set()

    def add_player(self, user):
        if self.running or user.id in self.players:
            return False
        if len(self.players) >= MEMBER_BATTLE_MAX_PLAYERS:
            return False

        self.players[user.id] = {
            "user": user,
            "points": 0
        }
        return True

    def remove_player(self, user_id):
        if user_id not in self.players:
            return False

        del self.players[user_id]
        return True


class MemberBattleView(discord.ui.View):
    def __init__(self, battle):
        super().__init__(timeout=None)
        self.battle = battle

    @discord.ui.button(
        label="Participer",
        style=discord.ButtonStyle.green,
        emoji="🎮"
    )
    @auto_interaction_response
    async def join(self, interaction, button):
        if self.battle.running:
            await interaction.response.send_message(
                "❌ La Member Battle a déjà commencé.",
                ephemeral=True
            )
            return

        if interaction.user.bot:
            await interaction.response.send_message(
                "❌ Les bots ne peuvent pas participer.",
                ephemeral=True
            )
            return

        if len(self.battle.players) >= MEMBER_BATTLE_MAX_PLAYERS:
            await interaction.response.send_message(
                f"❌ La partie est complète "
                f"({MEMBER_BATTLE_MAX_PLAYERS} joueurs maximum).",
                ephemeral=True
            )
            return

        added = self.battle.add_player(interaction.user)

        if not added:
            await interaction.response.send_message(
                "❌ Tu participes déjà.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "✅ Tu participes à la Member Battle !",
            ephemeral=True
        )
        await update_memberbattle_message(self.battle)

    @discord.ui.button(
        label="Quitter",
        style=discord.ButtonStyle.red,
        emoji="🚪"
    )
    @auto_interaction_response
    async def leave(self, interaction, button):
        if self.battle.running:
            await interaction.response.send_message(
                "❌ La Member Battle a déjà commencé.",
                ephemeral=True
            )
            return

        removed = self.battle.remove_player(interaction.user.id)

        if not removed:
            await interaction.response.send_message(
                "❌ Tu ne participes pas.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "🚪 Tu as quitté la Member Battle.",
            ephemeral=True
        )
        await update_memberbattle_message(self.battle)

    @discord.ui.button(
        label="Lancer",
        style=discord.ButtonStyle.blurple,
        emoji="🚀"
    )
    @auto_interaction_response
    async def launch(self, interaction, button):
        if not can_memberbattle(interaction.user):
            await interaction.response.send_message(
                "❌ Permission refusée.\n\n"
                "Seuls les administrateurs et animateurs peuvent lancer "
                "une Member Battle.",
                ephemeral=True
            )
            return

        if self.battle.running or self.battle.starting or self.battle.finished:
            await interaction.response.send_message(
                "❌ Cette Member Battle est déjà en cours ou terminée.",
                ephemeral=True
            )
            return

        if len(self.battle.players) < 1:
            await interaction.response.send_message(
                "❌ Il faut au moins un joueur.",
                ephemeral=True
            )
            return

        # Verrouille immédiatement le lancement pour empêcher deux clics
        # simultanés de démarrer deux Member Battles dans le même salon.
        self.battle.starting = True
        self.battle.running = True
        await self.disable_buttons()

        await interaction.response.edit_message(
            content="🚀 **Member Battle lancée !**",
            view=self
        )

        try:
            await run_memberbattle(self.battle)
        except Exception as e:
            print("❌ ERREUR MEMBER BATTLE :", repr(e))
        finally:
            self.battle.running = False
            self.battle.finished = True
            self.battle.starting = False
            active_member_battles.pop(
                self.battle.channel_id,
                None
            )

    async def disable_buttons(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


async def update_memberbattle_message(battle):
    if battle.message is None:
        return

    if not battle.players:
        players_text = "Aucun joueur pour le moment."
    else:
        players_text = "\n".join(
            f"🎮 {info['user'].mention} — **{info['points']} points**"
            for info in battle.players.values()
        )

    embed = discord.Embed(
        title="🏆 Member Battle",
        description=(
            "Participe puis attends le lancement !\n"
            "🎮 **5 mini-jeux aléatoires** • score individuel • classement final\n\n"
            f"👥 **Joueurs : {len(battle.players)}/"
            f"{MEMBER_BATTLE_MAX_PLAYERS}**\n\n"
            f"{players_text}"
        ),
        color=discord.Color.blurple()
    )

    try:
        await battle.message.edit(embed=embed)
    except Exception as e:
        print("❌ Erreur update Member Battle :", repr(e))


async def run_memberbattle(battle):
    games = [
        game_math,
        game_reaction,
        game_guess,
        game_plusmoins,
        game_speed
    ]

    random.shuffle(games)

    # Un seul run peut exister pour une battle.
    async with battle.lock:
        try:
            for index, game in enumerate(games, start=1):
                if battle.finished or not battle.players:
                    break

                channel = bot.get_channel(battle.channel_id)
                if channel is None:
                    raise RuntimeError("Salon Member Battle introuvable")

                await channel.send(
                    f"🎮 **MINI-JEU {index}/5**\n"
                    "Préparez-vous !"
                )

                await game(battle)
                await asyncio.sleep(2)

            if battle.players:
                await finish_memberbattle(battle)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("❌ ERREUR RUN MEMBER BATTLE :", repr(e))
            channel = bot.get_channel(battle.channel_id)
            if channel:
                try:
                    await channel.send(
                        "❌ La Member Battle a été arrêtée à cause d'une erreur. "
                        "Aucune nouvelle partie n'a été lancée automatiquement."
                    )
                except Exception:
                    pass
        finally:
            battle.running = False
            battle.starting = False
            battle.finished = True
            active_member_battles.pop(battle.channel_id, None)


@auto_interaction_response
async def game_math(battle):
    channel = bot.get_channel(battle.channel_id)
    if channel is None:
        return

    a = random.randint(5, 30)
    b = random.randint(5, 30)
    operation = random.choice(["+", "-", "*"])

    if operation == "+":
        answer = a + b
    elif operation == "-":
        answer = a - b
    else:
        answer = a * b

    choices = [answer]

    while len(choices) < 4:
        value = answer + random.randint(-10, 10)
        if value not in choices:
            choices.append(value)

    random.shuffle(choices)

    embed = discord.Embed(
        title="🧠 MINI-JEU : MATHS",
        description=f"Combien font **{a} {operation} {b}** ?",
        color=discord.Color.blue()
    )

    view = discord.ui.View(timeout=15)
    finished = False

    @auto_interaction_response
    async def answer_button(interaction, value):
        nonlocal finished

        if finished:
            await interaction.response.send_message(
                "❌ Trop tard !",
                ephemeral=True
            )
            return

        if interaction.user.id not in battle.players:
            await interaction.response.send_message(
                "❌ Tu ne participes pas.",
                ephemeral=True
            )
            return

        if value == answer:
            finished = True
            battle.players[interaction.user.id]["points"] += 100

            for item in view.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True

            await interaction.response.edit_message(
                content=(
                    f"🏆 {interaction.user.mention} "
                    f"trouve la bonne réponse ! **+100 points**"
                ),
                view=view
            )
        else:
            await interaction.response.send_message(
                "❌ Mauvaise réponse !",
                ephemeral=True
            )

    for value in choices:
        button = discord.ui.Button(
            label=str(value),
            style=discord.ButtonStyle.primary
        )

        async def callback(interaction, value=value):
            await answer_button(interaction, value)

        button.callback = callback
        view.add_item(button)

    await channel.send(embed=embed, view=view)
    await asyncio.sleep(15)


@auto_interaction_response
async def game_reaction(battle):
    channel = bot.get_channel(battle.channel_id)
    if channel is None:
        return

    await channel.send(
        "⚡ **RÉACTION !**\n"
        "Le bouton va apparaître..."
    )

    await asyncio.sleep(random.uniform(2, 5))

    view = discord.ui.View(timeout=10)
    finished = False

    button = discord.ui.Button(
        label="⚡ CLIQUE !",
        style=discord.ButtonStyle.green
    )

    @auto_interaction_response
    async def callback(interaction):
        nonlocal finished

        if finished:
            await interaction.response.send_message(
                "❌ Trop tard !",
                ephemeral=True
            )
            return

        if interaction.user.id not in battle.players:
            await interaction.response.send_message(
                "❌ Tu ne participes pas.",
                ephemeral=True
            )
            return

        finished = True
        battle.players[interaction.user.id]["points"] += 100

        for item in view.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

        await interaction.response.edit_message(
            content=(
                f"🏆 {interaction.user.mention} est le plus rapide ! "
                f"**+100 points**"
            ),
            view=view
        )

    button.callback = callback
    view.add_item(button)

    await channel.send("🚨 **MAINTENANT !**", view=view)
    await asyncio.sleep(10)


async def game_guess(battle):
    channel = bot.get_channel(battle.channel_id)
    if channel is None:
        return

    number = random.randint(1, 100)

    await channel.send(
        "🎯 **DEVINE LE NOMBRE !**\n"
        "Nombre entre **1 et 100**.\n"
        "Écris ton nombre dans le chat !"
    )

    end_time = asyncio.get_running_loop().time() + 30
    used_messages = set()

    def check(message):
        if message.id in used_messages or message.author.bot:
            return False
        if message.channel.id != battle.channel_id or message.author.id not in battle.players:
            return False
        try:
            value = int(message.content.strip())
        except ValueError:
            return False
        return 1 <= value <= 100

    while True:
        remaining = end_time - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            message = await bot.wait_for(
                "message",
                timeout=remaining,
                check=check
            )
            used_messages.add(message.id)

            guess_number = int(message.content.strip())

            if guess_number == number:
                battle.players[message.author.id]["points"] += 150
                await channel.send(
                    f"🏆 {message.author.mention} a trouvé !\n"
                    f"Le nombre était **{number}**.\n"
                    "**+150 points**"
                )
                return

            if guess_number < number:
                await message.reply("⬆️ Plus haut !")
            else:
                await message.reply("⬇️ Plus bas !")

        except asyncio.TimeoutError:
            break

    await channel.send(
        f"⏰ Temps écoulé ! Le nombre était **{number}**."
    )


async def game_plusmoins(battle):
    channel = bot.get_channel(battle.channel_id)
    if channel is None:
        return

    number = random.randint(1, 100)
    await channel.send(
        "🔼🔽 **PLUS HAUT / PLUS BAS**\n"
        "Trouve le nombre entre **1 et 100**.\n"
        "💡 Après chaque réponse valide : le bot te dit immédiatement **PLUS HAUT** ou **PLUS BAS**.\n"
        "🏆 **+200 points** pour le premier qui trouve !"
    )

    end_time = asyncio.get_running_loop().time() + 35
    used_messages = set()

    def check(message):
        if message.id in used_messages:
            return False
        if message.author.bot:
            return False
        if message.channel.id != battle.channel_id:
            return False
        if message.author.id not in battle.players:
            return False
        try:
            value = int(message.content.strip())
        except ValueError:
            return False
        return 1 <= value <= 100

    while True:
        remaining = end_time - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            message = await bot.wait_for(
                "message",
                timeout=remaining,
                check=check
            )
        except asyncio.TimeoutError:
            break

        used_messages.add(message.id)
        guess_number = int(message.content.strip())

        # Le joueur peut continuer à proposer tant qu'il n'a pas trouvé.
        if guess_number == number:
            battle.players[message.author.id]["points"] += 200
            await channel.send(
                f"🏆 {message.author.mention} **a trouvé le nombre !**\n"
                f"Le nombre était **{number}**.\n"
                "+200 points"
            )
            return

        if guess_number < number:
            await message.reply("⬆️ **PLUS HAUT !**", mention_author=False)
        else:
            await message.reply("⬇️ **PLUS BAS !**", mention_author=False)

    await channel.send(
        f"⏰ Temps écoulé ! Le nombre était **{number}**.\n"
        "Aucun point supplémentaire pour cette manche."
    )


async def game_speed(battle):
    channel = bot.get_channel(battle.channel_id)
    if channel is None:
        return

    # Petit délai avant le signal pour éviter qu'un joueur puisse répondre
    # au texte d'annonce avant que le défi commence réellement.
    await channel.send(
        "⚡ **DÉFI MOT RAPIDE**\n"
        "Attends le signal... puis écris le mot affiché le plus vite possible !"
    )
    await asyncio.sleep(random.uniform(1.5, 3.5))

    # Le mot ne peut pas être réutilisé pendant la même Member Battle.
    # On garde un pool assez large pour éviter l'impression que le jeu répète toujours le même mot.
    word_pool = [
        "AURA", "VIBE", "AURABOT", "BATTLE", "VICTOIRE", "GAMING",
        "DRAGON", "ECLAIR", "FANTOME", "ROCKET", "PIXEL", "JOYSTICK",
        "CLAVIER", "SOURIS", "FORTNITE", "ROBLOX", "POKEMON", "BRAWL",
        "ARENE", "COMBAT", "CHAMPION", "RAPIDE", "PUISSANCE", "NINJA",
        "GALAXIE", "TEMPETE", "FLAMME", "TONNERRE", "COSMOS", "LEGEND",
        "TURBO", "MYSTERE", "PORTAIL", "LASER", "CRISTAL", "VOLCAN",
        "OMBRE", "PHOENIX", "TITAN", "EPIQUE", "RARE", "ULTIME"
    ]

    available = [word for word in word_pool if word not in battle.speed_words_used]
    if not available:
        battle.speed_words_used.clear()
        available = word_pool

    word = random.choice(available)
    battle.speed_words_used.add(word)

    await channel.send(
        f"🚨 **MAINTENANT !** Écris : **`{word}`**\n"
        "⏱️ Tu as **12 secondes** !"
    )

    start_time = datetime.now().timestamp()

    def normalize_word(content):
        # Tolère les majuscules/minuscules et les espaces accidentels,
        # sans accepter un autre mot.
        return "".join(str(content).strip().casefold().split())

    expected = normalize_word(word)

    def check(message):
        if message.author.bot:
            return False
        if message.channel.id != battle.channel_id:
            return False
        if message.author.id not in battle.players:
            return False
        if message.created_at.timestamp() < start_time:
            return False
        return normalize_word(message.content) == expected

    try:
        message = await bot.wait_for(
            "message",
            timeout=12,
            check=check
        )
        battle.players[message.author.id]["points"] += 125
        await channel.send(
            f"⚡ {message.author.mention} est le plus rapide ! **+125 points**"
        )
    except asyncio.TimeoutError:
        await channel.send(
            f"⏰ Trop lent ! Le mot était **{word}**."
        )

async def get_memberbattle_wins(guild_id, user_id):
    """Retourne les victoires du membre dans ce serveur et sur tous les serveurs."""
    with db_lock:
        conn = db_connect()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT wins FROM member_battle_wins WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id))
        )
        row = cursor.fetchone()
        server_wins = row[0] if row else 0

        cursor.execute(
            "SELECT COALESCE(SUM(wins), 0) FROM member_battle_wins WHERE user_id=?",
            (str(user_id),)
        )
        global_wins = cursor.fetchone()[0]

        conn.close()

    return server_wins, global_wins


def add_memberbattle_win(guild_id, user_id):
    """Ajoute exactement 1 victoire Member Battle au membre."""
    with db_lock:
        conn = db_connect()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO member_battle_wins(guild_id, user_id, wins)
            VALUES (?, ?, 1)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET wins = wins + 1
            """,
            (str(guild_id), str(user_id))
        )

        conn.commit()
        conn.close()


async def finish_memberbattle(battle):
    channel = bot.get_channel(battle.channel_id)
    if channel is None:
        return

    ranking = sorted(
        battle.players.values(),
        key=lambda x: x["points"],
        reverse=True
    )

    if not ranking:
        return

    guild_id = channel.guild.id
    winner = ranking[0]["user"]

    # Seul le gagnant de la Member Battle reçoit une victoire.
    add_memberbattle_win(guild_id, winner.id)
    server_wins, global_wins = await get_memberbattle_wins(guild_id, winner.id)

    lines = []

    for position, player in enumerate(ranking):
        user = player["user"]
        points = player["points"]

        aura = 10 if position == 0 else 0
        user_data = get_user(user.id)
        if user_data is not None and aura:
            user_data["aura"] += aura
            save_user(user_data)
        reward_text = " — ✨ +10 Aura" if aura else ""
        lines.append(f"**#{position + 1}** {user.mention} — **{points} pts**{reward_text}")

    # Affichage demandé : serveur actuel puis tous les serveurs réunis.
    winner_stats = (
        "\n\n🏆 **VICTOIRES DE MEMBER BATTLE — " + winner.display_name + "**\n"
        f"🏠 Ce serveur : **{server_wins}**\n"
        f"🌍 Tous les serveurs réunis : **{global_wins}**"
    )

    embed = discord.Embed(
        title="🏆 MEMBER BATTLE TERMINÉE !",
        description="\n".join(lines) + winner_stats,
        color=discord.Color.gold()
    )

    await channel.send(embed=embed)


@bot.tree.command(
    name="memberbattle",
    description="Lance une Member Battle."
)
@auto_interaction_response
async def memberbattle(interaction):
    if not can_memberbattle(interaction.user):
        await interaction.response.send_message(
            "❌ Permission refusée.\n\n"
            "Seuls les administrateurs et animateurs peuvent lancer "
            "une Member Battle.",
            ephemeral=True
        )
        return

    channel_id = interaction.channel.id

    if channel_id in active_member_battles:
        await interaction.response.send_message(
            "❌ Une Member Battle est déjà active dans ce salon.",
            ephemeral=True
        )
        return

    battle = MemberBattle(channel_id)
    active_member_battles[channel_id] = battle

    embed = discord.Embed(
        title="🏆 MEMBER BATTLE",
        description=(
            "Une nouvelle Member Battle vient d'être créée !\n\n"
            "🎮 Clique sur **Participer** pour rejoindre.\n"
            "🚪 Clique sur **Quitter** pour partir.\n"
            "🚀 Un administrateur ou animateur peut la lancer.\n\n"
            f"👥 Maximum : **{MEMBER_BATTLE_MAX_PLAYERS} joueurs**"
        ),
        color=discord.Color.blurple()
    )

    view = MemberBattleView(battle)

    try:
        await interaction.response.send_message(
            embed=embed,
            view=view
        )
        battle.message = await interaction.original_response()
    except Exception:
        active_member_battles.pop(channel_id, None)
        raise


# =========================================================


@bot.tree.command(name="ping", description="Vérifie que le bot répond.")
@auto_interaction_response
async def ping(interaction):
    await interaction.response.send_message(f"🏓 Pong ! **{round(bot.latency * 1000)} ms**", ephemeral=True)


@bot.tree.command(name="help", description="Affiche les commandes disponibles d'AuraBot.")
@auto_interaction_response
async def help_command(interaction):
    cfg = get_guild_config(interaction.guild.id)
    embed=discord.Embed(title=cfg.get("bot_server_name") or "🤖 AURABOT — COMMANDES",description=cfg.get("bot_server_description") or "Les commandes disponibles.")
    apply_server_appearance(embed, interaction.guild, cfg)
    embed.add_field(name="⚔️ DUELS",value="`/duel` • `/daily` • `/shop_duel` • `/shop`",inline=False)
    embed.add_field(name="📈 SERVEUR",value="`/niveau` • `/argent` • `/topxp` • `/topargent`",inline=False)
    embed.add_field(name="🏆 MEMBER BATTLE",value="`/memberbattle`",inline=False)
    embed.add_field(name="🎮 JEUX",value="`/loup-garou` • `/bombparty` • `/undercover`",inline=False)
    embed.add_field(name="🎫 COMMUNAUTÉ",value="`/config` • Tickets et confessions configurés via `/config`",inline=False)
    embed.add_field(name="⚙️ ADMINISTRATION",value="`/config` • `/annonce` • `/addrole`",inline=False)
    embed.add_field(name="🛰️ PROPRIÉTAIRE",value="`/serveur`",inline=False)
    await interaction.response.send_message(embed=embed,ephemeral=True)


# ---------- ANIMATIONS SÉPARÉES ----------
@auto_interaction_response
async def launch_animation_game(interaction, game_type, title, minimum):
    if not can_memberbattle(interaction.user):
        return await interaction.response.send_message("❌ Seuls les administrateurs et animateurs peuvent lancer ce jeu.", ephemeral=True)
    if interaction.channel_id in active_animation_lobbies or interaction.channel_id in active_lg_games or interaction.channel_id in active_bomb_games or interaction.channel_id in active_undercover_games:
        return await interaction.response.send_message("❌ Une animation est déjà active dans ce salon.", ephemeral=True)
    cfg = get_guild_config(interaction.guild.id)
    maxp = max(2, min(20, int(cfg.get("animation_max_players", 20))))
    lobby = AnimationLobby(interaction.channel, game_type, minimum, maxp)
    active_animation_lobbies[interaction.channel_id] = lobby
    view = AnimationLobbyView(lobby)
    await interaction.response.send_message(
        embed=discord.Embed(
            title=title,
            description=f"Clique sur **Participer** pour rejoindre.\nMinimum : **{minimum}** joueurs\nMaximum : **{maxp}**\n\nLes gagnants reçoivent de l'Aura.",
            color=discord.Color.blurple()
        ),
        view=view
    )
    lobby.message = await interaction.original_response()


@bot.tree.command(name="loup-garou", description="Lance une partie de Loup-Garou.")
async def loup_garou_command(interaction):
    await launch_animation_game(interaction, "lg", "🐺 LOUP-GAROU", 4)


@bot.tree.command(name="bombparty", description="Lance une partie de BombParty.")
async def bombparty_command(interaction):
    await launch_animation_game(interaction, "bomb", "💣 BOMBPARTY", 2)


@bot.tree.command(name="undercover", description="Lance une partie d'Undercover.")
async def undercover_command(interaction):
    await launch_animation_game(interaction, "undercover", "🕵️ UNDERCOVER", 4)


# ---------- LOUP-GAROU ----------
class LGActionView(discord.ui.View):
    def __init__(self, game, actor_id, action, targets):
        super().__init__(timeout=None)
        self.game=game; self.actor_id=actor_id; self.action=action
        for target_id in targets:
            user=game.players[target_id]
            button=discord.ui.Button(label=user.display_name[:80],style=discord.ButtonStyle.primary,row=len(self.children)//5)
            @auto_interaction_response
            async def callback(interaction, target_id=target_id):
                if interaction.user.id != self.actor_id:
                    return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.",ephemeral=True)
                if target_id not in self.game.alive:
                    return await interaction.response.send_message("❌ Ce joueur est éliminé.",ephemeral=True)
                if self.action=="vote" and target_id==self.actor_id:
                    return await interaction.response.send_message("❌ Tu ne peux pas voter pour toi-même.",ephemeral=True)
                self.game.actions[self.action][self.actor_id]=target_id
                await interaction.response.send_message("✅ Choix enregistré.",ephemeral=True)
                self.stop()
            button.callback=callback; self.add_item(button)


async def run_loup_garou(lobby):
    game=LGGame(lobby); active_lg_games[lobby.channel_id]=game; cfg=get_guild_config(lobby.channel.guild.id)
    ids=list(game.players); random.shuffle(ids); wolves=max(1, round(len(ids) * max(10, min(50, int(cfg.get("lg_wolves_percent",25)))) / 100))
    for uid in ids[:wolves]: game.roles[uid]="Loup-Garou"
    rest=[uid for uid in ids if uid not in game.roles]
    if cfg.get("lg_seer_enabled",1) and rest: game.roles[rest.pop()]="Voyante"
    if cfg.get("lg_doctor_enabled",1) and rest: game.roles[rest.pop()]="Médecin"
    for uid in rest: game.roles[uid]="Villageois"
    for uid,role in game.roles.items():
        try:
            await game.players[uid].send(f"🐺 **Loup-Garou**\n\nTon rôle est : **{role}**.")
        except Exception: pass
    await game.channel.send("🐺 **La nuit tombe...** Les rôles ont été distribués en privé.")
    while game.alive:
        win=game.winner()
        if win: break
        game.actions={"kill":{},"heal":{},"inspect":{},"vote":{}}
        night=int(cfg.get("lg_night_seconds",45)); await game.channel.send(f"🌙 **Nuit — {night}s**\nLes actions secrètes sont envoyées en MP.")
        for uid in list(game.alive):
            role=game.roles.get(uid)
            action = "kill" if role=="Loup-Garou" else ("heal" if role=="Médecin" else ("inspect" if role=="Voyante" else None))
            if action:
                try:
                    targets=[x for x in game.alive if action!="kill" or x!=uid]
                    await game.players[uid].send(view=LGActionView(game,uid,action,targets))
                except Exception: pass
        await asyncio.sleep(night)
        kills=list(game.actions["kill"].values()); target=max(set(kills),key=kills.count) if kills else None
        heals=list(game.actions["heal"].values()); saved=heals[-1] if heals else None
        if target and target!=saved and target in game.alive: game.alive.remove(target); await game.channel.send(f"☀️ Au matin, {game.players[target].mention} a été retrouvé éliminé. Son rôle était **{game.roles[target]}**.")
        else: await game.channel.send("☀️ Au matin, personne n'est mort cette nuit.")
        win=game.winner()
        if win: break
        game.actions["vote"]={}; vote_seconds=int(cfg.get("lg_vote_seconds",45))
        await game.channel.send(f"🗳️ **Vote — {vote_seconds}s**\nChaque joueur vivant reçoit son vote en MP.")
        for uid in list(game.alive):
            try: await game.players[uid].send("🗳️ **Vote pour éliminer un joueur :**", view=LGActionView(game,uid,"vote",list(game.alive)))
            except Exception: pass
        await asyncio.sleep(vote_seconds)
        votes=[x for x in game.actions["vote"].values() if x in game.alive];
        if votes:
            counts={x:votes.count(x) for x in set(votes)}; top=max(counts.values()); winners=[x for x,v in counts.items() if v==top]
            if len(winners)==1:
                out=winners[0]; game.alive.remove(out); await game.channel.send(f"⚖️ {game.players[out].mention} est éliminé par le vote. Son rôle était **{game.roles[out]}**.")
            else: await game.channel.send("⚖️ Égalité : personne n'est éliminé.")
    result=game.winner()
    winners = [uid for uid in game.alive if (game.roles.get(uid)=="Loup-Garou" if result=="wolves" else game.roles.get(uid)!="Loup-Garou")]
    for uid in winners: reward_animation_winner(uid, 10)
    mentions = ", ".join(game.players[uid].mention for uid in winners)
    await game.channel.send(("🏆 **Victoire des Loups-Garous !**" if result=="wolves" else "🏆 **Victoire du Village !**") + f"\n✨ Les gagnants reçoivent **+10 Aura** : {mentions}")
    active_lg_games.pop(lobby.channel_id,None)




# ---------- BOMBPARTY ----------
# ---------- BOMBPARTY ----------
_bomb_word_cache = {}

# Petit filet de sécurité pour les mots très courants si le service dictionnaire
# est momentanément indisponible. La validation en ligne reste la règle principale.
COMMON_FRENCH_WORDS = {
    "chat", "chien", "maison", "voiture", "table", "chaise", "porte", "fenetre",
    "livre", "jeu", "joueur", "serveur", "message", "salon", "monde", "jour",
    "nuit", "matin", "soir", "bonjour", "merci", "salut", "ami", "amie", "amis",
    "fille", "garcon", "enfant", "personne", "homme", "femme", "eau", "feu",
    "terre", "air", "soleil", "lune", "etoile", "temps", "heure", "minute",
    "seconde", "question", "reponse", "mot", "lettre", "phrase", "france",
    "francais", "francaise", "discord", "bot", "gaming", "jouer", "joue", "joues",
    "manger", "boire", "dormir", "courir", "marcher", "parler", "dire", "faire",
    "avoir", "etre", "aller", "venir", "voir", "savoir", "vouloir", "pouvoir",
    "devoir", "prendre", "mettre", "donner", "trouver", "aimer", "adorer", "gagner",
    "perdre", "partie", "groupe", "equipe", "niveau", "score", "vie", "coeur",
    "rouge", "bleu", "vert", "jaune", "noir", "blanc", "grand", "petit", "bon",
    "bonne", "mauvais", "mauvaise", "nouveau", "nouvelle", "vrai", "vraie", "faux",
    "rapide", "lent", "facile", "difficile", "fort", "forte", "beau", "belle",
}


def normalize_bomb_word(value):
    value = str(value or "").strip().lower()
    # Un mot doit être une seule unité alphabétique, avec éventuellement
    # apostrophe/trait d'union. Pas de nombres, emojis ou phrases.
    if not value or len(value) < 2 or len(value) > 40:
        return ""
    if any(ch.isdigit() for ch in value) or any(ch.isspace() for ch in value):
        return ""
    allowed = set("abcdefghijklmnopqrstuvwxyzàâäæçéèêëîïôöœùûüÿ'-")
    if any(ch not in allowed for ch in value):
        return ""
    if value[0] in "'-" or value[-1] in "'-" or "--" in value or "''" in value:
        return ""
    return value


async def is_real_french_word(word):
    """Validation BombParty : dictionnaire principal + Wiktionnaire.

    Le dictionnaire précédent refusait beaucoup de conjugaisons, pluriels et
    mots courants. On consulte maintenant deux sources et on ne transforme
    pas une panne réseau en faux refus.
    """
    word = normalize_bomb_word(word)
    if not word:
        return False

    cache_key = word
    if cache_key in _bomb_word_cache:
        return _bomb_word_cache[cache_key]

    plain = word.replace("'", "").replace("-", "")
    if plain in COMMON_FRENCH_WORDS:
        _bomb_word_cache[cache_key] = True
        return True

    dictionary_found = False
    dictionary_answered = False

    # 1) Dictionary API : rapide pour les mots avec une entrée classique.
    try:
        url = "https://api.dictionaryapi.dev/api/v2/entries/fr/" + word
        timeout = aiohttp.ClientTimeout(total=2.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                dictionary_answered = True
                if response.status == 200:
                    dictionary_found = True
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass

    if dictionary_found:
        _bomb_word_cache[cache_key] = True
        return True

    # 2) Wiktionnaire : beaucoup plus large pour les pluriels, conjugaisons
    # et formes fléchies absentes de Dictionary API.
    try:
        import urllib.parse
        title = urllib.parse.quote(word, safe="")
        url = (
            "https://fr.wiktionary.org/w/api.php?action=query&format=json"
            "&redirects=1&titles=" + title
        )
        timeout = aiohttp.ClientTimeout(total=2.5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": "AuraBot/1.0"}) as response:
                if response.status == 200:
                    data = await response.json(content_type=None)
                    pages = data.get("query", {}).get("pages", {})
                    dictionary_answered = True
                    if any(str(page_id) != "-1" for page_id in pages):
                        _bomb_word_cache[cache_key] = True
                        return True
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
        pass

    # Si les services ont répondu et confirment tous les deux l'absence du
    # mot, on le refuse. Si Internet est indisponible, on accepte le mot
    # syntaxiquement valide pour éviter de bloquer injustement une partie.
    if dictionary_answered:
        _bomb_word_cache[cache_key] = False
        return False

    _bomb_word_cache[cache_key] = True
    return True


async def run_bombparty(lobby):
    cfg = get_guild_config(lobby.channel.guild.id)
    game = {
        "players": list(lobby.players.values()),
        "lives": {u.id: int(cfg.get("bomb_lives", 3)) for u in lobby.players.values()},
        "used": set(),
        "current": 0,
        "syllable": "",
        "waiting_for": None,
        "accepted": False,
        "wrong": False,
        "status_message": None,
    }
    active_bomb_games[lobby.channel_id] = game
    timer = 20

    # Interface volontairement courte : un seul message de jeu est réutilisé.
    try:
        game["status_message"] = await lobby.channel.send(
            f"💣 **BombParty** • {timer}s"
        )

        while True:
            alive = [u for u in game["players"] if game["lives"].get(u.id, 0) > 0]
            if len(alive) <= 1:
                break

            user = alive[game["current"] % len(alive)]
            game["current"] += 1
            custom = [x.strip().lower() for x in str(cfg.get("bomb_syllables") or "").split(",") if x.strip()]
            syllables = custom if len(custom) >= 2 else BOMB_SYLLABLES
            game["syllable"] = random.choice(syllables)
            game["waiting_for"] = user.id
            game["accepted"] = False
            game["wrong"] = False

            status = (
                f"💣 {user.mention} • **{game['syllable'].upper()}** • "
                f"❤️ {game['lives'][user.id]} • ⏱️ {timer}s"
            )
            try:
                await game["status_message"].edit(content=status)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                game["status_message"] = await lobby.channel.send(status)

            try:
                await asyncio.wait_for(_wait_bomb(game, user.id), timeout=timer)
            except asyncio.TimeoutError:
                if not game.get("accepted"):
                    game["lives"][user.id] -= 1
                    remaining = max(0, game["lives"][user.id])
                    status = f"💥 {user.mention} −1 ❤️ • **{remaining} restante(s)**"
                    try:
                        await game["status_message"].edit(content=status)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                    await asyncio.sleep(0.8)
    finally:
        alive = [u for u in game["players"] if game["lives"].get(u.id, 0) > 0]
        if alive:
            winner = alive[0]
            reward_animation_winner(winner.id, 10)
            try:
                await game["status_message"].edit(content=f"🏆 **{winner.display_name}** gagne • ✨ +10 Aura")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await lobby.channel.send(f"🏆 **{winner.mention}** gagne • ✨ +10 Aura")
        else:
            try:
                await game["status_message"].edit(content="🏁 **BombParty terminé**")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        active_bomb_games.pop(lobby.channel_id, None)


async def _wait_bomb(game, uid):
    while not game.get("accepted"):
        await asyncio.sleep(0.1)


# ---------- UNDERCOVER ----------
async def run_undercover(lobby):
    players=list(lobby.players.values()); cfg=get_guild_config(lobby.channel.guild.id); custom_pairs=[]
    for item in str(cfg.get("undercover_pairs") or "").split(","):
        if ":" in item:
            a,b=[x.strip() for x in item.split(":",1)]
            if a and b: custom_pairs.append((a,b))
    pairs=custom_pairs if len(custom_pairs)>=2 else UNDERCOVER_PAIRS; normal,under=random.choice(pairs); random.shuffle(players)
    undercover=players[0]; white=players[1] if cfg.get("undercover_white_enabled",1) and len(players)>=6 else None
    game={"channel":lobby.channel,"players":players,"alive":{u.id for u in players},"roles":{},"words":{},"clues":{},"votes":{},"undercover":undercover.id,"white":white.id if white else None,"phase":"clues"}; active_undercover_games[lobby.channel_id]=game
    for u in players:
        role="Undercover" if u.id==undercover.id else ("Mr White" if white and u.id==white.id else "Civil")
        word="" if role=="Mr White" else (under if role=="Undercover" else normal); game["roles"][u.id]=role; game["words"][u.id]=word
        try: await u.send(f"🕵️ **Undercover**\n\nTon rôle : **{role}**\nTon mot : **{word or 'Aucun mot'}**")
        except Exception: pass
    await lobby.channel.send("🕵️ **Manche 1 — indices !** Les joueurs reçoivent leur bouton en MP.")
    for round_no in range(1,10):
        game["clues"]={}; game["votes"]={}; game["phase"]="clues"; alive=[u for u in players if u.id in game["alive"]]
        for u in alive:
            try: await u.send(f"🕵️ C'est à ton tour de donner un indice dans **#{lobby.channel.name}** avec le bouton **Donner mon indice**. ")
            except Exception: pass
        deadline=datetime.now()+timedelta(seconds=60)
        while datetime.now()<deadline and len(game["clues"])<len(alive): await asyncio.sleep(.5)
        game["phase"]="vote"; await lobby.channel.send("🗳️ **Vote !** Chaque joueur reçoit son vote en privé.")
        for uid in list(game["alive"]):
            try: await game["players"][uid].send("🗳️ **Vote pour éliminer quelqu'un :**",view=UndercoverVoteView(game,uid,list(game["alive"])))
            except Exception: pass
        deadline=datetime.now()+timedelta(seconds=45)
        while datetime.now()<deadline and len(game["votes"])<len(alive): await asyncio.sleep(.5)
        votes=[v for uid,v in game["votes"].items() if uid in game["alive"] and v in game["alive"]]
        if votes:
            counts={x:votes.count(x) for x in set(votes)}; top=max(counts.values()); winners=[x for x,v in counts.items() if v==top]
            if len(winners)==1:
                out=winners[0]; game["alive"].remove(out); await lobby.channel.send(f"⚖️ {lobby.players[out].mention} est éliminé. Rôle : **{game['roles'][out]}**.")
            else: await lobby.channel.send("⚖️ Égalité : personne n'est éliminé.")
        if undercover.id not in game["alive"]:
            winners=[u.id for u in players if game["roles"].get(u.id)=="Civil" and u.id in game["alive"]]
            for uid in winners: reward_animation_winner(uid,10)
            await lobby.channel.send("🏆 **Les Civils gagnent !**\n✨ Les civils survivants reçoivent **+10 Aura**."); break
        if len(game["alive"])<=2:
            reward_animation_winner(undercover.id,10)
            await lobby.channel.send("🏆 **L'Undercover gagne !**\n✨ **+10 Aura**"); break
    active_undercover_games.pop(lobby.channel_id,None)

class UndercoverClueModal(discord.ui.Modal, title="Ton indice"):
    clue=discord.ui.TextInput(label="Indice",placeholder="Donne un indice sans révéler le mot.",max_length=200)
    def __init__(self, game, user_id):
        super().__init__(); self.game=game; self.user_id=user_id
    @auto_interaction_response
    async def on_submit(self, interaction):
        if self.user_id not in self.game["alive"] or self.game["phase"]!="clues":
            return await interaction.response.send_message("❌ Ce n'est plus ton tour.",ephemeral=True)
        if self.user_id in self.game["clues"]:
            return await interaction.response.send_message("❌ Tu as déjà donné ton indice.",ephemeral=True)
        self.game["clues"][self.user_id]=str(self.clue.value).strip()[:200]
        await interaction.response.send_message("✅ Indice envoyé.",ephemeral=True)
        await self.game["channel"].send(f"💬 **Indice de {interaction.user.display_name} :** {self.game['clues'][self.user_id]}")

class UndercoverClueView(discord.ui.View):
    def __init__(self, game, user_id):
        super().__init__(timeout=120); self.game=game; self.user_id=user_id
    @discord.ui.button(label="Donner mon indice",style=discord.ButtonStyle.primary,emoji="💬")
    @auto_interaction_response
    async def clue(self,interaction,button):
        if interaction.user.id!=self.user_id: return await interaction.response.send_message("❌ Ce bouton ne t'appartient pas.",ephemeral=True)
        await interaction.response.send_modal(UndercoverClueModal(self.game,self.user_id))

class UndercoverVoteView(discord.ui.View):
    def __init__(self, game, user_id, targets):
        super().__init__(timeout=60); self.game=game; self.user_id=user_id
        for tid in targets:
            b=discord.ui.Button(label=game["players"][tid].display_name[:80],style=discord.ButtonStyle.secondary)
            @auto_interaction_response
            async def cb(interaction,tid=tid):
                if interaction.user.id!=self.user_id: return await interaction.response.send_message("❌ Ce vote ne t'appartient pas.",ephemeral=True)
                if self.user_id not in self.game["alive"] or tid not in self.game["alive"] or tid==self.user_id or self.game["phase"]!="vote":
                    return await interaction.response.send_message("❌ Vote impossible.",ephemeral=True)
                self.game["votes"][self.user_id]=tid; await interaction.response.send_message("✅ Vote enregistré.",ephemeral=True); self.stop()
            b.callback=cb; self.add_item(b)


# =========================================================
# ERREURS COMMANDES
# =========================================================

@bot.tree.error
async def on_app_command_error(interaction, error):
    """Gestionnaire central des erreurs slash-command, sans double réponse."""
    print("\n❌ ERREUR COMMANDE / INTERACTION :")
    import traceback
    traceback.print_exception(type(error), error, error.__traceback__)

    # Les erreurs de permissions/validation sont plus utiles pour l'utilisateur.
    if isinstance(error, app_commands.MissingPermissions):
        message = "❌ Tu n'as pas les permissions nécessaires pour cette commande."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"⏳ Réessaie dans **{error.retry_after:.1f}s**."
    elif isinstance(error, app_commands.CheckFailure):
        message = "❌ Tu n'es pas autorisé à utiliser cette commande."
    elif isinstance(error, discord.Forbidden):
        message = "❌ AuraBot n'a pas les permissions Discord nécessaires pour cette action."
    else:
        message = "❌ AuraBot a rencontré une erreur pendant l'action. L'erreur complète a été enregistrée dans la console."

    try:
        if interaction.is_expired():
            print("⚠️ Interaction expirée : impossible d'envoyer le message d'erreur.")
            return
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except (discord.NotFound, discord.HTTPException) as send_error:
        print(f"⚠️ Impossible d'envoyer le message d'erreur : {send_error!r}")


# =========================================================
# MODERATION
# =========================================================

def _mod_check(interaction):
    # Les commandes de modération d'AuraBot sont accessibles au propriétaire
    # et aux rôles admin configurés dans ADMIN_ROLES.
    # Le rôle Discord "Modérer les membres" n'est donc plus obligatoire
    # pour l'utilisateur si celui-ci est autorisé par AuraBot.
    return bool(interaction.guild and is_admin(interaction.user))


@bot.tree.command(name="kick", description="Expulse un membre du serveur.")
@app_commands.describe(membre="Membre à expulser", raison="Raison de l'expulsion")
@auto_interaction_response
async def kick(interaction, membre: discord.Member, raison: str = "Aucune raison indiquée"):
    if not _mod_check(interaction):
        return await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
    me = interaction.guild.me
    if membre == interaction.user:
        return await interaction.response.send_message("❌ Tu ne peux pas expulser ce membre.", ephemeral=True)
    if me is None or membre.top_role >= me.top_role:
        return await interaction.response.send_message("❌ Le rôle de ce membre est supérieur ou égal à celui d'AuraBot.", ephemeral=True)
    try:
        await interaction.response.defer(ephemeral=True)
        await membre.kick(reason=f"{raison} — par {interaction.user}")
    except discord.Forbidden:
        return await interaction.followup.send("❌ AuraBot n'a pas les permissions nécessaires pour expulser ce membre.", ephemeral=True)
    except discord.HTTPException as e:
        print(f"❌ Erreur kick Discord: {type(e).__name__}: {e}")
        return await interaction.followup.send(f"❌ Le kick a échoué côté Discord (`{type(e).__name__}`).", ephemeral=True)

    # Le kick est terminé : une éventuelle erreur d'envoi du message ne doit pas
    # être interprétée comme un échec du kick.
    try:
        await interaction.followup.send(f"👢 **{membre}** a été expulsé.\n**Raison :** {raison}", ephemeral=False)
    except (discord.NotFound, discord.HTTPException) as e:
        print(f"⚠️ Kick réussi mais confirmation impossible : {e!r}")


@bot.tree.command(name="ban", description="Bannit un membre du serveur.")
@app_commands.describe(membre="Membre à bannir", raison="Raison du bannissement")
@auto_interaction_response
async def ban(interaction, membre: discord.Member, raison: str = "Aucune raison indiquée"):
    if not _mod_check(interaction):
        return await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
    me = interaction.guild.me
    if membre == interaction.user:
        return await interaction.response.send_message("❌ Tu ne peux pas bannir ce membre.", ephemeral=True)
    if me is None or membre.top_role >= me.top_role:
        return await interaction.response.send_message("❌ Le rôle de ce membre est supérieur ou égal à celui d'AuraBot.", ephemeral=True)
    try:
        await interaction.response.defer(ephemeral=True)
        await membre.ban(reason=f"{raison} — par {interaction.user}")
    except discord.Forbidden:
        return await interaction.followup.send("❌ AuraBot n'a pas les permissions nécessaires pour bannir ce membre.", ephemeral=True)
    except discord.HTTPException as e:
        print(f"❌ Erreur ban Discord: {type(e).__name__}: {e}")
        return await interaction.followup.send(f"❌ Le bannissement a échoué côté Discord (`{type(e).__name__}`).", ephemeral=True)

    # Le bannissement a réussi. On sépare la confirmation de l'action Discord.
    try:
        await interaction.followup.send(f"🔨 **{membre}** a été banni.\n**Raison :** {raison}", ephemeral=False)
    except (discord.NotFound, discord.HTTPException) as e:
        print(f"⚠️ Ban réussi mais confirmation impossible : {e!r}")


@bot.tree.command(name="mute", description="Met un membre en timeout.")
@app_commands.describe(membre="Membre à mute", duree="Durée en minutes", raison="Raison du mute")
@auto_interaction_response
async def mute(interaction, membre: discord.Member, duree: app_commands.Range[int, 1, 40320], raison: str = "Aucune raison indiquée"):
    if not _mod_check(interaction):
        return await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
    me = interaction.guild.me
    if membre == interaction.user:
        return await interaction.response.send_message("❌ Tu ne peux pas mute ce membre.", ephemeral=True)
    if me is None or membre.top_role >= me.top_role:
        return await interaction.response.send_message("❌ Le rôle de ce membre est supérieur ou égal à celui d'AuraBot.", ephemeral=True)
    try:
        await interaction.response.defer(ephemeral=True)
        await membre.timeout(discord.utils.utcnow() + timedelta(minutes=duree), reason=f"{raison} — par {interaction.user}")
    except discord.Forbidden:
        return await interaction.followup.send("❌ AuraBot n'a pas les permissions nécessaires pour mute ce membre.", ephemeral=True)
    except discord.HTTPException as e:
        print(f"❌ Erreur mute Discord: {type(e).__name__}: {e}")
        return await interaction.followup.send(f"❌ Le mute a échoué côté Discord (`{type(e).__name__}`).", ephemeral=True)

    try:
        await interaction.followup.send(f"🔇 **{membre}** a été mute pendant **{duree} min**.\n**Raison :** {raison}", ephemeral=False)
    except (discord.NotFound, discord.HTTPException) as e:
        print(f"⚠️ Mute réussi mais confirmation impossible : {e!r}")


@bot.tree.command(name="warn", description="Avertit un membre.")
@app_commands.describe(membre="Membre à avertir", raison="Raison de l'avertissement")
@auto_interaction_response
async def warn(interaction, membre: discord.Member, raison: str = "Aucune raison indiquée"):
    if not _mod_check(interaction):
        return await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
    await interaction.response.send_message(f"⚠️ **{membre.mention}** a reçu un avertissement.\n**Raison :** {raison}")


# =========================================================
# CONFIGURATION SERVEUR — /CONFIG
# =========================================================

DEFAULT_WELCOME = "👋 Bienvenue {member} sur **{server}** ! Tu es le **{member_count}e membre** 🎉"
DEFAULT_LEAVE = "👋 **{member}** a quitté **{server}**. Nous sommes maintenant **{member_count} membres**."
DEFAULT_TICKET_PANEL_TITLE = "🎫 SUPPORT AURAVIBE"
DEFAULT_TICKET_PANEL_DESCRIPTION = "Besoin d'aide ? Clique sur le bouton ci-dessous pour ouvrir un ticket privé avec le staff."
DEFAULT_TICKET_BUTTON = "Créer un ticket"
DEFAULT_TICKET_OPEN_TITLE = "🎫 Ticket ouvert"
DEFAULT_TICKET_OPEN_DESCRIPTION = "Bienvenue {member} !\n\nExplique ta demande ici. Un membre du staff pourra t'aider.\n\n🔒 Utilise le bouton ci-dessous pour fermer le ticket."
DEFAULT_TICKET_CLOSE_BUTTON = "Fermer le ticket"
spam_tracker = {}


def get_guild_config(guild_id):
    guild_id = str(guild_id)
    with db_lock:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,))
        row = cursor.fetchone()
        columns = [d[0] for d in cursor.description]
        if row is None:
            cursor.execute("INSERT INTO guild_config(guild_id, welcome_message, leave_message, ticket_panel_title, ticket_panel_description, ticket_button_label, ticket_open_title, ticket_open_description, ticket_close_label) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (guild_id, DEFAULT_WELCOME, DEFAULT_LEAVE, DEFAULT_TICKET_PANEL_TITLE, DEFAULT_TICKET_PANEL_DESCRIPTION, DEFAULT_TICKET_BUTTON, DEFAULT_TICKET_OPEN_TITLE, DEFAULT_TICKET_OPEN_DESCRIPTION, DEFAULT_TICKET_CLOSE_BUTTON))
            conn.commit()
            cursor.execute("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,))
            row = cursor.fetchone()
            columns = [d[0] for d in cursor.description]
        conn.close()
    data = dict(zip(columns, row))
    defaults = {
        "welcome_message": DEFAULT_WELCOME,
        "leave_message": DEFAULT_LEAVE,
        "welcome_enabled": 1,
        "leave_enabled": 1,
        "ticket_panel_title": DEFAULT_TICKET_PANEL_TITLE,
        "ticket_panel_description": DEFAULT_TICKET_PANEL_DESCRIPTION,
        "ticket_button_label": DEFAULT_TICKET_BUTTON,
        "ticket_open_title": DEFAULT_TICKET_OPEN_TITLE,
        "ticket_open_description": DEFAULT_TICKET_OPEN_DESCRIPTION,
        "ticket_close_label": DEFAULT_TICKET_CLOSE_BUTTON,
        "ticket_staff_role_ids": data.get("ticket_staff_role_ids") or data.get("ticket_staff_role_id") or "",
        "ticket_panel_channel_id": None,
        "ticket_panel_message_id": None,
        "ticket_categories": "[]",
        "temp_voice_enabled": 0,
        "animations_enabled": 1,
        "animation_max_players": 20,
        "lg_wolves_percent": 25,
        "lg_night_seconds": 45,
        "lg_vote_seconds": 45,
        "lg_doctor_enabled": 1,
        "lg_seer_enabled": 1,
        "bomb_timer": 15,
        "bomb_lives": 3,
        "bomb_syllables": "",
        "suggestion_up_reaction": "👍",
        "suggestion_down_reaction": "👎",
        "counter_correct_reaction": "✅",
        "counter_wrong_reaction": "❌",
        "bomb_correct_reaction": "✅",
        "bomb_wrong_reaction": "❌",
        "undercover_white_enabled": 1,
        "undercover_pairs": "",
        "bot_server_color": "#5865F2",
        "bot_server_footer": "AuraBot",
        "bot_server_bio": 'Un serveur cool 🎮 et le serveur de support : https://discord.gg/6M8s4RvSHp',
        "counter_reward_type": "money",
        "counter_reward_amount": 0,
        "counter_last_user_id": None,
        "counter_wrong_action": "none",
        "counter_wrong_mute_seconds": 60,
        "counter_wrong_fine": 0,
        "level_enabled": 1,
        "level_xp_per_message": 5,
        "level_xp_cooldown": 60,
        "level_money_per_level": 100,
        "level_reward_type": "none",
        "level_reward_amount": 0,
        "level_xp_base": 100,
        "server_currency_name": "Argent",
        "server_currency_emoji": "💵",
        "custom_shop_items": "[]",
        "money_enabled": 1,
        "money_min": 15,
        "money_max": 25,
        "money_cooldown": 30,
        "money_long_message_enabled": 0,
        "money_long_message_length": 250,
        "money_long_message_multiplier": 2.0,
        "confession_panel_message_id": None,
    }
    for k, v in defaults.items():
        if data.get(k) in (None, ""):
            data[k] = v
    return data


def update_guild_config(guild_id, **values):
    if not values:
        return False
    allowed = {
        "welcome_channel_id", "welcome_message", "leave_channel_id", "leave_message",
        "autorole_id", "log_channel_id", "modlog_channel_id", "suggestions_channel_id",
        "confession_channel_id", "confession_report_channel_id", "confession_panel_message_id",
        "ticket_category_id", "ticket_log_channel_id", "ticket_staff_role_id", "ticket_staff_role_ids", "ticket_panel_channel_id", "ticket_panel_message_id", "ticket_categories",
        "temp_voice_enabled", "temp_voice_create_channel_id", "temp_voice_category_id",
        "bot_server_name", "bot_server_avatar_url", "bot_server_description", "bot_server_bio", "bot_server_banner_url", "bot_server_color", "bot_server_footer",
        "anti_links", "anti_spam", "welcome_enabled", "leave_enabled",
        "ticket_panel_title", "ticket_panel_description", "ticket_button_label",
        "ticket_open_title", "ticket_open_description", "ticket_close_label",
        "animations_enabled", "animations_channel_id", "animation_max_players",
        "lg_wolves_percent", "lg_night_seconds", "lg_vote_seconds",
        "lg_doctor_enabled", "lg_seer_enabled", "bomb_timer", "bomb_lives", "bomb_syllables",
        "suggestion_up_reaction", "suggestion_down_reaction", "counter_correct_reaction", "counter_wrong_reaction",
        "bomb_correct_reaction", "bomb_wrong_reaction",
        "undercover_white_enabled", "undercover_pairs",
        "counter_channel_id", "counter_enabled", "counter_count", "counter_last_user_id", "counter_mode",
        "counter_interval", "counter_exact", "counter_reward_type", "counter_reward_amount",
        "counter_wrong_action", "counter_wrong_mute_seconds", "counter_wrong_fine",
        "level_enabled", "level_xp_per_message", "level_xp_cooldown", "level_money_per_level", "level_reward_type", "level_reward_amount",
        "level_xp_base", "server_currency_name", "server_currency_emoji", "custom_shop_items",
        "money_enabled", "money_min", "money_max", "money_cooldown", "money_long_message_enabled",
        "money_long_message_length", "money_long_message_multiplier"
    }
    values = {k: v for k, v in values.items() if k in allowed}
    if not values:
        return False
    get_guild_config(guild_id)
    assignments = ", ".join(f"{key}=?" for key in values)
    params = list(values.values()) + [str(guild_id)]
    with db_lock:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE guild_config SET {assignments} WHERE guild_id=?", params)
        conn.commit()
        success = cursor.rowcount == 1
        conn.close()
        return success


def config_channel(guild, channel_id):
    if not channel_id:
        return "❌ Non configuré"
    channel = guild.get_channel(int(channel_id))
    return channel.mention if channel else "⚠️ Salon supprimé"


def config_role(guild, role_id):
    if not role_id:
        return "❌ Non configuré"
    role = guild.get_role(int(role_id))
    return role.mention if role else "⚠️ Rôle supprimé"


def replace_config_variables(text, member, guild):
    if not text:
        return ""
    return (text
        .replace("{member}", member.mention)
        .replace("{member_name}", member.display_name)
        .replace("{member_id}", str(member.id))
        .replace("{server}", guild.name)
        .replace("{member_count}", str(guild.member_count or len(guild.members)))
        .replace("{channel}", "#salon")
    )


CONFIG_VARIABLES_TEXT = (
    "`{member}` → mention du membre\n"
    "`{member_name}` → nom du membre\n"
    "`{member_id}` → identifiant Discord\n"
    "`{server}` → nom du serveur\n"
    "`{member_count}` → nombre de membres\n"
    "`{channel}` → salon concerné"
)


def get_appearance_color(cfg):
    """Retourne une couleur Discord valide depuis la configuration serveur."""
    raw = str(cfg.get("bot_server_color") or "#5865F2").strip().lstrip("#")
    if len(raw) != 6:
        return discord.Color.blurple()
    try:
        return discord.Color(int(raw, 16))
    except ValueError:
        return discord.Color.blurple()


def apply_server_appearance(embed, guild, cfg=None, include_images=False):
    """Applique l'apparence configurée aux embeds d'AuraBot."""
    cfg = cfg or get_guild_config(guild.id)
    embed.color = get_appearance_color(cfg)

    footer = str(cfg.get("bot_server_footer") or "AuraBot").strip()
    if footer:
        embed.set_footer(text=footer)

    avatar_url = str(cfg.get("bot_server_avatar_url") or "").strip()
    if avatar_url:
        try:
            embed.set_thumbnail(url=avatar_url)
        except Exception:
            pass

    if include_images:
        banner_url = str(cfg.get("bot_server_banner_url") or "").strip()
        if banner_url:
            try:
                embed.set_image(url=banner_url)
            except Exception:
                pass
    return embed


def get_custom_commands(guild_id):
    with db_lock:
        conn = db_connect()
        rows = conn.execute(
            "SELECT name, response, created_at FROM custom_commands WHERE guild_id=? ORDER BY name",
            (str(guild_id),)
        ).fetchall()
        conn.close()
    return [dict(name=row[0], response=row[1], created_at=row[2]) for row in rows]


def get_custom_command(guild_id, name):
    with db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT name, response FROM custom_commands WHERE guild_id=? AND name=?",
            (str(guild_id), name.lower())
        ).fetchone()
        conn.close()
    return dict(name=row[0], response=row[1]) if row else None


def save_custom_command(guild_id, name, response):
    with db_lock:
        conn = db_connect()
        conn.execute(
            "INSERT INTO custom_commands(guild_id,name,response,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(guild_id,name) DO UPDATE SET response=excluded.response",
            (str(guild_id), name.lower(), response, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()


def delete_custom_command(guild_id, name):
    with db_lock:
        conn = db_connect()
        cur = conn.execute(
            "DELETE FROM custom_commands WHERE guild_id=? AND name=?",
            (str(guild_id), name.lower())
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0


def render_custom_command_response(response, message, args_text=""):
    member = message.author
    guild = message.guild
    replacements = {
        "{user}": member.display_name,
        "{mention}": member.mention,
        "{id}": str(member.id),
        "{server}": guild.name,
        "{membercount}": str(guild.member_count),
        "{channel}": message.channel.mention,
        "{args}": args_text.strip(),
    }
    text = response
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text[:2000]


def render_config_embed(guild):
    embed = discord.Embed(
        title="⚙️ Configuration d'AuraBot",
        description=(
            "Choisis un module dans le menu ci-dessous.\n\n"
            "Les réglages sont enregistrés directement sur ce serveur.\n"
            "Aucun module de lancement d'animations ni de canal commandes n'est présent ici."
        ),
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="📋 Modules",
        value=(
            "👋 Arrivées & départs\n"
            "🏷️ Rôles automatiques\n"
            "🛡️ Modération\n"
            "💡 Suggestions\n"
            "🎫 Tickets\n"
            "🕶️ Confessions\n"
            "🔊 Vocaux temporaires\n"
            "🎨 Apparence\n"
            "🔢 Compteur\n"
            "📈 Niveaux & économie\n"
            "🤖 Commandes personnalisées\n"
            "♻️ Réinitialiser"
        ),
        inline=False
    )
    embed.set_footer(text=f"AuraBot • {guild.name}")
    return embed


CONFIG_GUILD = discord.Object(id=1522344351756325088)

# Session /config : le panneau reste utilisable uniquement par son lanceur
# et uniquement dans le salon où /config a été lancé.
active_config_sessions = {}


class ConfigBaseView(discord.ui.View):
    def __init__(self, guild_id, user_id, timeout=600):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.user_id = user_id

    @auto_interaction_response
    async def check_user(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Ce menu ne t'appartient pas.", ephemeral=True
            )
            return False
        session_channel = active_config_sessions.get((self.guild_id, self.user_id))
        if session_channel is not None and interaction.channel_id != session_channel:
            await interaction.response.send_message(
                "❌ Ce panneau /config ne peut être utilisé que dans le salon où il a été lancé.",
                ephemeral=True
            )
            return False
        return True


class ConfigHomeSelect(discord.ui.Select):
    def __init__(self, guild_id, user_id):
        options = [
            discord.SelectOption(label="Accueil", value="home", emoji="🏠", description="Revenir à l’accueil"),
            discord.SelectOption(label="Arrivées & départs", value="welcome", emoji="👋", description="Bienvenue et départ des membres"),
            discord.SelectOption(label="Rôles automatiques", value="autorole", emoji="🏷️", description="Rôle donné aux nouveaux membres"),
            discord.SelectOption(label="Modération", value="moderation", emoji="🛡️", description="Logs, anti-liens et anti-spam"),
            discord.SelectOption(label="Suggestions", value="suggestions", emoji="💡", description="Configurer le salon des suggestions"),
            discord.SelectOption(label="Compteur", value="counter", emoji="🔢", description="Compter les messages et distribuer des récompenses"),
            discord.SelectOption(label="Niveaux & économie", value="levels", emoji="📈", description="XP par message, niveaux, argent et boutique custom"),
            discord.SelectOption(label="Commandes personnalisées", value="custom_commands", emoji="🤖", description="Créer des commandes comme avec DraftBot"),
            discord.SelectOption(label="Tickets", value="tickets", emoji="🎫", description="Configurer le système de tickets"),
            discord.SelectOption(label="Confessions", value="confessions", emoji="🕶️", description="Configurer les confessions anonymes"),
            discord.SelectOption(label="Vocaux temporaires", value="temp_voice", emoji="🔊", description="Configurer les salons vocaux temporaires"),
            discord.SelectOption(label="Apparence", value="appearance", emoji="🎨", description="Personnaliser les embeds et l’identité du bot"),
            discord.SelectOption(label="Réinitialiser", value="reset", emoji="♻️", description="Remettre la configuration serveur à zéro"),
        ]
        super().__init__(
            placeholder="Choisis un module à configurer...",
            min_values=1,
            max_values=1,
            options=options,
            row=0
        )
        self.guild_id = guild_id
        self.user_id = user_id

    @auto_interaction_response
    async def callback(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)

        await interaction.response.defer()

        value = self.values[0]
        guild = interaction.guild
        cfg = get_guild_config(guild.id)

        if value == "home":
            return await interaction.edit_original_response(
                embed=render_config_embed(guild),
                view=ConfigPanelView(self.guild_id, self.user_id)
            )

        if value == "welcome":
            embed = discord.Embed(
                title="👋 Arrivées & départs",
                description=(
                    "Configure les messages envoyés lorsque des membres arrivent ou quittent le serveur.\n\n"
                    f"**Bienvenue :** {'🟢 Activé' if cfg.get('welcome_enabled') else '🔴 Désactivé'}\n"
                    f"**Salon :** {config_channel(guild, cfg.get('welcome_channel_id'))}\n\n"
                    f"**Départ :** {'🟢 Activé' if cfg.get('leave_enabled') else '🔴 Désactivé'}\n"
                    f"**Salon :** {config_channel(guild, cfg.get('leave_channel_id'))}\n\n"
                    f"**Variables disponibles :**\n{CONFIG_VARIABLES_TEXT}"
                ),
                color=discord.Color.blurple()
            )
            return await interaction.edit_original_response(
                embed=embed,
                view=ConfigWelcomeLeaveView(self.guild_id, self.user_id)
            )

        if value == "autorole":
            embed = discord.Embed(
                title="🏷️ Rôles automatiques",
                description=(
                    f"Rôle attribué automatiquement aux nouveaux membres :\n\n"
                    f"**Actuel :** {config_role(guild, cfg.get('autorole_id'))}"
                ),
                color=discord.Color.blurple()
            )
            return await interaction.edit_original_response(
                embed=embed,
                view=ConfigAutoroleView(self.guild_id, self.user_id)
            )

        if value == "moderation":
            embed = discord.Embed(
                title="🔨 Modération",
                description=(
                    f"**Logs générales :** {config_channel(guild, cfg.get('log_channel_id'))}\n"
                    f"**Logs modération :** {config_channel(guild, cfg.get('modlog_channel_id'))}\n\n"
                    f"**Anti-liens :** {'🟢 Activé' if cfg.get('anti_links') else '🔴 Désactivé'}\n"
                    f"**Anti-spam :** {'🟢 Activé' if cfg.get('anti_spam') else '🔴 Désactivé'}"
                ),
                color=discord.Color.blurple()
            )
            return await interaction.edit_original_response(
                embed=embed,
                view=ConfigModerationView(self.guild_id, self.user_id)
            )

        if value == "suggestions":
            embed = discord.Embed(
                title="💡 Suggestions",
                description=f"**Salon des suggestions :** {config_channel(guild, cfg.get('suggestions_channel_id'))}\n\nLes membres pourront envoyer leurs suggestions dans ce salon.",
                color=discord.Color.blurple()
            )
            return await interaction.edit_original_response(
                embed=embed,
                view=ConfigSuggestionView(self.guild_id, self.user_id)
            )

        if value == "counter":
            cfg = get_guild_config(guild.id)
            mode = cfg.get("counter_mode") or "every"
            interval = int(cfg.get("counter_interval") or 1)
            exact = int(cfg.get("counter_exact") or 0)
            amount = int(cfg.get("counter_reward_amount") or 0)
            rtype = cfg.get("counter_reward_type") or "money"
            if rtype == "coins":
                rtype = "money"
            if rtype not in {"money", "aura"}:
                rtype = "money"
            if mode == "every":
                rule = "à chaque message"
            elif mode == "interval":
                rule = f"tous les {interval} messages"
            else:
                rule = f"uniquement au message n°{exact}"
            embed = discord.Embed(
                title="🔢 Compteur",
                description=(
                    f"**Salon :** {config_channel(guild, cfg.get('counter_channel_id'))}\n"
                    f"**État :** {'🟢 Activé' if cfg.get('counter_enabled') else '🔴 Désactivé'}\n"
                    f"**Compteur actuel :** {cfg.get('counter_count', 0)} messages\n"
                    f"**Récompense :** {amount} {(server_currency(cfg) if rtype == 'money' else 'Aura')}\n"
                    f"**Règle :** {rule}\n\n"
                    "Le compteur compte les messages envoyés dans le salon configuré."
                ),
                color=discord.Color.blurple()
            )
            return await interaction.edit_original_response(
                embed=embed,
                view=CounterConfigView(self.guild_id, self.user_id)
            )

        if value == "levels":
            embed = discord.Embed(
                title="📈 Niveaux & économie",
                description=(
                    f"**Système de niveaux :** {'🟢 Activé' if cfg.get('level_enabled',1) else '🔴 Désactivé'}\n"
                    f"**XP par message :** {cfg.get('level_xp_per_message',5)}\n"
                    f"**Cooldown XP :** {cfg.get('level_xp_cooldown',60)}s\n"
                    f"**XP de base :** {cfg.get('level_xp_base',100)} par niveau\n"
                    f"**Récompense de niveau :** {((str(cfg.get('level_reward_amount',0)) + ' ' + str(cfg.get('level_reward_type'))) if str(cfg.get('level_reward_type') or 'none') != 'none' else 'Aucune')}\n"
                    f"**Argent :** {server_currency(cfg)}\n"
                    f"**Gains d'argent par message :** {'🟢 Activé' if cfg.get('money_enabled',1) else '🔴 Désactivé'} — {cfg.get('money_min',15)} à {cfg.get('money_max',25)} / {cfg.get('money_cooldown',30)}s\n\n"
                    "Le niveau et l'argent sont propres à chaque serveur. Le staff peut aussi gérer la boutique custom."
                ), color=discord.Color.blurple()
            )
            return await interaction.edit_original_response(embed=embed, view=LevelsConfigView(self.guild_id, self.user_id))

        if value == "custom_commands":
            commands_list = get_custom_commands(guild.id)
            embed = discord.Embed(
                title="🤖 Commandes personnalisées",
                description=(
                    "Crée tes propres commandes comme sur DraftBot.\n\n"
                    "Les membres les utilisent avec **`!nom`**.\n"
                    "Tu peux utiliser : `{user}`, `{mention}`, `{server}`, `{membercount}`, `{channel}` et `{args}`.\n\n"
                    f"**Commandes créées :** {len(commands_list)}"
                ),
                color=discord.Color.blurple()
            )
            return await interaction.edit_original_response(
                embed=embed,
                view=CustomCommandsConfigView(self.guild_id, self.user_id)
            )

        if value == "tickets":
            embed = discord.Embed(
                title="🎫 Tickets",
                description=(
                    f"**Catégorie :** {config_channel(guild, cfg.get('ticket_category_id'))}\n"
                    f"**Logs :** {config_channel(guild, cfg.get('ticket_log_channel_id'))}\n"
                    f"**Staff :** {config_role(guild, cfg.get('ticket_staff_role_id'))}\n\n"
                    f"**Panel**\n"
                    f"Titre : {cfg.get('ticket_panel_title') or DEFAULT_TICKET_PANEL_TITLE}\n"
                    f"Boutons : {len(get_ticket_reasons(guild))} raison(s) configurée(s) + raison personnalisée\n\n"
                    f"**Ticket ouvert**\n"
                    f"Titre : {cfg.get('ticket_open_title') or DEFAULT_TICKET_OPEN_TITLE}\n"
                    f"Fermeture : {cfg.get('ticket_close_label') or DEFAULT_TICKET_CLOSE_BUTTON}"
                ),
                color=discord.Color.blurple()
            )
            return await interaction.edit_original_response(
                embed=embed,
                view=TicketConfigView(self.guild_id, self.user_id)
            )

        if value == "confessions":
            embed = discord.Embed(
                title="🕶️ Confessions",
                description=(
                    f"**Salon des confessions :** {config_channel(guild, cfg.get('confession_channel_id'))}\n"
                    f"**Salon des reports :** {config_channel(guild, cfg.get('confession_report_channel_id'))}\n\n"
                    "Les reports permettent aux administrateurs de retrouver l'auteur d'une confession."
                ),
                color=discord.Color.blurple()
            )
            return await interaction.edit_original_response(
                embed=embed,
                view=ConfessionConfigView(self.guild_id, self.user_id)
            )

        if value == "temp_voice":
            embed = discord.Embed(
                title="🔊 Salons vocaux temporaires",
                description=(
                    "Les membres rejoignent un salon déclencheur et AuraBot crée automatiquement leur salon.\n\n"
                    "Le créateur reçoit un **panel privé de gestion** et peut gérer uniquement son propre salon : nom, limite, débit, visibilité, verrouillage et autres réglages."
                ),
                color=discord.Color.blurple()
            )
            return await interaction.edit_original_response(embed=embed, view=TempVoiceConfigView(self.guild_id, self.user_id))

        if value == "appearance":
            embed = discord.Embed(
                title="🎨 Apparence d'AuraBot",
                description=(
                    "Personnalisation **propre à ce serveur**.\n\n"
                    "🎨 **Couleur des embeds**\n"
                    "🏷️ **Nom/surnom du bot**\n"
                    "🖼️ **Image et bannière des embeds**\n"
                    "📝 **Footer personnalisé**\n"
                    "👁️ **Aperçu avant utilisation**\n\n"
                    "La bio du bot est fixe et ne peut pas être modifiée ici. Les images peuvent être configurées par lien ou fichier selon les options disponibles."
                ),
                color=get_appearance_color(cfg)
            )
            apply_server_appearance(embed, guild, cfg)
            return await interaction.edit_original_response(
                embed=embed,
                view=BotAppearanceConfigView(self.guild_id, self.user_id)
            )

        if value == "reset":
            embed = discord.Embed(
                title="♻️ Réinitialiser la configuration",
                description=(
                    "Cette action remet uniquement les réglages du serveur à leurs valeurs par défaut.\n\n"
                    "Elle ne supprime ni les profils, ni les duels, ni les Member Battles, ni les giveaways.\n\n"
                    "⚠️ Cette action est définitive."
                ),
                color=discord.Color.red()
            )
            return await interaction.edit_original_response(
                embed=embed,
                view=ConfigResetView(self.guild_id, self.user_id)
            )

        return await interaction.response.send_message("❌ Ce module n'est pas disponible dans AuraBot.", ephemeral=True)


class ConfigPanelView(ConfigBaseView):
    def __init__(self, guild_id, user_id):
        super().__init__(guild_id, user_id)
        self.add_item(ConfigHomeSelect(guild_id, user_id))

    @discord.ui.button(label="Actualiser", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
    @auto_interaction_response
    async def refresh(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.edit_message(
            embed=render_config_embed(interaction.guild),
            view=ConfigPanelView(self.guild_id, self.user_id)
        )

    @discord.ui.button(label="Fermer", style=discord.ButtonStyle.secondary, emoji="✖️", row=1)
    @auto_interaction_response
    async def close(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.edit_message(
            content="⚙️ **Menu de configuration fermé.**",
            embed=None,
            view=None
        )


class ConfigBackView(ConfigBaseView):
    @discord.ui.button(label="Retour aux catégories", style=discord.ButtonStyle.primary, emoji="↩️")
    @auto_interaction_response
    async def back(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.edit_message(
            embed=render_config_embed(interaction.guild),
            view=ConfigPanelView(self.guild_id, self.user_id)
        )


class ConfigInfoEmbed(discord.Embed):
    def __init__(self, title, description):
        super().__init__(title=title, description=description, color=discord.Color.blurple())


class ConfigSingleChannelView(ConfigBackView):
    def __init__(self, guild_id, user_id, field, label):
        super().__init__(guild_id, user_id)
        self.field = field
        self.label = label

    @discord.ui.button(label="Choisir le salon", style=discord.ButtonStyle.primary, emoji="📍", row=0)
    @auto_interaction_response
    async def choose_channel(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.send_message(
            f"📍 Choisis le salon pour **{self.label}** :",
            view=ConfigChannelPicker(self.guild_id, self.user_id, self.field, self.label),
            ephemeral=True
        )


class SuggestionReactionModal(discord.ui.Modal, title="Réactions des suggestions"):
    positive = discord.ui.TextInput(label="Réaction positive", placeholder="👍", required=True, max_length=20)
    negative = discord.ui.TextInput(label="Réaction négative", placeholder="👎", required=True, max_length=20)

    def __init__(self, guild_id, user_id, cfg):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.positive.default_value = str(cfg.get("suggestion_up_reaction") or "👍")
        self.negative.default_value = str(cfg.get("suggestion_down_reaction") or "👎")

    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        pos = self.positive.value.strip() or "👍"
        neg = self.negative.value.strip() or "👎"
        update_guild_config(self.guild_id, suggestion_up_reaction=pos, suggestion_down_reaction=neg)
        await interaction.response.send_message(f"✅ Réactions des suggestions : {pos} / {neg}", ephemeral=True)


class ConfigSuggestionView(ConfigBackView):
    @discord.ui.button(label="Choisir le salon", style=discord.ButtonStyle.primary, emoji="📍", row=0)
    @auto_interaction_response
    async def choose_channel(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.send_message(
            "📍 Choisis le salon où les suggestions seront publiées :",
            view=ConfigChannelPicker(self.guild_id, self.user_id, "suggestions_channel_id", "Suggestions"),
            ephemeral=True
        )

    @discord.ui.button(label="Réactions", style=discord.ButtonStyle.success, emoji="👍", row=0)
    @auto_interaction_response
    async def reactions(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.send_modal(SuggestionReactionModal(self.guild_id, self.user_id, get_guild_config(self.guild_id)))


class ReactionConfigModal(discord.ui.Modal, title="Réactions suggestions / compteur"):
    suggestion_up = discord.ui.TextInput(label="Suggestion positive", placeholder="👍", required=True, max_length=20)
    suggestion_down = discord.ui.TextInput(label="Suggestion négative", placeholder="👎", required=True, max_length=20)
    counter_correct = discord.ui.TextInput(label="Compteur correct", placeholder="✅", required=True, max_length=20)
    counter_wrong = discord.ui.TextInput(label="Compteur incorrect", placeholder="❌", required=True, max_length=20)

    def __init__(self, guild_id, user_id, cfg):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.suggestion_up.default_value = str(cfg.get("suggestion_up_reaction") or "👍")
        self.suggestion_down.default_value = str(cfg.get("suggestion_down_reaction") or "👎")
        self.counter_correct.default_value = str(cfg.get("counter_correct_reaction") or "✅")
        self.counter_wrong.default_value = str(cfg.get("counter_wrong_reaction") or "❌")

    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        update_guild_config(
            self.guild_id,
            suggestion_up_reaction=self.suggestion_up.value.strip() or "👍",
            suggestion_down_reaction=self.suggestion_down.value.strip() or "👎",
            counter_correct_reaction=self.counter_correct.value.strip() or "✅",
            counter_wrong_reaction=self.counter_wrong.value.strip() or "❌",
        )
        await interaction.response.send_message("✅ Réactions des suggestions et du compteur enregistrées.", ephemeral=True)


class BombReactionConfigModal(discord.ui.Modal, title="Réactions BombParty"):
    correct = discord.ui.TextInput(label="Réponse correcte", placeholder="✅", required=True, max_length=20)
    wrong = discord.ui.TextInput(label="Réponse incorrecte", placeholder="❌", required=True, max_length=20)

    def __init__(self, guild_id, user_id, cfg):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.correct.default_value = str(cfg.get("bomb_correct_reaction") or "✅")
        self.wrong.default_value = str(cfg.get("bomb_wrong_reaction") or "❌")

    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        update_guild_config(
            self.guild_id,
            bomb_correct_reaction=self.correct.value.strip() or "✅",
            bomb_wrong_reaction=self.wrong.value.strip() or "❌",
        )
        await interaction.response.send_message("✅ Réactions BombParty enregistrées.", ephemeral=True)




class LevelsConfigModal(discord.ui.Modal, title="Paramètres XP"):
    xp = discord.ui.TextInput(label="XP par message", placeholder="Ex : 5", required=True, max_length=6)
    cooldown = discord.ui.TextInput(label="Cooldown XP (secondes)", placeholder="Ex : 60", required=True, max_length=6)
    base = discord.ui.TextInput(label="XP nécessaire pour le niveau 1", placeholder="Ex : 100", required=True, max_length=8)

    def __init__(self, guild_id, user_id):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        cfg = get_guild_config(guild_id)
        self.xp.default_value = str(cfg.get("level_xp_per_message", 5))
        self.cooldown.default_value = str(cfg.get("level_xp_cooldown", 60))
        self.base.default_value = str(cfg.get("level_xp_base", 100))

    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        try:
            xp = max(0, int(self.xp.value))
            cooldown = max(0, int(self.cooldown.value))
            base = max(1, int(self.base.value))
        except ValueError:
            return await interaction.response.send_message("❌ Les valeurs doivent être numériques.", ephemeral=True)
        update_guild_config(self.guild_id, level_xp_per_message=xp, level_xp_cooldown=cooldown, level_xp_base=base)
        await interaction.response.send_message("✅ Paramètres XP enregistrés.", ephemeral=True)

class LevelRewardConfigModal(discord.ui.Modal, title="Récompense de montée de niveau"):
    type_reward = discord.ui.TextInput(label="Type", placeholder="none | argent | aura | jetons", required=True, max_length=10)
    amount = discord.ui.TextInput(label="Montant", placeholder="Ex : 100", required=True, max_length=10)
    def __init__(self, guild_id, user_id):
        super().__init__(); self.guild_id=guild_id; self.user_id=user_id
    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        rtype=self.type_reward.value.strip().lower()
        aliases={"aucune":"none","aucun":"none","off":"none","desactive":"none","désactivé":"none","money":"argent","coins":"jetons","coin":"jetons"}
        rtype=aliases.get(rtype,rtype)
        if rtype not in {"none","argent","aura","jetons"}:
            return await interaction.response.send_message("❌ Type invalide. Utilise `none`, `argent`, `aura` ou `jetons`.", ephemeral=True)
        try: amount=max(0,int(self.amount.value))
        except ValueError: return await interaction.response.send_message("❌ Le montant doit être un nombre.", ephemeral=True)
        if rtype == "none": amount=0
        update_guild_config(self.guild_id, level_reward_type=rtype, level_reward_amount=amount)
        label={"none":"Aucune","argent":"Argent","aura":"Aura","jetons":"Jetons"}[rtype]
        await interaction.response.send_message(f"✅ Récompense de niveau : **{amount} {label}**." if rtype!="none" else "✅ Récompense de niveau désactivée.", ephemeral=True)


class ShopItemModal(discord.ui.Modal, title="Ajouter un article à la boutique"):
    nom=discord.ui.TextInput(label="Nom", max_length=50, required=True)
    description=discord.ui.TextInput(label="Description", max_length=150, required=False)
    prix=discord.ui.TextInput(label="Prix en argent", placeholder="Ex : 500", max_length=10, required=True)
    recompense=discord.ui.TextInput(label="Récompense", placeholder="aura 100 | money 250 | xp 500 | role ID", max_length=80, required=True)
    options=discord.ui.TextInput(label="Options", placeholder="stock=10;max=1;level=5;role=ID", max_length=150, required=False)
    def __init__(self,guild_id,user_id): super().__init__(); self.guild_id=guild_id; self.user_id=user_id
    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        try: price=max(0,int(self.prix.value))
        except ValueError: return await interaction.response.send_message("❌ Le prix doit être un nombre.", ephemeral=True)
        raw=self.recompense.value.strip().split(maxsplit=1)
        if len(raw)!=2 or raw[0].lower() not in {"aura","money","xp","role"}: return await interaction.response.send_message("❌ Récompense : `aura 100`, `money 250`, `xp 500` ou `role ID`.",ephemeral=True)
        try: int(raw[1])
        except ValueError: return await interaction.response.send_message("❌ La valeur de la récompense est invalide.",ephemeral=True)
        opts={"stock":-1,"max":0,"level":0,"role":""}
        for part in self.options.value.split(";"):
            if "=" in part:
                k,v=part.split("=",1); k=k.strip().lower(); v=v.strip()
                if k in opts:
                    if k=="role": opts[k]=v
                    else:
                        try: opts[k]=max(-1,int(v)) if k=="stock" else max(0,int(v))
                        except ValueError: return await interaction.response.send_message(f"❌ Option `{k}` invalide.",ephemeral=True)
        if raw[0].lower()=="role":
            role=interaction.guild.get_role(int(raw[1]));
            if role is None: return await interaction.response.send_message("❌ ID de rôle introuvable.",ephemeral=True)
        if opts["role"] and interaction.guild.get_role(int(opts["role"])) is None: return await interaction.response.send_message("❌ Le rôle requis n'existe pas.",ephemeral=True)
        shop_id=ensure_default_shop(self.guild_id)
        with db_lock:
            conn=db_connect(); conn.execute("INSERT INTO custom_shop_items(guild_id,name,description,price,reward_type,reward_value,enabled,shop_id,stock,max_per_user,min_level,required_role_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(str(self.guild_id),self.nom.value.strip(),self.description.value.strip(),price,raw[0].lower(),raw[1].strip(),1,shop_id,opts["stock"],opts["max"],opts["level"],opts["role"] or None)); conn.commit(); conn.close()
        await interaction.response.send_message("✅ Article ajouté : il possède maintenant une vraie récompense achetable dans `/shop`.",ephemeral=True)

class MoneyConfigModal(discord.ui.Modal, title="Paramètres argent"):
    currency = discord.ui.TextInput(label="Nom de la monnaie", placeholder="Ex : Argent", required=True, max_length=20)
    enabled = discord.ui.TextInput(label="Gains par message", placeholder="oui ou non", required=True, max_length=5)
    minimum = discord.ui.TextInput(label="Minimum par gain", placeholder="15", required=True, max_length=8)
    maximum = discord.ui.TextInput(label="Maximum par gain", placeholder="25", required=True, max_length=8)
    cooldown = discord.ui.TextInput(label="Cooldown en secondes", placeholder="30", required=True, max_length=8)

    def __init__(self, guild_id, user_id):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        cfg = get_guild_config(guild_id)
        self.currency.default_value = str(cfg.get("server_currency_name") or "Argent")
        self.enabled.default_value = "oui" if cfg.get("money_enabled", 1) else "non"
        self.minimum.default_value = str(cfg.get("money_min", 15))
        self.maximum.default_value = str(cfg.get("money_max", 25))
        self.cooldown.default_value = str(cfg.get("money_cooldown", 30))

    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        try:
            low = max(0, int(self.minimum.value))
            high = max(low, int(self.maximum.value))
            cd = max(0, int(self.cooldown.value))
        except ValueError:
            return await interaction.response.send_message("❌ Les valeurs doivent être numériques.", ephemeral=True)
        yes = {"oui", "o", "yes", "y", "1", "on"}
        no = {"non", "n", "no", "0", "off"}
        enabled_value = self.enabled.value.strip().lower()
        if enabled_value not in yes | no:
            return await interaction.response.send_message("❌ Utilise oui ou non pour les gains par message.", ephemeral=True)
        enabled = enabled_value in yes
        name = self.currency.value.strip() or "Argent"
        update_guild_config(self.guild_id, server_currency_name=name, money_enabled=int(enabled), money_min=low, money_max=high, money_cooldown=cd)
        await interaction.response.send_message(f"✅ Paramètres argent enregistrés : **{name}**, gains {'🟢 activés' if enabled else '🔴 désactivés'}, **{low}–{high}** toutes les **{cd}s**.", ephemeral=True)

class LevelsConfigView(ConfigBackView):
    @discord.ui.button(label="Paramètres argent", style=discord.ButtonStyle.success, emoji="💵", row=0)
    @auto_interaction_response
    async def money(self, interaction, button):
        if await self.check_user(interaction):
            await interaction.response.send_modal(MoneyConfigModal(self.guild_id,self.user_id))

    @discord.ui.button(label="Paramètres XP", style=discord.ButtonStyle.primary, emoji="📈", row=0)
    @auto_interaction_response
    async def settings(self, interaction, button):
        if await self.check_user(interaction): await interaction.response.send_modal(LevelsConfigModal(self.guild_id,self.user_id))
    @discord.ui.button(label="Récompense niveau", style=discord.ButtonStyle.success, emoji="🎁", row=1)
    @auto_interaction_response
    async def level_reward(self, interaction, button):
        if not await self.check_user(interaction): return
        cfg=get_guild_config(self.guild_id)
        modal=LevelRewardConfigModal(self.guild_id,self.user_id)
        modal.type_reward.default_value=str(cfg.get("level_reward_type") or "none")
        modal.amount.default_value=str(cfg.get("level_reward_amount",0) or 0)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Activer / désactiver", style=discord.ButtonStyle.secondary, emoji="🔘", row=0)
    @auto_interaction_response
    async def toggle(self, interaction, button):
        if not await self.check_user(interaction): return
        cfg=get_guild_config(self.guild_id); new=0 if cfg.get("level_enabled",1) else 1; update_guild_config(self.guild_id,level_enabled=new)
        await interaction.response.send_message(f"📈 Niveaux : {'🟢 activés' if new else '🔴 désactivés'}",ephemeral=True)
    @discord.ui.button(label="Configurer la boutique", style=discord.ButtonStyle.success, emoji="🛒", row=1)
    @auto_interaction_response
    async def add_item(self, interaction, button):
        if await self.check_user(interaction): await interaction.response.send_modal(ShopItemModal(self.guild_id,self.user_id))

class CounterRewardModal(discord.ui.Modal, title="Récompenses du compteur"):
    mode = discord.ui.TextInput(
        label="Mode",
        placeholder="every | interval | exact",
        required=True,
        max_length=10
    )
    valeur = discord.ui.TextInput(
        label="Valeur",
        placeholder="Ex : 1 (chaque message), 10 (tous les 10), 100 (au message 100)",
        required=True,
        max_length=10
    )
    recompense = discord.ui.TextInput(
        label="Récompense",
        placeholder="Ex : 10 argent, 5 aura",
        required=True,
        max_length=40
    )

    def __init__(self, guild_id, user_id):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id

    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)

        mode = self.mode.value.strip().lower()
        aliases = {
            "chaque": "every", "message": "every", "every": "every",
            "tous": "interval", "toutes": "interval", "interval": "interval",
            "precis": "exact", "exact": "exact", "nombre": "exact"
        }
        mode = aliases.get(mode, mode)

        if mode not in {"every", "interval", "exact"}:
            return await interaction.response.send_message(
                "❌ Mode invalide. Utilise `every`, `interval` ou `exact`.", ephemeral=True
            )

        try:
            valeur = int(self.valeur.value.strip())
            if valeur < 1:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌ La valeur doit être un nombre supérieur à 0.", ephemeral=True)

        m = re.fullmatch(r"\s*(\d+)\s*(argent|money|aura|coins?)\s*", self.recompense.value.strip().lower())
        if not m:
            return await interaction.response.send_message(
                "❌ Récompense invalide. Exemples : `100 argent`, `5 aura`.", ephemeral=True
            )

        amount = int(m.group(1))
        raw_type = m.group(2)
        reward_type = "aura" if raw_type == "aura" else "money"
        if amount < 1:
            return await interaction.response.send_message("❌ La récompense doit être supérieure à 0.", ephemeral=True)

        update_guild_config(
            self.guild_id,
            counter_enabled=1,
            counter_mode=mode,
            counter_interval=valeur if mode == "interval" else 1,
            counter_exact=valeur if mode == "exact" else 0,
            counter_reward_type=reward_type,
            counter_reward_amount=amount
        )
        cfg = get_guild_config(self.guild_id)
        await interaction.response.send_message(
            f"✅ Récompense du compteur configurée : **{amount} {reward_type}** — "
            f"{'à chaque message' if mode == 'every' else f'tous les {valeur} messages' if mode == 'interval' else f'au message n°{valeur}'}."
            f"\n🔢 Compteur actuel : **{cfg.get('counter_count', 0)}**",
            ephemeral=True
        )


class CounterPunishmentModal(discord.ui.Modal, title="Sanction du mauvais comptage"):
    action=discord.ui.TextInput(label="Action", placeholder="none | mute | fine | both", required=True, max_length=10)
    mute=discord.ui.TextInput(label="Durée mute en secondes", placeholder="Ex : 60", required=True, max_length=8)
    fine=discord.ui.TextInput(label="Amende en argent", placeholder="Ex : 100", required=True, max_length=8)
    def __init__(self,guild_id,user_id): super().__init__(); self.guild_id=guild_id; self.user_id=user_id
    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        action=self.action.value.strip().lower()
        if action not in {"none","mute","fine","both"}: return await interaction.response.send_message("❌ Action invalide.",ephemeral=True)
        try: mute=max(1,int(self.mute.value)); fine=max(0,int(self.fine.value))
        except ValueError: return await interaction.response.send_message("❌ Les valeurs doivent être numériques.",ephemeral=True)
        update_guild_config(self.guild_id,counter_wrong_action=action,counter_wrong_mute_seconds=mute,counter_wrong_fine=fine)
        await interaction.response.send_message("✅ Sanction du mauvais comptage enregistrée.",ephemeral=True)

class CounterConfigView(ConfigBackView):
    @discord.ui.button(label="Choisir le salon", style=discord.ButtonStyle.primary, emoji="📍", row=0)
    @auto_interaction_response
    async def choose_channel(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.send_message(
            "📍 Choisis le salon où le compteur doit compter les messages :",
            view=ConfigChannelPicker(self.guild_id, self.user_id, "counter_channel_id", "Compteur"),
            ephemeral=True
        )

    @discord.ui.button(label="Configurer récompense", style=discord.ButtonStyle.success, emoji="🎁", row=0)
    @auto_interaction_response
    async def reward(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.send_modal(CounterRewardModal(self.guild_id, self.user_id))

    @discord.ui.button(label="Réinitialiser compteur", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
    @auto_interaction_response
    async def reset_count(self, interaction, button):
        if not await self.check_user(interaction):
            return
        update_guild_config(self.guild_id, counter_count=0)
        await interaction.response.send_message("✅ Compteur remis à **0**.", ephemeral=True)

    @discord.ui.button(label="Sanction mauvais comptage", style=discord.ButtonStyle.danger, emoji="⚠️", row=1)
    @auto_interaction_response
    async def punishment(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.send_modal(CounterPunishmentModal(self.guild_id, self.user_id))

    @discord.ui.button(label="Réactions", style=discord.ButtonStyle.success, emoji="👍", row=2)
    @auto_interaction_response
    async def reactions(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.send_modal(ReactionConfigModal(self.guild_id, self.user_id, get_guild_config(self.guild_id)))

    @discord.ui.button(label="Activer / désactiver", style=discord.ButtonStyle.secondary, emoji="🔘", row=1)
    @auto_interaction_response
    async def toggle(self, interaction, button):
        if not await self.check_user(interaction):
            return
        cfg = get_guild_config(self.guild_id)
        new = 0 if cfg.get("counter_enabled") else 1
        update_guild_config(self.guild_id, counter_enabled=new)
        await interaction.response.send_message(
            f"🔢 Compteur : {'🟢 activé' if new else '🔴 désactivé'}", ephemeral=True
        )

class ConfigWelcomeLeaveView(ConfigBackView):
    @discord.ui.button(label="Bienvenue", style=discord.ButtonStyle.primary, emoji="👋", row=0)
    @auto_interaction_response
    async def welcome(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.send_message(
            "📍 Choisis le salon de bienvenue :",
            view=ConfigChannelPicker(self.guild_id, self.user_id, "welcome_channel_id", "Bienvenue"),
            ephemeral=True
        )

    @discord.ui.button(label="Message bienvenue", style=discord.ButtonStyle.success, emoji="✏️", row=0)
    @auto_interaction_response
    async def welcome_message(self, interaction, button):
        if not await self.check_user(interaction):
            return
        cfg = get_guild_config(self.guild_id)
        await interaction.response.send_modal(ConfigMessageModal(
            self.guild_id, self.user_id, "welcome_message", "Message de bienvenue",
            cfg.get("welcome_message") or DEFAULT_WELCOME
        ))

    @discord.ui.button(label="Départ", style=discord.ButtonStyle.primary, emoji="🚪", row=1)
    @auto_interaction_response
    async def leave(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.send_message(
            "📍 Choisis le salon de départ :",
            view=ConfigChannelPicker(self.guild_id, self.user_id, "leave_channel_id", "Départ"),
            ephemeral=True
        )

    @discord.ui.button(label="Message départ", style=discord.ButtonStyle.success, emoji="✏️", row=1)
    @auto_interaction_response
    async def leave_message(self, interaction, button):
        if not await self.check_user(interaction):
            return
        cfg = get_guild_config(self.guild_id)
        await interaction.response.send_modal(ConfigMessageModal(
            self.guild_id, self.user_id, "leave_message", "Message de départ",
            cfg.get("leave_message") or DEFAULT_LEAVE
        ))

    @discord.ui.button(label="Bienvenue ON/OFF", style=discord.ButtonStyle.secondary, emoji="🔘", row=2)
    @auto_interaction_response
    async def toggle_welcome(self, interaction, button):
        if not await self.check_user(interaction):
            return
        cfg = get_guild_config(self.guild_id)
        new = 0 if cfg.get("welcome_enabled") else 1
        update_guild_config(self.guild_id, welcome_enabled=new)
        await interaction.response.send_message(
            f"👋 Bienvenue : {'🟢 activé' if new else '🔴 désactivé'}",
            ephemeral=True
        )

    @discord.ui.button(label="Départ ON/OFF", style=discord.ButtonStyle.secondary, emoji="🚪", row=2)
    @auto_interaction_response
    async def toggle_leave(self, interaction, button):
        if not await self.check_user(interaction):
            return
        cfg = get_guild_config(self.guild_id)
        new = 0 if cfg.get("leave_enabled") else 1
        update_guild_config(self.guild_id, leave_enabled=new)
        await interaction.response.send_message(
            f"🚪 Départ : {'🟢 activé' if new else '🔴 désactivé'}",
            ephemeral=True
        )


class ConfigAutoroleView(ConfigBackView):
    @discord.ui.button(label="Choisir le rôle", style=discord.ButtonStyle.primary, emoji="🏷️", row=0)
    @auto_interaction_response
    async def choose_role(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.send_message(
            "🏷️ Choisis le rôle automatique :",
            view=ConfigRoleFieldView(self.guild_id, self.user_id, "autorole_id", "Autorôle"),
            ephemeral=True
        )


class ConfigModerationView(ConfigBackView):
    @discord.ui.button(label="Logs", style=discord.ButtonStyle.primary, emoji="📜", row=0)
    @auto_interaction_response
    async def logs(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.send_message(
            "📜 Choisis le salon des logs :",
            view=ConfigChannelPicker(self.guild_id, self.user_id, "log_channel_id", "Logs"),
            ephemeral=True
        )

    @discord.ui.button(label="Modlogs", style=discord.ButtonStyle.primary, emoji="🛡️", row=0)
    @auto_interaction_response
    async def modlogs(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.send_message(
            "🛡️ Choisis le salon des logs de modération :",
            view=ConfigChannelPicker(self.guild_id, self.user_id, "modlog_channel_id", "Modlogs"),
            ephemeral=True
        )

    @discord.ui.button(label="Anti-liens", style=discord.ButtonStyle.secondary, emoji="🔗", row=1)
    @auto_interaction_response
    async def links(self, interaction, button):
        if not await self.check_user(interaction):
            return
        cfg = get_guild_config(self.guild_id)
        new = 0 if cfg.get("anti_links") else 1
        update_guild_config(self.guild_id, anti_links=new)
        await interaction.response.send_message(f"{'🟢 Anti-liens activé.' if new else '🔴 Anti-liens désactivé.'}", ephemeral=True)

    @discord.ui.button(label="Anti-spam", style=discord.ButtonStyle.secondary, emoji="🚨", row=1)
    @auto_interaction_response
    async def spam(self, interaction, button):
        if not await self.check_user(interaction):
            return
        cfg = get_guild_config(self.guild_id)
        new = 0 if cfg.get("anti_spam") else 1
        update_guild_config(self.guild_id, anti_spam=new)
        await interaction.response.send_message(f"{'🟢 Anti-spam activé.' if new else '🔴 Anti-spam désactivé.'}", ephemeral=True)


class ConfigChannelPicker(discord.ui.View):
    def __init__(self, guild_id, user_id, field, label):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id
        self.field = field
        self.label = label

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Choisis un salon...", channel_types=[discord.ChannelType.text])
    @auto_interaction_response
    async def choose(self, interaction, select):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        ch = select.values[0]

        # La création/suppression du panneau peut prendre plusieurs secondes.
        # On répond immédiatement à Discord pour éviter « AuraBot n'a pas pu répondre à temps ».
        if self.field == "confession_channel_id":
            await interaction.response.defer(ephemeral=True)

        # Si on déplace les confessions, on retire l'ancien panneau quand c'est possible.
        old_cfg = get_guild_config(self.guild_id)
        old_channel_id = old_cfg.get("confession_channel_id")
        old_panel_id = old_cfg.get("confession_panel_message_id")

        ok = update_guild_config(self.guild_id, **{self.field: str(ch.id)})
        if not ok:
            return await interaction.response.edit_message(content="❌ Impossible d'enregistrer ce réglage.", embed=None, view=None)

        if self.field == "confession_channel_id":
            try:
                if old_channel_id and old_panel_id:
                    old_channel = interaction.guild.get_channel(int(old_channel_id))
                    if old_channel:
                        old_panel = await old_channel.fetch_message(int(old_panel_id))
                        await old_panel.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                pass

            try:
                embed = discord.Embed(
                    title="🕶️ Confessions anonymes",
                    description=(
                        "Tu veux dire quelque chose anonymement ?\n\n"
                        "Clique sur **Se confesser** pour ouvrir le formulaire.\n"
                        "Ta confession sera publiée anonymement et un fil sera créé pour les réponses."
                    ),
                    color=discord.Color.dark_gray()
                )
                panel = await ch.send(embed=embed, view=ConfessionPanelView())
                update_guild_config(self.guild_id, confession_panel_message_id=str(panel.id))
                await interaction.edit_original_response(
                    content=f"✅ **{self.label}** → {ch.mention}\n🕶️ Le panneau de confession a été publié dans ce salon.",
                    embed=None, view=None
                )
            except (discord.Forbidden, discord.HTTPException):
                update_guild_config(self.guild_id, confession_panel_message_id=None)
                await interaction.edit_original_response(
                    content=f"⚠️ **{self.label}** → {ch.mention}, mais AuraBot ne peut pas publier le panneau dans ce salon. Vérifie **Envoyer des messages** et **Intégrer des liens**.",
                    embed=None, view=None
                )
        else:
            await interaction.response.edit_message(content=f"✅ **{self.label}** → {ch.mention}", embed=None, view=None)
        self.stop()

    @discord.ui.button(label="Retirer la configuration", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    @auto_interaction_response
    async def clear(self, interaction, button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        update_guild_config(self.guild_id, **{self.field: None})
        await interaction.response.edit_message(content=f"🗑️ **{self.label}** n’est plus configuré.", embed=None, view=None)
        self.stop()


def get_ticket_reasons(guild):
    cfg = get_guild_config(guild.id)
    raw = cfg.get("ticket_categories") or "[]"
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        data = []
    reasons = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, str):
            name = item.strip()
            emoji = "🎫"
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            emoji = str(item.get("emoji") or "🎫")[:2]
        else:
            continue
        if name and name.lower() not in [x["name"].lower() for x in reasons]:
            reasons.append({"name": name[:80], "emoji": emoji or "🎫"})
    return reasons


def ticket_channel_name(reason, member):
    # Discord n'autorise pas les espaces/brackets/@ dans les noms de salons.
    # On conserve donc la forme [raison] [@membre] sous une forme valide : raison-membre.
    import re
    r = re.sub(r"[^a-zA-Z0-9_-]+", "-", reason.lower()).strip("-") or "ticket"
    m = re.sub(r"[^a-zA-Z0-9_-]+", "-", member.display_name.lower()).strip("-") or "membre"
    return f"{r}-{m}"[:100]


async def send_ticket_panel(guild, channel):
    cfg = get_guild_config(guild.id)
    embed = discord.Embed(
        title=cfg.get("ticket_panel_title") or DEFAULT_TICKET_PANEL_TITLE,
        description=replace_config_variables(cfg.get("ticket_panel_description") or DEFAULT_TICKET_PANEL_DESCRIPTION, guild.me, guild),
        color=discord.Color.blurple()
    )
    apply_server_appearance(embed, guild, cfg)
    view = TicketPanelView(guild)
    old_id = cfg.get("ticket_panel_message_id")
    if old_id:
        try:
            old_channel = guild.get_channel(int(cfg.get("ticket_panel_channel_id"))) if cfg.get("ticket_panel_channel_id") else None
            old_message = await old_channel.fetch_message(int(old_id)) if old_channel else None
            if old_message:
                await old_message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, TypeError):
            pass
    message = await channel.send(embed=embed, view=view)
    update_guild_config(guild.id, ticket_panel_channel_id=str(channel.id), ticket_panel_message_id=str(message.id))
    return message


class ConfigCategoryPicker(discord.ui.View):
    def __init__(self, guild_id, user_id):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Choisis une catégorie...", channel_types=[discord.ChannelType.category])
    @auto_interaction_response
    async def choose(self, interaction, select):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        ch = select.values[0]
        update_guild_config(self.guild_id, ticket_category_id=str(ch.id))
        await interaction.response.edit_message(content=f"✅ Catégorie tickets → **{ch.name}**", embed=None, view=None)
        self.stop()

    @discord.ui.button(label="Retirer la catégorie", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    @auto_interaction_response
    async def clear(self, interaction, button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        update_guild_config(self.guild_id, ticket_category_id=None)
        await interaction.response.edit_message(content="🗑️ Catégorie tickets retirée.", view=None)
        self.stop()


def get_ticket_staff_roles(guild):
    cfg = get_guild_config(guild.id)
    raw = cfg.get("ticket_staff_role_ids") or cfg.get("ticket_staff_role_id") or ""
    roles = []
    for part in str(raw).split(","):
        try:
            role = guild.get_role(int(part.strip()))
            if role and role not in roles:
                roles.append(role)
        except (ValueError, TypeError):
            pass
    return roles


class ConfigTicketStaffRolesPicker(discord.ui.View):
    def __init__(self, guild_id, user_id):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id
        self.select = discord.ui.RoleSelect(placeholder="Choisis les rôles staff...", min_values=1, max_values=10, row=0)
        self.select.callback = self.choose
        self.add_item(self.select)
        clear = discord.ui.Button(label="Aucun rôle", style=discord.ButtonStyle.secondary, emoji="🗑️", row=1)
        clear.callback = self.clear
        self.add_item(clear)

    @auto_interaction_response
    async def clear(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        update_guild_config(self.guild_id, ticket_staff_role_ids="", ticket_staff_role_id=None)
        await interaction.response.edit_message(content="🗑️ Aucun rôle staff configuré.", view=None)
        self.stop()

    @auto_interaction_response
    async def choose(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        roles = list(self.select.values)
        ids = ",".join(str(r.id) for r in roles)
        update_guild_config(self.guild_id, ticket_staff_role_ids=ids, ticket_staff_role_id=(str(roles[0].id) if roles else None))
        names = ", ".join(r.mention for r in roles)
        await interaction.response.edit_message(content=f"✅ Rôles staff tickets → {names}\nCes mêmes rôles auront accès aux tickets et seront ping à leur création.", view=None)
        self.stop()


class ConfigTicketCategoriesModal(discord.ui.Modal, title="Boutons de tickets"):
    def __init__(self, guild_id, user_id):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        current = get_ticket_reasons(discord.utils.get(bot.guilds, id=guild_id)) if discord.utils.get(bot.guilds, id=guild_id) else []
        text = "\n".join(f"{x['emoji']} {x['name']}" for x in current)
        self.categories = discord.ui.TextInput(
            label="Un bouton / une raison par ligne",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
            placeholder="🛠️ Support\n🐛 Bug\n🤝 Partenariat\n💳 Remboursement",
            default=text[:1000]
        )
        self.add_item(self.categories)

    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        reasons = []
        for line in str(self.categories.value).splitlines():
            name = line.strip()
            if not name:
                continue
            if name.startswith("<"):
                name = name.lstrip("<:0123456789>")
            if name and name.lower() not in [x["name"].lower() for x in reasons]:
                reasons.append({"name": name[:80], "emoji": "🎫"})
        if len(reasons) > 24:
            return await interaction.response.send_message("❌ Maximum 24 catégories + l'option raison personnalisée.", ephemeral=True)
        update_guild_config(self.guild_id, ticket_categories=json.dumps(reasons, ensure_ascii=False))
        await interaction.response.send_message(f"✅ {len(reasons)} bouton(s) de ticket enregistré(s).", ephemeral=True)


class ConfigTicketPanelChannelPicker(discord.ui.View):
    def __init__(self, guild_id, user_id):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id
        self.select = discord.ui.ChannelSelect(placeholder="Choisis le salon du panel...", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
        self.select.callback = self.choose
        self.add_item(self.select)

    @auto_interaction_response
    async def choose(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        selected = self.select.values[0]
        # ChannelSelect peut retourner un AppCommandChannel, qui sert uniquement
        # à identifier le salon et ne possède pas les méthodes de TextChannel.
        # On récupère donc le vrai salon depuis le cache/API Discord.
        channel = interaction.guild.get_channel(selected.id)
        if channel is None:
            try:
                channel = await interaction.guild.fetch_channel(selected.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                return await interaction.response.edit_message(
                    content=f"❌ Impossible de récupérer ce salon : `{type(e).__name__}`.",
                    view=None
                )
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.edit_message(
                content="❌ Le salon sélectionné n'est pas un salon textuel utilisable pour un panel.",
                view=None
            )
        try:
            await interaction.response.defer(ephemeral=True)
            await send_ticket_panel(interaction.guild, channel)
            await interaction.edit_original_response(content=f"✅ Panel tickets publié dans {channel.mention}.", view=None)
        except (discord.Forbidden, discord.HTTPException) as e:
            if interaction.response.is_done():
                await interaction.edit_original_response(content=f"❌ Impossible de publier le panel dans {channel.mention} : `{type(e).__name__}`.", view=None)
            else:
                await interaction.response.send_message(content=f"❌ Impossible de publier le panel dans {channel.mention} : `{type(e).__name__}`.", ephemeral=True)
        self.stop()


class ConfigRolePicker(discord.ui.View):
    def __init__(self, guild_id, user_id):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Choisis les rôles staff...", min_values=1, max_values=10)
    @auto_interaction_response
    async def choose(self, interaction, select):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        roles = list(select.values)
        ids = ",".join(str(r.id) for r in roles)
        update_guild_config(self.guild_id, ticket_staff_role_ids=ids, ticket_staff_role_id=str(roles[0].id))
        names = ", ".join(r.mention for r in roles)
        await interaction.response.edit_message(content=f"✅ Rôles staff tickets → {names}\nCes mêmes rôles auront accès aux tickets et seront ping à leur création.", embed=None, view=None)
        self.stop()


class ConfigMessageModal(discord.ui.Modal):
    def __init__(self, guild_id, user_id, field, title, current):
        super().__init__(title=title[:45])
        self.guild_id = guild_id
        self.user_id = user_id
        self.field = field
        self.message_input = discord.ui.TextInput(
            label="Message",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
            default=(current or "")[:1000]
        )
        self.add_item(self.message_input)

    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        update_guild_config(self.guild_id, **{self.field: str(self.message_input.value)})
        await interaction.response.send_message("✅ Message enregistré.", ephemeral=True)


class ConfigTextModal(discord.ui.Modal):
    def __init__(self, guild_id, user_id, fields, title):
        super().__init__(title=title[:45])
        self.guild_id = guild_id
        self.user_id = user_id
        self.inputs = []
        for key, label, current in fields:
            inp = discord.ui.TextInput(
                label=label,
                style=discord.TextStyle.paragraph,
                required=True,
                max_length=1000,
                default=(current or "")[:1000]
            )
            self.inputs.append((key, inp))
            self.add_item(inp)

    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        update_guild_config(self.guild_id, **{k: str(inp.value) for k, inp in self.inputs})
        await interaction.response.send_message("✅ Paramètres enregistrés.", ephemeral=True)


class ConfigRoleFieldView(discord.ui.View):
    def __init__(self, guild_id, user_id, field, label):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id
        self.field = field
        self.label = label

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Choisis un rôle...")
    @auto_interaction_response
    async def choose(self, interaction, select):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        role = select.values[0]
        if self.field == "autorole_id" and interaction.guild.me and role >= interaction.guild.me.top_role:
            return await interaction.response.send_message("❌ Le rôle doit être sous le rôle d'AuraBot.", ephemeral=True)
        update_guild_config(self.guild_id, **{self.field: str(role.id)})
        await interaction.response.edit_message(content=f"✅ {self.label} → {role.mention}", view=None)
        self.stop()

    @discord.ui.button(label="Retirer le rôle", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    @auto_interaction_response
    async def clear(self, interaction, button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        update_guild_config(self.guild_id, **{self.field: None})
        await interaction.response.edit_message(content=f"🗑️ **{self.label}** n’est plus configuré.", view=None)
        self.stop()


class TicketConfigView(ConfigBackView):
    @discord.ui.button(label="Catégorie Discord", style=discord.ButtonStyle.primary, emoji="📁", row=0)
    @auto_interaction_response
    async def category(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.send_message("📁 Choisis la catégorie Discord où les tickets seront créés :", view=ConfigCategoryPicker(self.guild_id, self.user_id), ephemeral=True)

    @discord.ui.button(label="Salon du panel", style=discord.ButtonStyle.primary, emoji="📌", row=0)
    @auto_interaction_response
    async def panel_channel(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.send_message("📌 Choisis le salon où publier le panel tickets :", view=ConfigTicketPanelChannelPicker(self.guild_id, self.user_id), ephemeral=True)

    @discord.ui.button(label="Rôles staff", style=discord.ButtonStyle.primary, emoji="🛡️", row=0)
    @auto_interaction_response
    async def staff(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.send_message("🛡️ Ces rôles auront accès aux tickets ET seront ping à leur ouverture. Tu peux en sélectionner plusieurs :", view=ConfigTicketStaffRolesPicker(self.guild_id, self.user_id), ephemeral=True)

    @discord.ui.button(label="Boutons / raisons", style=discord.ButtonStyle.primary, emoji="🗂️", row=1)
    @auto_interaction_response
    async def reasons(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.send_modal(ConfigTicketCategoriesModal(self.guild_id, self.user_id))

    @discord.ui.button(label="Panel", style=discord.ButtonStyle.success, emoji="🖼️", row=1)
    @auto_interaction_response
    async def customize_panel(self, interaction, button):
        if not await self.check_user(interaction): return
        cfg = get_guild_config(self.guild_id)
        await interaction.response.send_modal(ConfigTextModal(self.guild_id, self.user_id, [("ticket_panel_title", "Titre du panel", cfg.get("ticket_panel_title")), ("ticket_panel_description", "Description du panel", cfg.get("ticket_panel_description"))], "Personnaliser le panel"))

    @discord.ui.button(label="Message du ticket", style=discord.ButtonStyle.success, emoji="✏️", row=1)
    @auto_interaction_response
    async def customize_ticket(self, interaction, button):
        if not await self.check_user(interaction): return
        cfg = get_guild_config(self.guild_id)
        await interaction.response.send_modal(ConfigTextModal(self.guild_id, self.user_id, [("ticket_open_title", "Titre du ticket", cfg.get("ticket_open_title")), ("ticket_open_description", "Message du ticket", cfg.get("ticket_open_description")), ("ticket_close_label", "Bouton fermeture", cfg.get("ticket_close_label"))], "Personnaliser le ticket"))


class ConfessionConfigView(ConfigBackView):
    @discord.ui.button(label="Salon confessions", style=discord.ButtonStyle.primary, emoji="🕶️", row=0)
    @auto_interaction_response
    async def confession(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.send_message("🕶️ Choisis le salon :", view=ConfigChannelPicker(self.guild_id, self.user_id, "confession_channel_id", "Confessions"), ephemeral=True)

    @discord.ui.button(label="Salon reports", style=discord.ButtonStyle.danger, emoji="🚩", row=0)
    @auto_interaction_response
    async def reports(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.send_message("🚩 Choisis le salon :", view=ConfigChannelPicker(self.guild_id, self.user_id, "confession_report_channel_id", "Reports confessions"), ephemeral=True)


class ConfigVoiceChannelPicker(discord.ui.View):
    def __init__(self, guild_id, user_id, field, label, channel_type):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id
        self.field = field
        self.label = label
        self.select = discord.ui.ChannelSelect(
            placeholder=f"Choisis {label.lower()}...",
            channel_types=[channel_type],
            min_values=1,
            max_values=1
        )
        self.select.callback = self.choose
        self.add_item(self.select)

    @auto_interaction_response
    async def choose(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        channel = self.select.values[0]
        update_guild_config(self.guild_id, **{self.field: str(channel.id)})
        await interaction.response.edit_message(content=f"✅ {self.label} → {channel.mention}", view=None)
        self.stop()


class TempVoiceConfigView(ConfigBackView):
    @discord.ui.button(label="Activer / désactiver", style=discord.ButtonStyle.success, emoji="🔘", row=0)
    @auto_interaction_response
    async def toggle(self, interaction, button):
        if not await self.check_user(interaction):
            return
        cfg = get_guild_config(self.guild_id)
        new = 0 if cfg.get("temp_voice_enabled") else 1
        update_guild_config(self.guild_id, temp_voice_enabled=new)
        await interaction.response.send_message(
            f"{'🟢 Salons vocaux temporaires activés.' if new else '🔴 Salons vocaux temporaires désactivés.'}",
            ephemeral=True
        )

    @discord.ui.button(label="Salon déclencheur", style=discord.ButtonStyle.primary, emoji="🎙️", row=0)
    @auto_interaction_response
    async def trigger(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.send_message(
            "🎙️ Choisis le salon vocal que les membres doivent rejoindre pour créer leur salon :",
            view=ConfigVoiceChannelPicker(self.guild_id, self.user_id, "temp_voice_create_channel_id", "le salon déclencheur", discord.ChannelType.voice),
            ephemeral=True
        )

    @discord.ui.button(label="Catégorie", style=discord.ButtonStyle.primary, emoji="📁", row=0)
    @auto_interaction_response
    async def category(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.send_message(
            "📁 Choisis la catégorie où créer les salons :",
            view=ConfigVoiceChannelPicker(self.guild_id, self.user_id, "temp_voice_category_id", "la catégorie", discord.ChannelType.category),
            ephemeral=True
        )

    @discord.ui.button(label="Voir la configuration", style=discord.ButtonStyle.secondary, emoji="📋", row=1)
    @auto_interaction_response
    async def status(self, interaction, button):
        if not await self.check_user(interaction):
            return
        cfg = get_guild_config(self.guild_id)
        trigger = interaction.guild.get_channel(int(cfg["temp_voice_create_channel_id"])) if cfg.get("temp_voice_create_channel_id") else None
        category = interaction.guild.get_channel(int(cfg["temp_voice_category_id"])) if cfg.get("temp_voice_category_id") else None
        embed = discord.Embed(title="🔊 Configuration des vocaux temporaires", color=discord.Color.blurple())
        embed.description = (
            f"**État :** {'🟢 Activé' if cfg.get('temp_voice_enabled') else '🔴 Désactivé'}\n"
            f"**Déclencheur :** {trigger.mention if trigger else '❌ Non configuré'}\n"
            f"**Catégorie :** {category.mention if category else '❌ Non configurée'}\n\n"
            "À chaque création, le propriétaire reçoit son panneau de gestion privé."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BotAppearanceModal(discord.ui.Modal):
    def __init__(self, guild_id, user_id, cfg):
        super().__init__(title="Apparence AuraBot • serveur")
        self.guild_id = guild_id
        self.user_id = user_id

        self.name = discord.ui.TextInput(
            label="Nom du bot sur ce serveur",
            required=False,
            max_length=32,
            placeholder="Ex : AuraBot • Aura Vibe",
            default=(cfg.get("bot_server_name") or "")[:32]
        )
        self.color = discord.ui.TextInput(
            label="Couleur des embeds (#RRGGBB)",
            required=False,
            max_length=7,
            placeholder="#5865F2",
            default=(cfg.get("bot_server_color") or "#5865F2")[:7]
        )
        self.avatar = discord.ui.TextInput(
            label="URL de l'image / avatar",
            required=False,
            max_length=500,
            placeholder="https://...",
            default=(cfg.get("bot_server_avatar_url") or "")[:500]
        )
        self.footer = discord.ui.TextInput(
            label="Footer des messages",
            required=False,
            max_length=200,
            placeholder="AuraBot • Aura Vibe",
            default=(cfg.get("bot_server_footer") or "AuraBot")[:200]
        )
        for item in (self.name, self.color, self.avatar, self.footer):
            self.add_item(item)

    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)

        color = str(self.color.value).strip() or "#5865F2"
        if not color.startswith("#"):
            color = "#" + color
        if len(color) != 7:
            return await interaction.response.send_message("❌ La couleur doit être au format `#RRGGBB`.", ephemeral=True)
        try:
            int(color[1:], 16)
        except ValueError:
            return await interaction.response.send_message("❌ Couleur invalide. Exemple : `#5865F2`.", ephemeral=True)

        urls = {
            "avatar": str(self.avatar.value).strip(),
        }
        for label, url in urls.items():
            if url and not (url.startswith("https://") or url.startswith("http://")):
                return await interaction.response.send_message(
                    f"❌ L'URL **{label}** doit commencer par `https://` ou `http://`.",
                    ephemeral=True
                )

        update_guild_config(
            self.guild_id,
            bot_server_name=str(self.name.value).strip() or None,
            bot_server_color=color.upper(),
            bot_server_avatar_url=urls["avatar"] or None,
            bot_server_footer=str(self.footer.value).strip() or "AuraBot",
            bot_server_bio='Un serveur cool 🎮 et le serveur de support : https://discord.gg/6M8s4RvSHp',
        )

        guild = interaction.guild
        nickname = str(self.name.value).strip() or None
        bio = 'Un serveur cool 🎮 et le serveur de support : https://discord.gg/6M8s4RvSHp'
        profile_result = ""
        if guild is not None:
            try:
                me = guild.me or guild.get_member(interaction.client.user.id)
                if me:
                    # Depuis discord.py 2.7, Discord permet au bot de modifier
                    # son profil propre au serveur, dont la bio.
                    edit_kwargs = {"bio": bio, "reason": "Apparence AuraBot configurée pour ce serveur"}
                    if me.guild_permissions.change_nickname:
                        edit_kwargs["nick"] = nickname
                    await me.edit(**edit_kwargs)
                    profile_result = "👤 Bio du profil serveur appliquée."
            except TypeError:
                profile_result = "⚠️ Ta version de discord.py est trop ancienne pour appliquer la bio. Mets-la à jour vers une version compatible avec les profils serveur."
            except (discord.Forbidden, discord.HTTPException) as e:
                profile_result = f"⚠️ La bio n'a pas pu être appliquée au profil Discord : `{type(e).__name__}`."

        await interaction.response.send_message(
            "✅ **Apparence enregistrée.**\n\n"
            f"🎨 Couleur : `{color.upper()}`\n"
            f"🏷️ Nom : **{nickname or interaction.client.user.name}**\n"
            f"📝 Bio fixe : **{bio}**\n"
            "🖼️ Les images et le footer seront utilisés par les panneaux/messages compatibles.\n"
            f"{profile_result}",
            ephemeral=True
        )



class BotImageLinksModal(discord.ui.Modal):
    def __init__(self, guild_id, user_id, cfg):
        super().__init__(title="Images du profil serveur")
        self.guild_id = guild_id
        self.user_id = user_id
        self.avatar = discord.ui.TextInput(
            label="Lien de la PDP",
            required=False,
            max_length=500,
            placeholder="https://...",
            default=(cfg.get("bot_server_avatar_url") or "")[:500]
        )
        self.banner = discord.ui.TextInput(
            label="Lien de la bannière",
            required=False,
            max_length=500,
            placeholder="https://...",
            default=(cfg.get("bot_server_banner_url") or "")[:500]
        )
        self.add_item(self.avatar)
        self.add_item(self.banner)

    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)

        avatar_url = str(self.avatar.value).strip()
        banner_url = str(self.banner.value).strip()
        for label, url in (("PDP", avatar_url), ("bannière", banner_url)):
            if url and not (url.startswith("https://") or url.startswith("http://")):
                return await interaction.response.send_message(
                    f"❌ Le lien de la **{label}** doit commencer par `https://` ou `http://`.", ephemeral=True
                )

        update_guild_config(
            self.guild_id,
            bot_server_avatar_url=avatar_url or None,
            bot_server_banner_url=banner_url or None,
        )
        await interaction.response.send_message(
            "✅ Liens enregistrés. Tu peux aussi utiliser les boutons **📤 Envoyer PDP** et **📤 Envoyer bannière** pour choisir directement un fichier image depuis ton téléphone.",
            ephemeral=True
        )


async def wait_for_server_profile_image(interaction, image_type):
    channel = interaction.channel
    if channel is None:
        return await interaction.followup.send("❌ Impossible de recevoir le fichier dans ce salon.", ephemeral=True)

    label = "PDP" if image_type == "avatar" else "bannière"
    await interaction.followup.send(
        f"📤 Envoie maintenant **le fichier image de ta {label} dans ce salon**.\n"
        "Tu as **60 secondes**. Formats image uniquement.",
        ephemeral=True
    )

    def check(message):
        return (
            message.author.id == interaction.user.id
            and message.channel.id == channel.id
            and bool(message.attachments)
        )

    try:
        message = await bot.wait_for("message", timeout=60, check=check)
    except asyncio.TimeoutError:
        return await interaction.followup.send("⏰ Temps écoulé. Aucun fichier reçu.", ephemeral=True)

    attachment = message.attachments[0]
    content_type = (attachment.content_type or "").lower()
    filename = attachment.filename.lower()
    if not (content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))):
        return await interaction.followup.send("❌ Le fichier doit être une image PNG, JPG, WEBP ou GIF.", ephemeral=True)

    if attachment.size > 10 * 1024 * 1024:
        return await interaction.followup.send("❌ L'image est trop lourde (maximum 10 Mo).", ephemeral=True)

    data = await attachment.read()
    guild = interaction.guild
    me = guild.me if guild else None
    if me is None:
        return await interaction.followup.send("❌ Impossible de trouver le profil du bot sur ce serveur.", ephemeral=True)

    try:
        if image_type == "avatar":
            await me.edit(avatar=data, reason="PDP serveur AuraBot configurée")
            update_guild_config(interaction.guild.id, bot_server_avatar_url=attachment.url)
        else:
            await me.edit(banner=data, reason="Bannière serveur AuraBot configurée")
            update_guild_config(interaction.guild.id, bot_server_banner_url=attachment.url)
    except TypeError:
        return await interaction.followup.send("⚠️ Ta version de discord.py ne permet pas encore de modifier cette image sur le profil serveur. Mets discord.py à jour.", ephemeral=True)
    except discord.Forbidden:
        return await interaction.followup.send("❌ Discord a refusé la modification du profil du bot.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.followup.send(f"❌ Discord a refusé l'image : `{e}`", ephemeral=True)

    try:
        await message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass

    await interaction.followup.send(f"✅ **{label} modifiée avec succès** sur ce serveur.", ephemeral=True)


class AnimationConfigModal(discord.ui.Modal):
    def __init__(self, guild_id, user_id, cfg):
        super().__init__(title="Réglages des animations")
        self.guild_id = guild_id
        self.user_id = user_id
        self.max_players = discord.ui.TextInput(label="Joueurs maximum", required=True, max_length=2, default=str(cfg.get("animation_max_players", 20)))
        self.lg_wolves = discord.ui.TextInput(label="Loup-Garou : % de loups", required=True, max_length=2, default=str(cfg.get("lg_wolves_percent", 25)))
        self.lg_night = discord.ui.TextInput(label="Loup-Garou : durée nuit (sec)", required=True, max_length=3, default=str(cfg.get("lg_night_seconds", 45)))
        self.lg_vote = discord.ui.TextInput(label="Loup-Garou : durée vote (sec)", required=True, max_length=3, default=str(cfg.get("lg_vote_seconds", 45)))
        self.bomb = discord.ui.TextInput(label="BombParty : temps par tour (sec)", required=True, max_length=2, default=str(cfg.get("bomb_timer", 15)))
        for item in (self.max_players, self.lg_wolves, self.lg_night, self.lg_vote, self.bomb): self.add_item(item)

    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        try:
            maxp=max(2,min(20,int(str(self.max_players.value))))
            wolves=max(10,min(50,int(str(self.lg_wolves.value))))
            night=max(15,min(180,int(str(self.lg_night.value))))
            vote=max(15,min(180,int(str(self.lg_vote.value))))
            bomb=max(5,min(60,int(str(self.bomb.value))))
        except ValueError:
            return await interaction.response.send_message("❌ Vérifie les nombres saisis.", ephemeral=True)
        update_guild_config(self.guild_id, animation_max_players=maxp, lg_wolves_percent=wolves, lg_night_seconds=night, lg_vote_seconds=vote, bomb_timer=bomb)
        await interaction.response.send_message("✅ Réglages des animations enregistrés.", ephemeral=True)


class BombPartySyllablesModal(discord.ui.Modal):
    def __init__(self, guild_id, user_id, cfg):
        super().__init__(title="Syllabes BombParty")
        self.guild_id=guild_id; self.user_id=user_id
        current=cfg.get("bomb_syllables") or ""
        self.values=discord.ui.TextInput(label="Syllabes séparées par des virgules", style=discord.TextStyle.paragraph, required=False, max_length=400, default=current[:400], placeholder="an,ar,ba,che,ou,ra")
        self.add_item(self.values)
    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id!=self.user_id: return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.",ephemeral=True)
        raw=str(self.values.value).strip()
        vals=[x.strip().lower() for x in raw.split(",") if 1<=len(x.strip())<=8]
        if raw and len(vals)<2: return await interaction.response.send_message("❌ Il faut au moins 2 syllabes valides.",ephemeral=True)
        update_guild_config(self.guild_id,bomb_syllables=",".join(dict.fromkeys(vals)) if vals else "")
        await interaction.response.send_message("✅ Syllabes BombParty enregistrées." if vals else "✅ Liste personnalisée supprimée : liste par défaut réactivée.",ephemeral=True)


class UndercoverPairsModal(discord.ui.Modal):
    def __init__(self, guild_id, user_id, cfg):
        super().__init__(title="Mots Undercover")
        self.guild_id=guild_id; self.user_id=user_id
        current=cfg.get("undercover_pairs") or ""
        self.values=discord.ui.TextInput(label="Paires : mot1:mot2, mot3:mot4", style=discord.TextStyle.paragraph, required=False, max_length=900, default=current[:900], placeholder="chat:tigre,pizza:burger,plage:piscine")
        self.add_item(self.values)
    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id!=self.user_id: return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.",ephemeral=True)
        raw=str(self.values.value).strip(); pairs=[]
        if raw:
            for item in raw.split(","):
                if ":" not in item: continue
                a,b=[x.strip() for x in item.split(":",1)]
                if a and b and len(a)<=40 and len(b)<=40: pairs.append(f"{a}:{b}")
        if raw and len(pairs)<2: return await interaction.response.send_message("❌ Il faut au moins 2 paires valides au format `mot1:mot2`.",ephemeral=True)
        update_guild_config(self.guild_id,undercover_pairs=",".join(pairs) if pairs else "")
        await interaction.response.send_message("✅ Paires Undercover enregistrées." if pairs else "✅ Liste personnalisée supprimée : liste par défaut réactivée.",ephemeral=True)


class BombPartyConfigModal(discord.ui.Modal):
    def __init__(self, guild_id, user_id, cfg):
        super().__init__(title="Réglages BombParty")
        self.guild_id=guild_id; self.user_id=user_id
        self.lives=discord.ui.TextInput(label="Nombre de vies", required=True, max_length=2, default=str(cfg.get("bomb_lives",3)))
        self.add_item(self.lives)
    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id!=self.user_id: return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.",ephemeral=True)
        try: lives=max(1,min(9,int(str(self.lives.value))))
        except ValueError: return await interaction.response.send_message("❌ Entre un nombre entre 1 et 9.",ephemeral=True)
        update_guild_config(self.guild_id,bomb_lives=lives)
        await interaction.response.send_message(f"✅ BombParty : **{lives} vies**.",ephemeral=True)


class UndercoverConfigView(discord.ui.View):
    def __init__(self, guild_id, user_id):
        super().__init__(timeout=120); self.guild_id=guild_id; self.user_id=user_id
    @discord.ui.button(label="Mr White", style=discord.ButtonStyle.secondary, emoji="⬜")
    @auto_interaction_response
    async def white(self, interaction, button):
        if interaction.user.id!=self.user_id: return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.",ephemeral=True)
        cfg=get_guild_config(self.guild_id); new=0 if cfg.get("undercover_white_enabled") else 1; update_guild_config(self.guild_id,undercover_white_enabled=new)
        await interaction.response.edit_message(content=f"⬜ Mr White : **{'activé' if new else 'désactivé'}**.",view=self)


class AnimationsConfigView(ConfigBackView):
    @discord.ui.button(label="Activer / désactiver", style=discord.ButtonStyle.success, emoji="🔘", row=0)
    @auto_interaction_response
    async def toggle(self, interaction, button):
        if not await self.check_user(interaction): return
        cfg=get_guild_config(self.guild_id); new=0 if cfg.get("animations_enabled") else 1
        update_guild_config(self.guild_id, animations_enabled=new)
        await interaction.response.send_message(f"{'🟢 Animations activées.' if new else '🔴 Animations désactivées.'}", ephemeral=True)

    @discord.ui.button(label="Salon des animations", style=discord.ButtonStyle.primary, emoji="📺", row=0)
    @auto_interaction_response
    async def channel(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.send_message("📺 Choisis le salon dans lequel les animations peuvent être lancées :", view=ConfigChannelPicker(self.guild_id, self.user_id, "animations_channel_id", "Animations"), ephemeral=True)

    @discord.ui.button(label="Réglages", style=discord.ButtonStyle.primary, emoji="⚙️", row=1)
    @auto_interaction_response
    async def settings(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.send_modal(AnimationConfigModal(self.guild_id, self.user_id, get_guild_config(self.guild_id)))

    @discord.ui.button(label="Rôles Loup-Garou", style=discord.ButtonStyle.secondary, emoji="🐺", row=1)
    @auto_interaction_response
    async def roles(self, interaction, button):
        if not await self.check_user(interaction): return
        cfg=get_guild_config(self.guild_id)
        embed=discord.Embed(title="🐺 Loup-Garou — rôles", description=(
            f"Loups : **{cfg.get('lg_wolves_percent',25)}%** des joueurs\\n"
            f"Voyante : {'🟢 Oui' if cfg.get('lg_seer_enabled') else '🔴 Non'}\\n"
            f"Médecin : {'🟢 Oui' if cfg.get('lg_doctor_enabled') else '🔴 Non'}\\n\\n"
            "Les rôles sont distribués automatiquement et envoyés en message privé."
        ), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Réactions BombParty", style=discord.ButtonStyle.success, emoji="👍", row=3)
    @auto_interaction_response
    async def reactions(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.send_modal(BombReactionConfigModal(self.guild_id, self.user_id, get_guild_config(self.guild_id)))

    @discord.ui.button(label="BombParty", style=discord.ButtonStyle.secondary, emoji="💣", row=2)
    @auto_interaction_response
    async def bomb_settings(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.send_modal(BombPartyConfigModal(self.guild_id,self.user_id,get_guild_config(self.guild_id)))

    @discord.ui.button(label="Syllabes", style=discord.ButtonStyle.secondary, emoji="🔤", row=3)
    @auto_interaction_response
    async def syllables(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.send_modal(BombPartySyllablesModal(self.guild_id,self.user_id,get_guild_config(self.guild_id)))

    @discord.ui.button(label="Mots Undercover", style=discord.ButtonStyle.secondary, emoji="📝", row=3)
    @auto_interaction_response
    async def pairs(self, interaction, button):
        if not await self.check_user(interaction): return
        await interaction.response.send_modal(UndercoverPairsModal(self.guild_id,self.user_id,get_guild_config(self.guild_id)))

    @discord.ui.button(label="Undercover", style=discord.ButtonStyle.secondary, emoji="🕵️", row=2)
    @auto_interaction_response
    async def undercover_settings(self, interaction, button):
        if not await self.check_user(interaction): return
        cfg=get_guild_config(self.guild_id)
        await interaction.response.send_message(f"🕵️ **Undercover**\n\nMr White : **{'activé' if cfg.get('undercover_white_enabled') else 'désactivé'}**\n\nChoisis si Mr White peut être présent dans les parties de 6 joueurs ou plus.",view=UndercoverConfigView(self.guild_id,self.user_id),ephemeral=True)

    @discord.ui.button(label="Sous-options", style=discord.ButtonStyle.secondary, emoji="🧩", row=2)
    @auto_interaction_response
    async def subsettings(self, interaction, button):
        if not await self.check_user(interaction): return
        cfg=get_guild_config(self.guild_id)
        class V(discord.ui.View):
            def __init__(self, parent): super().__init__(timeout=120); self.parent=parent
            @discord.ui.button(label="Voyante", style=discord.ButtonStyle.success if cfg.get("lg_seer_enabled") else discord.ButtonStyle.secondary)
            async def seer(self, inter, b):
                if not await self.parent.check_user(inter): return
                new=0 if cfg.get("lg_seer_enabled") else 1; update_guild_config(self.parent.guild_id, lg_seer_enabled=new); b.label="Voyante ON" if new else "Voyante OFF"; b.style=discord.ButtonStyle.success if new else discord.ButtonStyle.secondary; await inter.response.edit_message(view=self)
            @discord.ui.button(label="Médecin", style=discord.ButtonStyle.success if cfg.get("lg_doctor_enabled") else discord.ButtonStyle.secondary)
            async def doctor(self, inter, b):
                if not await self.parent.check_user(inter): return
                new=0 if cfg.get("lg_doctor_enabled") else 1; update_guild_config(self.parent.guild_id, lg_doctor_enabled=new); b.label="Médecin ON" if new else "Médecin OFF"; b.style=discord.ButtonStyle.success if new else discord.ButtonStyle.secondary; await inter.response.edit_message(view=self)
        await interaction.response.send_message("🧩 Active ou désactive les rôles spéciaux :", view=V(self), ephemeral=True)

class BotAppearanceConfigView(ConfigBackView):
    @discord.ui.button(label="Modifier", style=discord.ButtonStyle.primary, emoji="🎨", row=0)
    @auto_interaction_response
    async def edit_appearance(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.send_modal(
            BotAppearanceModal(
                self.guild_id,
                self.user_id,
                get_guild_config(self.guild_id)
            )
        )

    @discord.ui.button(label="Images par lien", style=discord.ButtonStyle.secondary, emoji="🔗", row=0)
    @auto_interaction_response
    async def image_links(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.send_modal(
            BotImageLinksModal(self.guild_id, self.user_id, get_guild_config(self.guild_id))
        )

    @discord.ui.button(label="📤 Envoyer PDP", style=discord.ButtonStyle.secondary, row=1)
    @auto_interaction_response
    async def upload_avatar(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await wait_for_server_profile_image(interaction, "avatar")

    @discord.ui.button(label="📤 Envoyer bannière", style=discord.ButtonStyle.secondary, row=1)
    @auto_interaction_response
    async def upload_banner(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await wait_for_server_profile_image(interaction, "banner")

    @discord.ui.button(label="Aperçu", style=discord.ButtonStyle.secondary, emoji="👁️", row=2)
    @auto_interaction_response
    async def preview(self, interaction, button):
        if not await self.check_user(interaction):
            return
        cfg = get_guild_config(self.guild_id)
        guild = interaction.guild
        embed = discord.Embed(
            title=cfg.get("bot_server_name") or interaction.client.user.name,
            description=cfg.get("bot_server_description") or "Aucune description configurée.",
        )
        if cfg.get("bot_server_bio"):
            embed.add_field(name="Bio", value=cfg["bot_server_bio"], inline=False)
        apply_server_appearance(embed, guild, cfg, include_images=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Réinitialiser", style=discord.ButtonStyle.danger, emoji="♻️", row=2)
    @auto_interaction_response
    async def reset_appearance(self, interaction, button):
        if not await self.check_user(interaction):
            return
        update_guild_config(
            self.guild_id,
            bot_server_name=None,
            bot_server_avatar_url=None,
            bot_server_banner_url=None,
            bot_server_description=None,
            bot_server_bio="Un serveur cool 🎮 et le serveur de support : https://discord.gg/6M8s4RvSHp",
            bot_server_color="#5865F2",
            bot_server_footer="AuraBot",
        )
        guild = interaction.guild
        try:
            if guild and guild.me:
                await guild.me.edit(
                    nick=None if guild.me.guild_permissions.change_nickname else discord.utils.MISSING,
                    bio="Un serveur cool 🎮 et le serveur de support : https://discord.gg/6M8s4RvSHp",
                    reason="Réinitialisation apparence AuraBot"
                )
        except (discord.Forbidden, discord.HTTPException, TypeError):
            pass
        await interaction.response.send_message("♻️ **Apparence réinitialisée.** La bio par défaut du serveur a été remise.", ephemeral=True)


# =========================================================
# SALONS VOCAUX TEMPORAIRES
# =========================================================

def temp_voice_db_add(channel_id, guild_id, owner_id):
    with db_lock:
        conn = db_connect()
        conn.execute(
            "INSERT OR REPLACE INTO temp_voice_channels(channel_id, guild_id, owner_id, created_at) VALUES (?, ?, ?, ?)",
            (str(channel_id), str(guild_id), str(owner_id), datetime.now().isoformat())
        )
        conn.commit(); conn.close()


def temp_voice_db_get(channel_id):
    with db_lock:
        conn = db_connect()
        row = conn.execute("SELECT channel_id,guild_id,owner_id FROM temp_voice_channels WHERE channel_id=?", (str(channel_id),)).fetchone()
        conn.close()
    return row


def temp_voice_db_delete(channel_id):
    with db_lock:
        conn = db_connect()
        conn.execute("DELETE FROM temp_voice_channels WHERE channel_id=?", (str(channel_id),))
        conn.commit(); conn.close()


def temp_voice_db_all():
    with db_lock:
        conn = db_connect()
        rows = conn.execute("SELECT channel_id,owner_id FROM temp_voice_channels").fetchall()
        conn.close()
    return rows


class TempVoicePanel(discord.ui.View):
    def __init__(self, channel_id, owner_id):
        super().__init__(timeout=None)
        self.channel_id = int(channel_id)
        self.owner_id = int(owner_id)
        action_names = ["rename", "limit", "bitrate", "lock", "unlock", "region", "transfer", "delete"]
        for child, action in zip(self.children, action_names):
            if isinstance(child, discord.ui.Button):
                child.custom_id = f"aurabot:tempvoice:{self.channel_id}:{self.owner_id}:{action}"

    async def check_owner(self, interaction):
        """Vérifie le propriétaire et récupère le vocal sans dépendre de interaction.guild.

        Les interactions persistantes peuvent parfois arriver avec guild=None.
        On utilise alors guild_id + le cache global du bot, puis fetch_channel en dernier recours.
        """
        try:
            if interaction.user.id != self.owner_id:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Ce panneau est réservé au créateur de ce salon.", ephemeral=True
                    )
                return False

            guild = interaction.guild
            channel = guild.get_channel(self.channel_id) if guild else bot.get_channel(self.channel_id)

            if channel is None:
                try:
                    channel = await bot.fetch_channel(self.channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    channel = None

            if not isinstance(channel, discord.VoiceChannel):
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Le salon vocal n'existe plus ou n'est plus accessible.", ephemeral=True
                    )
                return False

            # Sécurité supplémentaire : le vocal doit appartenir au serveur du clic.
            if interaction.guild_id is not None and channel.guild.id != interaction.guild_id:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Ce panneau n'appartient pas à ce serveur.", ephemeral=True
                    )
                return False

            return channel
        except Exception as e:
            print(f"❌ TempVoice check_owner error ({self.channel_id}) : {type(e).__name__}: {e}")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        "❌ Impossible d'accéder à ce salon vocal. Réessaie.", ephemeral=True
                    )
                except Exception:
                    pass
            return False

    @discord.ui.button(label="Renommer", style=discord.ButtonStyle.primary, emoji="✏️", row=0)
    async def rename(self, interaction, button):
        channel = await self.check_owner(interaction)
        if not channel: return
        await interaction.response.send_modal(TempVoiceNameModal(self.channel_id, self.owner_id, channel.name))

    @discord.ui.button(label="Limite", style=discord.ButtonStyle.primary, emoji="👥", row=0)
    async def limit(self, interaction, button):
        channel = await self.check_owner(interaction)
        if not channel: return
        await interaction.response.send_modal(TempVoiceLimitModal(self.channel_id, self.owner_id, channel.user_limit))

    @discord.ui.button(label="Débit", style=discord.ButtonStyle.primary, emoji="📶", row=0)
    async def bitrate(self, interaction, button):
        channel = await self.check_owner(interaction)
        if not channel: return
        await interaction.response.send_modal(TempVoiceBitrateModal(self.channel_id, self.owner_id, channel.bitrate.value // 1000))

    @discord.ui.button(label="Verrouiller", style=discord.ButtonStyle.danger, emoji="🔒", row=1)
    @auto_interaction_response
    async def lock(self, interaction, button):
        channel = await self.check_owner(interaction)
        if not channel: return
        await interaction.response.defer(ephemeral=True)
        await channel.set_permissions(interaction.guild.default_role, connect=False, reason="Propriétaire du vocal temporaire")
        await interaction.followup.send("🔒 Salon verrouillé. Les membres déjà présents peuvent rester.", ephemeral=True)

    @discord.ui.button(label="Déverrouiller", style=discord.ButtonStyle.success, emoji="🔓", row=1)
    @auto_interaction_response
    async def unlock(self, interaction, button):
        channel = await self.check_owner(interaction)
        if not channel: return
        await interaction.response.defer(ephemeral=True)
        await channel.set_permissions(interaction.guild.default_role, connect=None, reason="Propriétaire du vocal temporaire")
        await interaction.followup.send("🔓 Salon déverrouillé.", ephemeral=True)

    @discord.ui.button(label="Région", style=discord.ButtonStyle.secondary, emoji="🌍", row=1)
    async def region(self, interaction, button):
        channel = await self.check_owner(interaction)
        if not channel: return
        await interaction.response.send_modal(TempVoiceRegionModal(self.channel_id, self.owner_id, channel.rtc_region or "auto"))

    @discord.ui.button(label="Transférer", style=discord.ButtonStyle.secondary, emoji="👤", row=2)
    @auto_interaction_response
    async def transfer(self, interaction, button):
        channel = await self.check_owner(interaction)
        if not channel: return
        await interaction.response.send_message("👤 Choisis le nouveau propriétaire :", view=TempVoiceTransferView(self.channel_id, self.owner_id), ephemeral=True)

    @discord.ui.button(label="Supprimer", style=discord.ButtonStyle.danger, emoji="🗑️", row=2)
    @auto_interaction_response
    async def delete(self, interaction, button):
        channel = await self.check_owner(interaction)
        if not channel: return
        await interaction.response.defer(ephemeral=True)
        temp_voice_db_delete(channel.id)
        await interaction.followup.send("🗑️ Suppression du salon...", ephemeral=True)
        try: await channel.delete(reason="Suppression par le propriétaire du vocal temporaire")
        except (discord.Forbidden, discord.HTTPException): pass


class TempVoiceNameModal(discord.ui.Modal):
    def __init__(self, channel_id, owner_id, current):
        super().__init__(title="Renommer le salon")
        self.channel_id=channel_id; self.owner_id=owner_id
        self.name=discord.ui.TextInput(label="Nouveau nom", required=True, max_length=100, default=current[:100])
        self.add_item(self.name)
    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.owner_id: return await interaction.response.send_message("❌ Panneau réservé au propriétaire.", ephemeral=True)
        ch=bot.get_channel(self.channel_id)
        if ch is None:
            try: ch=await bot.fetch_channel(self.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException): ch=None
        if not isinstance(ch, discord.VoiceChannel): return await interaction.response.send_message("❌ Salon introuvable.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await ch.edit(name=str(self.name.value).strip(), reason="Gestion du vocal temporaire")
        await interaction.followup.send("✅ Nom modifié.", ephemeral=True)


class TempVoiceLimitModal(discord.ui.Modal):
    def __init__(self, channel_id, owner_id, current):
        super().__init__(title="Limite de membres")
        self.channel_id=channel_id; self.owner_id=owner_id
        self.limit=discord.ui.TextInput(label="0 = illimité, 1 à 99 sinon", required=True, max_length=2, default=str(current))
        self.add_item(self.limit)
    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.owner_id: return await interaction.response.send_message("❌ Panneau réservé au propriétaire.", ephemeral=True)
        try: value=max(0,min(99,int(str(self.limit.value))))
        except ValueError: return await interaction.response.send_message("❌ Entre un nombre entre 0 et 99.", ephemeral=True)
        ch=bot.get_channel(self.channel_id)
        if ch is None:
            try: ch=await bot.fetch_channel(self.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException): ch=None
        if not isinstance(ch, discord.VoiceChannel): return await interaction.response.send_message("❌ Salon introuvable.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await ch.edit(user_limit=value, reason="Gestion du vocal temporaire")
        await interaction.followup.send(f"✅ Limite définie à **{value or 'illimitée'}**.", ephemeral=True)


class TempVoiceBitrateModal(discord.ui.Modal):
    def __init__(self, channel_id, owner_id, current):
        super().__init__(title="Débit du salon")
        self.channel_id=channel_id; self.owner_id=owner_id
        self.rate=discord.ui.TextInput(label="Débit en kbps", required=True, max_length=4, default=str(current))
        self.add_item(self.rate)
    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.owner_id: return await interaction.response.send_message("❌ Panneau réservé au propriétaire.", ephemeral=True)
        try: value=int(str(self.rate.value))
        except ValueError: return await interaction.response.send_message("❌ Débit invalide.", ephemeral=True)
        ch=bot.get_channel(self.channel_id)
        if ch is None:
            try: ch=await bot.fetch_channel(self.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException): ch=None
        if not isinstance(ch, discord.VoiceChannel): return await interaction.response.send_message("❌ Salon introuvable.", ephemeral=True)
        max_kbps=ch.guild.bitrate_limit//1000
        value=max(8,min(max_kbps,value))
        await interaction.response.defer(ephemeral=True)
        await ch.edit(bitrate=value*1000, reason="Gestion du vocal temporaire")
        await interaction.followup.send(f"✅ Débit défini à **{value} kbps**.", ephemeral=True)


class TempVoiceRegionModal(discord.ui.Modal):
    def __init__(self, channel_id, owner_id, current):
        super().__init__(title="Région du salon")
        self.channel_id=channel_id; self.owner_id=owner_id
        self.region=discord.ui.TextInput(label="auto, eu-central, etc.", required=True, max_length=32, default=current)
        self.add_item(self.region)
    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.owner_id: return await interaction.response.send_message("❌ Panneau réservé au propriétaire.", ephemeral=True)
        value=str(self.region.value).strip().lower()
        if value == "auto": value=None
        ch=bot.get_channel(self.channel_id)
        if ch is None:
            try: ch=await bot.fetch_channel(self.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException): ch=None
        if not isinstance(ch, discord.VoiceChannel): return await interaction.response.send_message("❌ Salon introuvable.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            await ch.edit(rtc_region=value, reason="Gestion du vocal temporaire")
        except discord.HTTPException:
            return await interaction.followup.send("❌ Région invalide ou non disponible.", ephemeral=True)
        await interaction.followup.send("✅ Région modifiée.", ephemeral=True)


class TempVoiceTransferView(discord.ui.View):
    def __init__(self, channel_id, owner_id):
        super().__init__(timeout=120)
        self.channel_id=channel_id; self.owner_id=owner_id
        self.select=discord.ui.UserSelect(placeholder="Choisis le nouveau propriétaire...", min_values=1, max_values=1)
        self.select.callback=self.choose; self.add_item(self.select)
    @auto_interaction_response
    async def choose(self, interaction):
        if interaction.user.id != self.owner_id: return await interaction.response.send_message("❌ Panneau réservé au propriétaire.", ephemeral=True)
        member=self.select.values[0]
        ch=bot.get_channel(self.channel_id)
        if ch is None:
            try: ch=await bot.fetch_channel(self.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException): ch=None
        if not isinstance(ch, discord.VoiceChannel): return await interaction.response.send_message("❌ Salon introuvable.", ephemeral=True)
        if member.voice is None or member.voice.channel != ch:
            return await interaction.response.send_message("❌ Le nouveau propriétaire doit être dans ce salon vocal.", ephemeral=True)
        temp_voice_db_add(ch.id, ch.guild.id, member.id)
        await interaction.response.edit_message(content=f"✅ Propriétaire transféré à {member.mention}.", view=None)
        try:
            await member.send(f"🔊 Tu es maintenant propriétaire de **{ch.name}** sur **{ch.guild.name}**.", view=TempVoicePanel(ch.id, member.id))
        except (discord.Forbidden, discord.HTTPException): pass



class CustomCommandModal(discord.ui.Modal, title="Créer / modifier une commande"):
    def __init__(self, guild_id, user_id, existing=None):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.existing = existing
        self.command_name = discord.ui.TextInput(
            label="Nom de la commande",
            placeholder="Ex : règles",
            required=True,
            max_length=32,
            default=(existing["name"] if existing else "")
        )
        self.response_text = discord.ui.TextInput(
            label="Réponse",
            style=discord.TextStyle.paragraph,
            placeholder="Ex : Bienvenue {mention} !",
            required=True,
            max_length=2000,
            default=(existing["response"] if existing else "")
        )
        self.add_item(self.command_name)
        self.add_item(self.response_text)

    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        name = str(self.command_name.value).strip().lower()
        name = name.lstrip("!/")
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
            return await interaction.response.send_message(
                "❌ Nom invalide. Utilise uniquement des lettres, chiffres, `_` ou `-`.", ephemeral=True
            )
        reserved = {c.name for c in bot.tree.get_commands()}
        if name in reserved:
            return await interaction.response.send_message(
                f"❌ `{name}` est déjà une commande native d'AuraBot.", ephemeral=True
            )
        save_custom_command(self.guild_id, name, str(self.response_text.value).strip())
        await interaction.response.send_message(
            f"✅ Commande **!{name}** enregistrée.\n\nElle répondra avec le texte configuré dès qu'un membre écrit `!{name}`.",
            ephemeral=True
        )


class CustomCommandDeleteView(ConfigBackView):
    def __init__(self, guild_id, user_id):
        super().__init__(guild_id, user_id)
        commands_list = get_custom_commands(guild_id)
        options = [
            discord.SelectOption(
                label=f"!{item['name']}"[:100],
                value=item['name'],
                description=item['response'][:100] or "Aucune réponse"
            )
            for item in commands_list[:25]
        ]
        if not options:
            options = [discord.SelectOption(label="Aucune commande", value="__none__")]
        self.select = discord.ui.Select(
            placeholder="Choisis une commande à supprimer...",
            min_values=1,
            max_values=1,
            options=options,
            row=0
        )
        self.select.callback = self.delete_selected
        self.add_item(self.select)

    @auto_interaction_response
    async def delete_selected(self, interaction):
        if not await self.check_user(interaction):
            return
        name = self.select.values[0]
        if name == "__none__":
            return await interaction.response.send_message("❌ Aucune commande personnalisée.", ephemeral=True)
        if delete_custom_command(self.guild_id, name):
            await interaction.response.edit_message(
                content=f"🗑️ La commande **!{name}** a été supprimée.",
                embed=None,
                view=CustomCommandsConfigView(self.guild_id, self.user_id)
            )
        else:
            await interaction.response.send_message("❌ Commande introuvable.", ephemeral=True)


class CustomCommandsConfigView(ConfigBackView):
    @discord.ui.button(label="Créer une commande", style=discord.ButtonStyle.success, emoji="➕", row=0)
    @auto_interaction_response
    async def create(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.send_modal(CustomCommandModal(self.guild_id, self.user_id))

    @discord.ui.button(label="Modifier une commande", style=discord.ButtonStyle.primary, emoji="✏️", row=0)
    @auto_interaction_response
    async def edit(self, interaction, button):
        if not await self.check_user(interaction):
            return
        commands_list = get_custom_commands(self.guild_id)
        options = [discord.SelectOption(label=f"!{item['name']}"[:100], value=item['name']) for item in commands_list[:25]]
        if not options:
            return await interaction.response.send_message("❌ Aucune commande personnalisée à modifier.", ephemeral=True)
        select = discord.ui.Select(placeholder="Choisis une commande...", options=options)
        async def callback(inter):
            if not await self.check_user(inter):
                return
            selected = get_custom_command(self.guild_id, select.values[0])
            if not selected:
                return await inter.response.send_message("❌ Commande introuvable.", ephemeral=True)
            await inter.response.send_modal(CustomCommandModal(self.guild_id, self.user_id, selected))
        select.callback = callback
        view = discord.ui.View(timeout=120)
        view.add_item(select)
        await interaction.response.send_message("✏️ Choisis la commande à modifier :", view=view, ephemeral=True)

    @discord.ui.button(label="Supprimer une commande", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    @auto_interaction_response
    async def delete(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.edit_message(
            content="🗑️ Choisis la commande à supprimer :",
            embed=None,
            view=CustomCommandDeleteView(self.guild_id, self.user_id)
        )

    @discord.ui.button(label="Voir les commandes", style=discord.ButtonStyle.secondary, emoji="📋", row=1)
    @auto_interaction_response
    async def listing(self, interaction, button):
        if not await self.check_user(interaction):
            return
        commands_list = get_custom_commands(self.guild_id)
        if not commands_list:
            description = "Aucune commande personnalisée."
        else:
            lines = []
            for item in commands_list[:25]:
                preview = item["response"].replace("\n", " ")[:120]
                lines.append(f"**!{item['name']}** → {preview}")
            description = "\n".join(lines)
        embed = discord.Embed(title="📋 Commandes personnalisées", description=description, color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ConfigResetView(ConfigBackView):
    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.danger, emoji="♻️", row=0)
    @auto_interaction_response
    async def confirm(self, interaction, button):
        if not await self.check_user(interaction):
            return
        update_guild_config(
            self.guild_id,
            welcome_channel_id=None,
            welcome_message=DEFAULT_WELCOME,
            leave_channel_id=None,
            leave_message=DEFAULT_LEAVE,
            autorole_id=None,
            log_channel_id=None,
            modlog_channel_id=None,
            suggestions_channel_id=None,
            commands_channel_id=None,
            confession_channel_id=None,
            confession_report_channel_id=None,
            ticket_category_id=None,
            ticket_log_channel_id=None,
            ticket_staff_role_id=None,
            ticket_staff_role_ids=None,
            temp_voice_enabled=0,
            temp_voice_create_channel_id=None,
            temp_voice_category_id=None,
            bot_server_name=None,
            bot_server_avatar_url=None,
            bot_server_description=None,
            bot_server_bio="Un serveur cool 🎮 et le serveur de support : https://discord.gg/6M8s4RvSHp",
            bot_server_banner_url=None,
            bot_server_color="#5865F2",
            bot_server_footer="AuraBot",
            animations_enabled=1,
            animations_channel_id=None,
            animation_max_players=20,
            lg_wolves_percent=25,
            lg_night_seconds=45,
            lg_vote_seconds=45,
            lg_doctor_enabled=1,
            lg_seer_enabled=1,
            bomb_timer=15,
            bomb_lives=3,
            bomb_syllables="",
            suggestion_up_reaction="👍",
            suggestion_down_reaction="👎",
            counter_correct_reaction="✅",
            counter_wrong_reaction="❌",
            bomb_correct_reaction="✅",
            bomb_wrong_reaction="❌",
            undercover_white_enabled=1,
            undercover_pairs="",
            counter_channel_id=None,
            counter_enabled=0,
            counter_count=0,
            counter_last_user_id=None,
            counter_mode="every",
            counter_interval=10,
            counter_exact=0,
            counter_reward_type="money",
            counter_reward_amount=0,
            counter_wrong_action="none",
            counter_wrong_mute_seconds=60,
            counter_wrong_fine=0,
            level_enabled=1,
            level_xp_per_message=5,
            level_xp_cooldown=60,
            level_money_per_level=100,
            level_xp_base=100,
            money_enabled=1,
            money_min=15,
            money_max=25,
            money_cooldown=30,
            money_long_message_enabled=0,
            server_currency_name="Argent",
            server_currency_emoji="💵",
            anti_links=0,
            anti_spam=0,
            welcome_enabled=1,
            leave_enabled=1,
            ticket_panel_title=DEFAULT_TICKET_PANEL_TITLE,
            ticket_panel_description=DEFAULT_TICKET_PANEL_DESCRIPTION,
            ticket_button_label=DEFAULT_TICKET_BUTTON,
            ticket_open_title=DEFAULT_TICKET_OPEN_TITLE,
            ticket_open_description=DEFAULT_TICKET_OPEN_DESCRIPTION,
            ticket_close_label=DEFAULT_TICKET_CLOSE_BUTTON
        )
        await interaction.response.edit_message(content="✅ Configuration réinitialisée.", embed=None, view=None)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary, emoji="↩️", row=0)
    @auto_interaction_response
    async def cancel(self, interaction, button):
        if not await self.check_user(interaction):
            return
        await interaction.response.edit_message(content="↩️ Réinitialisation annulée.", embed=None, view=None)


@bot.tree.command(name="config", description="Ouvre le centre de configuration d'AuraBot.")
@auto_interaction_response
async def config(interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
    active_config_sessions[(interaction.guild.id, interaction.user.id)] = interaction.channel_id
    await interaction.response.send_message(
        content="⚙️ **Configuration d'AuraBot**\nCe panneau est visible et utilisable **uniquement par toi**, dans **ce salon**.",
        embed=render_config_embed(interaction.guild),
        view=ConfigPanelView(interaction.guild.id, interaction.user.id),
        ephemeral=True
    )


# =========================================================
# TICKETS + CONFESSIONS ANONYMES
# =========================================================


def get_ticket_category(guild):
    cfg = get_guild_config(guild.id)
    raw = cfg.get("ticket_category_id")
    if not raw:
        return None
    try:
        return guild.get_channel(int(raw))
    except (TypeError, ValueError):
        return None


def get_ticket_staff_role(guild):
    cfg = get_guild_config(guild.id)
    raw = cfg.get("ticket_staff_role_id")
    if not raw:
        return None
    try:
        return guild.get_role(int(raw))
    except (TypeError, ValueError):
        return None


def is_ticket_staff(member):
    if is_admin(member):
        return True
    roles = get_ticket_staff_roles(member.guild)
    return any(role in member.roles for role in roles)


async def send_ticket_log(guild, title, description, color=discord.Color.blurple()):
    cfg = get_guild_config(guild.id)
    raw = cfg.get("ticket_log_channel_id")
    if not raw:
        return
    channel = guild.get_channel(int(raw))
    if channel is None:
        return
    try:
        embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now())
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


class TicketCloseView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=None)
        self.owner_id = owner_id

    @discord.ui.button(label="Fermer", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="aurabot:ticket_close")
    @auto_interaction_response
    async def close(self, interaction, button):
        cfg = get_guild_config(interaction.guild.id)
        button.label = (cfg.get("ticket_close_label") or DEFAULT_TICKET_CLOSE_BUTTON)[:80]
        owner_id = self.owner_id
        if owner_id == 0 and isinstance(interaction.channel, discord.TextChannel):
            topic = interaction.channel.topic or ""
            if "AuraBot ticket owner:" in topic:
                try:
                    owner_id = int(topic.split("AuraBot ticket owner:", 1)[1].strip())
                except ValueError:
                    owner_id = 0
        if interaction.user.id != owner_id and not is_ticket_staff(interaction.user):
            await interaction.response.send_message("❌ Tu ne peux pas fermer ce ticket.", ephemeral=True)
            return

        channel = interaction.channel
        await interaction.response.send_message("🔒 Ticket fermé. Suppression dans 5 secondes...")
        await send_ticket_log(
            interaction.guild,
            "🔒 Ticket fermé",
            f"Salon : {channel.mention}\nFermé par : {interaction.user.mention}",
            discord.Color.red()
        )
        await asyncio.sleep(5)
        try:
            await channel.delete(reason="Ticket fermé")
        except (discord.Forbidden, discord.HTTPException):
            pass


class TicketCustomReasonModal(discord.ui.Modal, title="Raison du ticket"):
    def __init__(self, user_id):
        super().__init__()
        self.user_id = user_id
        self.reason = discord.ui.TextInput(label="Explique ta raison", style=discord.TextStyle.paragraph, required=True, min_length=2, max_length=80, placeholder="Ex : J'ai besoin d'aide pour...")
        self.add_item(self.reason)

    @auto_interaction_response
    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
        await create_ticket(interaction, str(self.reason.value).strip())


class TicketReasonView(discord.ui.View):
    """Vue privée utilisée uniquement si une ancienne configuration l'appelle.
    Les nouveaux panels utilisent TicketPanelView avec un bouton par raison.
    """
    def __init__(self, guild):
        super().__init__(timeout=120)
        self.user_id = None
        reasons = get_ticket_reasons(guild)
        for index, reason in enumerate(reasons[:24]):
            button = discord.ui.Button(
                label=reason["name"][:80],
                style=discord.ButtonStyle.primary,
                emoji=reason.get("emoji") or "🎫",
                row=index // 5
            )
            async def callback(interaction, button, reason_name=reason["name"]):
                if interaction.user.id != self.user_id:
                    return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
                await create_ticket(interaction, reason_name)
                self.stop()
            button.callback = callback
            self.add_item(button)

        custom = discord.ui.Button(
            label="Raison personnalisée",
            style=discord.ButtonStyle.secondary,
            emoji="✏️",
            row=min(4, len(reasons[:24]) // 5)
        )
        async def custom_callback(interaction, button):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
            await interaction.response.send_modal(TicketCustomReasonModal(interaction.user.id))
        custom.callback = custom_callback
        self.add_item(custom)


async def create_ticket(interaction, reason):
    # Accusé de réception immédiat : création de salon = appel réseau Discord.
    # On ne dépend plus du watchdog global pour ce flux.
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    category = get_ticket_category(guild)
    if category is None:
        return await interaction.followup.send("❌ La catégorie Discord des tickets n'est pas configurée.", ephemeral=True)

    existing = discord.utils.find(lambda c: c.topic and c.topic.startswith(f"AuraBot ticket owner:{interaction.user.id}|"), guild.text_channels)
    if existing:
        return await interaction.followup.send(f"❌ Tu as déjà un ticket ouvert : {existing.mention}", ephemeral=True)

    staff_roles = get_ticket_staff_roles(guild)
    if not staff_roles:
        old_staff = get_ticket_staff_role(guild)
        if old_staff:
            staff_roles = [old_staff]

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True, read_message_history=True),
    }
    for role in staff_roles:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

    channel_name = ticket_channel_name(reason, interaction.user)
    try:
        channel = await guild.create_text_channel(
            channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"AuraBot ticket owner:{interaction.user.id}|reason:{reason}",
            reason="Création d'un ticket AuraBot"
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        return await interaction.followup.send(f"❌ Impossible de créer le ticket : `{type(e).__name__}`.", ephemeral=True)

    cfg = get_guild_config(guild.id)
    open_description = replace_config_variables(cfg.get("ticket_open_description") or DEFAULT_TICKET_OPEN_DESCRIPTION, interaction.user, guild)
    embed = discord.Embed(title=cfg.get("ticket_open_title") or DEFAULT_TICKET_OPEN_TITLE, description=open_description, color=discord.Color.blurple())
    apply_server_appearance(embed, guild, cfg)
    embed.add_field(name="📌 Raison", value=reason[:1024], inline=False)
    close_view = TicketCloseView(interaction.user.id)
    close_view.children[0].label = (cfg.get("ticket_close_label") or DEFAULT_TICKET_CLOSE_BUTTON)[:80]
    role_pings = " ".join(role.mention for role in staff_roles)
    welcome = f"Bienvenue {interaction.user.mention} sur votre ticket, il sera pris en charge le plus rapidement possible."
    try:
        await channel.send(
            content=f"{role_pings} {welcome}".strip(),
            embed=embed,
            view=close_view,
            allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False)
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"❌ Ticket créé mais message d'ouverture impossible : {type(e).__name__}: {e}")
        await interaction.followup.send(
            f"⚠️ Ton ticket a bien été créé : {channel.mention}, mais AuraBot n'a pas pu publier son message d'accueil. Vérifie les permissions du salon.",
            ephemeral=True
        )
        return

    # La création du ticket est maintenant confirmée. Une panne du log ne doit
    # jamais transformer une création réussie en faux message d'erreur.
    await interaction.followup.send(f"✅ Ton ticket a été créé : {channel.mention}", ephemeral=True)
    try:
        await send_ticket_log(
            guild,
            "🎫 Ticket ouvert",
            f"Ticket : {channel.mention}\nCréé par : {interaction.user.mention}\nRaison : **{reason}**",
            discord.Color.green()
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"⚠️ Ticket créé mais log impossible : {type(e).__name__}: {e}")


class TicketPanelView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=None)
        self.guild_id = int(guild.id)
        reasons = get_ticket_reasons(guild)[:24]

        for index, reason in enumerate(reasons):
            button = discord.ui.Button(
                label=reason["name"][:80],
                style=discord.ButtonStyle.primary,
                emoji=reason.get("emoji") or "🎫",
                custom_id=f"aurabot:ticket_reason:{self.guild_id}:{index}",
                row=index // 5
            )

            async def callback(interaction, reason_name=reason["name"]):
                await create_ticket(interaction, reason_name)

            button.callback = callback
            self.add_item(button)

        custom_row = min(4, len(reasons) // 5)
        custom = discord.ui.Button(
            label="Raison personnalisée",
            style=discord.ButtonStyle.secondary,
            emoji="✏️",
            custom_id=f"aurabot:create_ticket_custom:{self.guild_id}",
            row=custom_row
        )

        async def custom_callback(interaction):
            await interaction.response.send_modal(TicketCustomReasonModal(interaction.user.id))

        custom.callback = custom_callback
        self.add_item(custom)



class ConfessionReportView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Report", style=discord.ButtonStyle.danger, emoji="🚩", custom_id="aurabot:confession_report")
    @auto_interaction_response
    async def report(self, interaction, button):
        cfg = get_guild_config(interaction.guild.id)
        raw = cfg.get("confession_report_channel_id")
        if not raw:
            await interaction.response.send_message("❌ Le salon de report des confessions n'est pas configuré.", ephemeral=True)
            return
        try:
            report_channel = interaction.guild.get_channel(int(raw))
            if report_channel is None:
                raise ValueError
        except (TypeError, ValueError):
            await interaction.response.send_message("❌ Le salon de report configuré est introuvable.", ephemeral=True)
            return

        confession_message_id = str(interaction.message.id)
        with db_lock:
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT author_id, content, thread_id FROM confessions WHERE message_id=? AND guild_id=?",
                (confession_message_id, str(interaction.guild.id))
            )
            row = cursor.fetchone()
            conn.close()

        if not row:
            await interaction.response.send_message("❌ Cette confession n'est plus disponible.", ephemeral=True)
            return

        author_id, content, thread_id = row
        author = interaction.guild.get_member(int(author_id))
        author_text = author.mention if author else f"ID : {author_id}"
        embed = discord.Embed(
            title="🚩 REPORT DE CONFESSION",
            description=content[:4000],
            color=discord.Color.red(),
            timestamp=datetime.now()
        )
        embed.add_field(name="✍️ Auteur de la confession", value=author_text, inline=False)
        embed.add_field(name="🧵 Fil", value=f"<#{thread_id}>" if thread_id else "Non disponible", inline=False)
        embed.add_field(name="🚩 Signalé par", value=interaction.user.mention, inline=False)
        try:
            await report_channel.send(embed=embed)
            await interaction.response.send_message("✅ Le signalement a été envoyé aux administrateurs.", ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message("❌ AuraBot n'a pas la permission d'envoyer le report dans ce salon.", ephemeral=True)


class ConfessionModal(discord.ui.Modal, title="🕶️ Se confesser"):
    texte = discord.ui.TextInput(
        label="Ta confession",
        placeholder="Écris ta confession ici...",
        style=discord.TextStyle.paragraph,
        min_length=1,
        max_length=4000,
        required=True
    )

    @auto_interaction_response
    async def on_submit(self, interaction):
        cfg = get_guild_config(interaction.guild.id)
        raw = cfg.get("confession_channel_id")
        if not raw:
            return await interaction.response.send_message(
                "❌ Les confessions ne sont pas configurées.", ephemeral=True
            )
        try:
            channel = interaction.guild.get_channel(int(raw))
        except (TypeError, ValueError):
            channel = None
        if channel is None:
            return await interaction.response.send_message(
                "❌ Le salon de confession configuré est introuvable.", ephemeral=True
            )

        texte = str(self.texte.value).strip()
        if not texte:
            return await interaction.response.send_message("❌ La confession est vide.", ephemeral=True)

        embed = discord.Embed(
            title="🕶️ CONFESSION ANONYME",
            description=texte,
            color=discord.Color.dark_gray(),
            timestamp=datetime.now()
        )
        embed.set_footer(text="Auteur anonyme • Réponses anonymes dans le fil")

        try:
            await interaction.response.defer(ephemeral=True)
            message = await channel.send(embed=embed, view=ConfessionReportView())
            thread = await message.create_thread(
                name="Discussion anonyme",
                auto_archive_duration=1440,
                reason="Discussion d'une confession anonyme"
            )

            with db_lock:
                conn = db_connect()
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO confessions(message_id, guild_id, channel_id, author_id, content, thread_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(message.id), str(interaction.guild.id), str(channel.id),
                        str(interaction.user.id), texte, str(thread.id), datetime.now().isoformat()
                    )
                )
                conn.commit()
                conn.close()

            await interaction.followup.send("✅ Ta confession a été publiée anonymement.", ephemeral=True)
        except (discord.Forbidden, discord.HTTPException) as e:
            try:
                await interaction.followup.send(
                    f"❌ Impossible de publier la confession : `{type(e).__name__}`.",
                    ephemeral=True
                )
            except Exception:
                pass


class ConfessionPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Se confesser",
        style=discord.ButtonStyle.primary,
        emoji="🕶️",
        custom_id="aurabot:confession_open"
    )
    @auto_interaction_response
    async def confess(self, interaction, button):
        await interaction.response.send_modal(ConfessionModal())


def claim_member_event(guild_id, member_id, event_type, window_seconds=15):
    """Retourne True une seule fois pour un même événement pendant quelques secondes."""
    key = f"{event_type}:{int(guild_id)}:{int(member_id)}"
    now = datetime.now().timestamp()
    cutoff = now - float(window_seconds)
    try:
        with db_lock:
            conn = db_connect()
            cur = conn.cursor()
            cur.execute("DELETE FROM member_event_dedupe WHERE created_at < ?", (cutoff,))
            cur.execute("SELECT 1 FROM member_event_dedupe WHERE event_key=?", (key,))
            if cur.fetchone() is not None:
                conn.commit()
                conn.close()
                return False
            cur.execute("INSERT INTO member_event_dedupe(event_key, created_at) VALUES(?, ?)", (key, now))
            conn.commit()
            conn.close()
        return True
    except Exception as e:
        print("⚠️ Déduplication arrivée/départ impossible :", repr(e))
        return True


async def send_config_log(guild, field, title, description, color=discord.Color.blurple()):
    cfg = get_guild_config(guild.id)
    channel_id = cfg.get(field)
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return
    try:
        embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now())
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.event
async def on_guild_join(guild):
    # Recherche qui a ajouté AuraBot dans les logs d'audit. Cela nécessite
    # que le bot dispose de la permission « Voir les logs d'audit ».
    adder_id, adder_name, audit_time = await find_bot_adder(guild)
    save_bot_server(guild, adder_id, adder_name, (audit_time or datetime.now()).isoformat())
    print(f"📥 AuraBot ajouté à {guild.name} ({guild.id}) — ajouté par {adder_name or 'inconnu'}")


@bot.event
async def on_member_join(member):
    if not claim_member_event(member.guild.id, member.id, "join"):
        return
    cfg = get_guild_config(member.guild.id)

    role_id = cfg.get("autorole_id")
    if role_id:
        role = member.guild.get_role(int(role_id))
        if role:
            try:
                await member.add_roles(role, reason="AuraBot autorole")
            except (discord.Forbidden, discord.HTTPException):
                pass

    channel_id = cfg.get("welcome_channel_id")
    if cfg.get("welcome_enabled", 1) and channel_id:
        channel = member.guild.get_channel(int(channel_id))
        if channel:
            text = replace_config_variables(cfg.get("welcome_message") or DEFAULT_WELCOME, member, member.guild)
            try:
                await channel.send(text)
            except (discord.Forbidden, discord.HTTPException):
                pass

    # Si le salon de logs est le même que le salon d'arrivée, ne pas envoyer un 2e message.
    if str(cfg.get("log_channel_id") or "") != str(channel_id or ""):
        await send_config_log(member.guild, "log_channel_id", "👋 Nouveau membre", f"{member.mention} a rejoint le serveur.")


@bot.event
async def on_member_remove(member):
    if not claim_member_event(member.guild.id, member.id, "leave"):
        return
    cfg = get_guild_config(member.guild.id)
    channel_id = cfg.get("leave_channel_id")
    if cfg.get("leave_enabled", 1) and channel_id:
        channel = member.guild.get_channel(int(channel_id))
        if channel:
            # Le nombre de membres est lu après le départ par Discord.
            text = replace_config_variables(cfg.get("leave_message") or DEFAULT_LEAVE, member, member.guild)
            try:
                await channel.send(text)
            except (discord.Forbidden, discord.HTTPException):
                pass
    # Si le salon de logs est le même que le salon de départ, ne pas envoyer un 2e message.
    if str(cfg.get("log_channel_id") or "") != str(channel_id or ""):
        await send_config_log(member.guild, "log_channel_id", "👋 Départ", f"**{member.display_name}** a quitté le serveur.")


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot or member.guild is None:
        return
    cfg = get_guild_config(member.guild.id)

    # Création automatique depuis le salon déclencheur.
    trigger_id = str(cfg.get("temp_voice_create_channel_id") or "")
    if cfg.get("temp_voice_enabled") and after.channel and str(after.channel.id) == trigger_id:
        category = None
        if cfg.get("temp_voice_category_id"):
            try: category = member.guild.get_channel(int(cfg["temp_voice_category_id"]))
            except (TypeError, ValueError): category = None
        if not isinstance(category, discord.CategoryChannel):
            category = after.channel.category
        overwrites = {
            member.guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True),
            member: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, move_members=True, mute_members=True, deafen_members=True),
            member.guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True, move_members=True, mute_members=True, deafen_members=True),
        }
        try:
            ch = await member.guild.create_voice_channel(
                name=f"🔊 {member.display_name}",
                category=category,
                overwrites=overwrites,
                reason="Création d'un salon vocal temporaire AuraBot"
            )
            temp_voice_db_add(ch.id, member.guild.id, member.id)
            await member.move_to(ch, reason="Déplacement vers le vocal temporaire")
            try:
                await member.send(
                    f"🔊 **Ton salon vocal a été créé !**\n\n"
                    f"Salon : **{ch.name}** sur **{member.guild.name}**\n"
                    "Tu es le propriétaire de ce salon. Le panneau ci-dessous te permet de le gérer comme un administrateur, uniquement pour ce salon.",
                    view=TempVoicePanel(ch.id, member.id)
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        except (discord.Forbidden, discord.HTTPException):
            pass

    # Suppression automatique quand un vocal temporaire devient vide.
    if before.channel:
        row = temp_voice_db_get(before.channel.id)
        if row and len(before.channel.members) == 0:
            temp_voice_db_delete(before.channel.id)
            try:
                await before.channel.delete(reason="Salon vocal temporaire vide")
            except (discord.Forbidden, discord.HTTPException):
                pass


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Les réponses dans les fils de confessions sont relayées par AuraBot
    # afin que l'auteur Discord ne soit jamais visible publiquement.
    if message.guild and isinstance(message.channel, discord.Thread):
        parent_id = message.channel.parent_id
        with db_lock:
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM confessions WHERE thread_id=? AND guild_id=?",
                (str(message.channel.id), str(message.guild.id))
            )
            is_confession_thread = cursor.fetchone() is not None
            conn.close()
        if is_confession_thread:
            try:
                content = message.content.strip()
                if content:
                    await message.delete()
                    await message.channel.send(
                        "🕶️ **Anonyme :** " + content[:1900]
                    )
            except (discord.Forbidden, discord.HTTPException):
                pass
            return

    if message.guild:
        try:
            # Recharge la configuration depuis SQLite à chaque message.
            # Le compteur continue donc à fonctionner après un redémarrage du bot.
            cfg = get_guild_config(message.guild.id)
        except Exception as e:
            print(f"⚠️ Erreur lecture configuration compteur : {e}")
            return

        # Commandes personnalisées façon DraftBot : !nom [arguments]
        # Elles sont configurées depuis /config > Commandes personnalisées.
        try:
            if message.content.startswith("!") and len(message.content) > 1:
                parts = message.content[1:].split(maxsplit=1)
                custom_name = parts[0].lower()
                args_text = parts[1] if len(parts) > 1 else ""
                custom = get_custom_command(message.guild.id, custom_name)
                if custom:
                    response = render_custom_command_response(custom["response"], message, args_text)
                    await message.channel.send(response, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except Exception as e:
            print("⚠️ Commande personnalisée :", e)

        # Niveaux / XP serveur : configurable comme un système de niveaux classique.
        try:
            gained_levels, new_level, level_rewards = add_server_message_xp(message.guild.id, message.author.id, cfg)
            if gained_levels > 0:
                reward_text = " • ".join(level_rewards) if level_rewards else "Aucune récompense"
                await message.channel.send(f"🎉 {message.author.mention} passe **niveau {new_level}** ! +{gained_levels} niveau.\n🎁 Récompense : **{reward_text}**", delete_after=8)
        except Exception as e:
            print("⚠️ XP serveur :", e)

        # Économie : vrai gain d'argent par message, indépendant de l'XP.
        try:
            gained_money = add_server_message_money(message.guild.id, message.author.id, cfg, len(message.content))
            # Aucun emoji de réaction pour les gains d'argent.
        except Exception as e:
            print("⚠️ Argent serveur :", e)

        # Compteur : seul le salon configuré est concerné.
        counter_channel = cfg.get("counter_channel_id")
        if cfg.get("counter_enabled") and counter_channel and str(message.channel.id) == str(counter_channel):
            current = int(cfg.get("counter_count", 0) or 0)
            expected = current + 1
            content = message.content.strip()
            try:
                value = int(content)
            except ValueError:
                value = None

            last_user_id = str(cfg.get("counter_last_user_id") or "")

            # Une même personne ne peut pas poser deux nombres d'affilée.
            # Cette erreur est traitée comme un mauvais comptage afin d'afficher
            # clairement la raison ET la punition configurée.
            same_person = value == expected and last_user_id == str(message.author.id)

            if value == expected and not same_person:
                update_guild_config(
                    message.guild.id,
                    counter_count=expected,
                    counter_last_user_id=str(message.author.id)
                )
                try:
                    try:
                        if bot.user:
                            await message.remove_reaction("💰", bot.user)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                    await message.add_reaction(str(cfg.get("counter_correct_reaction") or "✅"))
                except discord.Forbidden:
                    try:
                        await message.channel.send("⚠️ AuraBot n'a pas la permission d'ajouter des réactions.", delete_after=5)
                    except Exception:
                        pass
                except discord.HTTPException:
                    pass

                mode = str(cfg.get("counter_mode") or "every").lower()
                interval = max(1, int(cfg.get("counter_interval", 1) or 1))
                exact = int(cfg.get("counter_exact", 0) or 0)
                amount = max(0, int(cfg.get("counter_reward_amount", 0) or 0))
                rtype = str(cfg.get("counter_reward_type") or "money").lower()
                reward = (
                    amount > 0 and (
                        mode == "every"
                        or (mode == "interval" and expected % interval == 0)
                        or (mode == "exact" and expected == exact)
                    )
                )
                if reward:
                    user = get_user(message.author.id)
                    if user:
                        if rtype == "aura":
                            user["aura"] += amount
                            save_user(user)
                            label = "Aura"
                        else:
                            member_data = get_server_member(message.guild.id, message.author.id)
                            member_data["money"] += amount
                            save_server_member(member_data)
                            label = server_currency(cfg)
                        try:
                            await message.channel.send(
                                f"🎁 {message.author.mention} gagne **+{amount} {label}** au palier {expected} !",
                                delete_after=6
                            )
                        except (discord.Forbidden, discord.HTTPException):
                            pass
            else:
                # Mauvais nombre ou message non numérique : réaction personnalisée + suppression + sanction.
                try:
                    try:
                        if bot.user:
                            await message.remove_reaction("💰", bot.user)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                    await message.add_reaction(str(cfg.get("counter_wrong_reaction") or "❌"))
                except (discord.Forbidden, discord.HTTPException):
                    pass
                deleted = True
                try:
                    await message.delete(reason="Mauvais comptage")
                except discord.Forbidden:
                    deleted = False
                except discord.HTTPException:
                    deleted = False

                action = str(cfg.get("counter_wrong_action") or "none").lower()
                fine = max(0, int(cfg.get("counter_wrong_fine", 0) or 0))
                mute_seconds = max(1, int(cfg.get("counter_wrong_mute_seconds", 60) or 60))

                if action in {"fine", "both"} and fine:
                    member_data = get_server_member(message.guild.id, message.author.id)
                    member_data["money"] = max(0, int(member_data.get("money", 0)) - fine)
                    save_server_member(member_data)

                muted = False
                if action in {"mute", "both"}:
                    try:
                        await message.author.timeout(timedelta(seconds=mute_seconds), reason="Mauvais comptage")
                        muted = True
                    except (discord.Forbidden, discord.HTTPException):
                        pass

                # Message d'explication détaillé : mention, erreur, nombre attendu
                # et punition réellement appliquée.
                if same_person:
                    reason = (
                        f"tu as déjà joué le nombre précédent, donc tu ne peux pas jouer **{expected}** deux fois d'affilée. "
                        f"Un autre membre doit jouer **{expected}**."
                    )
                elif value is None:
                    reason = (
                        f"**{message.content.strip()[:80]}** n'est pas un nombre. "
                        f"Il fallait écrire **{expected}**."
                    )
                else:
                    reason = (
                        f"tu as écrit **{value}**, alors que le nombre attendu était **{expected}**."
                    )

                parts = [f"❌ {message.author.mention}, {reason}"]
                if deleted:
                    parts.append("🗑️ Message supprimé.")
                else:
                    parts.append("⚠️ Je n'ai pas pu supprimer le message : vérifie la permission **Gérer les messages**.")

                if action == "none":
                    parts.append("Punition : **aucune**.")
                else:
                    punishment_parts = []
                    if action in {"fine", "both"} and fine:
                        punishment_parts.append(f"amende de **-{fine} {server_currency(cfg)}**")
                    if action in {"mute", "both"}:
                        punishment_parts.append(
                            f"mute de **{mute_seconds}s**" if muted
                            else "mute non appliqué (permission manquante)"
                        )
                    if punishment_parts:
                        parts.append("Punition : " + " + ".join(punishment_parts) + ".")
                    else:
                        parts.append("Punition : **aucune configurée**.")

                try:
                    await message.channel.send("\n".join(parts), delete_after=9)
                except (discord.Forbidden, discord.HTTPException):
                    pass
            return

        # BombParty : seul le joueur dont c'est le tour est pris en compte.
        bomb = active_bomb_games.get(message.channel.id)
        if bomb and message.author.id == bomb.get("waiting_for") and not bomb.get("accepted"):
            raw_word = message.content.strip().lower()
            word = normalize_bomb_word(raw_word)
            syllable = str(bomb.get("syllable") or "").lower()
            basic_valid = (
                bool(word)
                and syllable in word
                and word not in bomb.get("used", set())
            )
            valid = basic_valid and await is_real_french_word(word)
            try:
                if valid:
                    bomb["used"].add(word)
                    bomb["accepted"] = True
                    await message.add_reaction(str(cfg.get("bomb_correct_reaction") or "✅"))
                else:
                    await message.add_reaction(str(cfg.get("bomb_wrong_reaction") or "❌"))
            except (discord.Forbidden, discord.HTTPException):
                pass
            return

        # Anti-liens simple, configurable.
        if cfg.get("anti_links") and ("http://" in message.content.lower() or "https://" in message.content.lower() or "discord.gg/" in message.content.lower()):
            if not is_admin(message.author):
                try:
                    await message.delete()
                    await message.channel.send(f"🚫 {message.author.mention}, les liens sont désactivés dans ce serveur.", delete_after=5)
                    await send_config_log(message.guild, "modlog_channel_id", "🔗 Lien supprimé", f"Message de {message.author.mention} supprimé dans {message.channel.mention}.", discord.Color.red())
                    return
                except (discord.Forbidden, discord.HTTPException):
                    pass

        # Anti-spam : 5 messages en 7 secondes.
        if cfg.get("anti_spam") and not is_admin(message.author):
            now = asyncio.get_running_loop().time()
            key = (message.guild.id, message.author.id)
            timestamps = [t for t in spam_tracker.get(key, []) if now - t < 7]
            timestamps.append(now)
            spam_tracker[key] = timestamps
            if len(timestamps) >= 5:
                spam_tracker[key] = []
                try:
                    await message.delete()
                    await message.channel.send(f"⚠️ {message.author.mention}, ralentis un peu 😅", delete_after=5)
                    await send_config_log(message.guild, "modlog_channel_id", "🚨 Anti-spam", f"Spam détecté de {message.author.mention} dans {message.channel.mention}.", discord.Color.orange())
                    return
                except (discord.Forbidden, discord.HTTPException):
                    pass

    await bot.process_commands(message)


# =========================================================
# SERVEURS DU BOT — propriétaire uniquement
# =========================================================

def save_bot_server(guild, added_by_id=None, added_by_name=None, joined_at=None):
    with db_lock:
        conn = db_connect()
        conn.execute(
            "INSERT OR REPLACE INTO bot_servers (guild_id, guild_name, added_by_id, added_by_name, joined_at) VALUES (?, ?, ?, ?, ?)",
            (str(guild.id), guild.name, str(added_by_id) if added_by_id else None, added_by_name, joined_at or datetime.now().isoformat())
        )
        conn.commit()
        conn.close()


def get_saved_bot_servers():
    with db_lock:
        conn = db_connect()
        rows = conn.execute(
            "SELECT guild_id, guild_name, added_by_id, added_by_name, joined_at FROM bot_servers ORDER BY joined_at DESC"
        ).fetchall()
        conn.close()
    return rows


async def find_bot_adder(guild):
    """Essaie de retrouver l'utilisateur qui a ajouté le bot via les logs d'audit."""
    try:
        async for entry in guild.audit_logs(limit=20, action=discord.AuditLogAction.bot_add):
            target = getattr(entry, "target", None)
            if target and getattr(target, "id", None) == bot.user.id:
                user = entry.user
                return user.id, str(user), entry.created_at
    except (discord.Forbidden, discord.HTTPException):
        pass
    except Exception:
        pass
    return None, None, None


async def make_server_invite(guild):
    # On réutilise d'abord un lien vanity s'il existe.
    try:
        if guild.vanity_url:
            return guild.vanity_url
    except Exception:
        pass

    # Sinon, cherche un salon où AuraBot peut créer une invitation permanente.
    for channel in guild.text_channels:
        perms = channel.permissions_for(guild.me)
        if perms.view_channel and perms.create_instant_invite:
            try:
                invite = await channel.create_invite(max_age=0, max_uses=0, unique=False, reason="Lien d'accès affiché au propriétaire d'AuraBot")
                return invite.url
            except (discord.Forbidden, discord.HTTPException):
                continue
    return None


class ServeurRemoveConfirmView(discord.ui.View):
    def __init__(self, owner_id, guild_id):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.guild_id = guild_id

    @discord.ui.button(label="Confirmer le retrait", style=discord.ButtonStyle.danger, emoji="🗑️")
    @auto_interaction_response
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("❌ Ce bouton est réservé au propriétaire du bot.", ephemeral=True)

        guild = bot.get_guild(self.guild_id)
        if guild is None:
            return await interaction.response.edit_message(content="❌ AuraBot n'est plus dans ce serveur.", embed=None, view=None)

        guild_name = guild.name
        try:
            await guild.leave()
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ Discord a refusé le retrait du serveur.", ephemeral=True
            )
        except discord.HTTPException as e:
            return await interaction.response.send_message(
                f"❌ Impossible de quitter le serveur : `{e}`", ephemeral=True
            )

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"✅ AuraBot a quitté **{guild_name}**.",
            embed=None,
            view=self
        )

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    @auto_interaction_response
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("❌ Ce bouton est réservé au propriétaire du bot.", ephemeral=True)
        await interaction.response.edit_message(content="❎ Retrait annulé.", embed=None, view=None)


class ServeurRemoveSelect(discord.ui.Select):
    def __init__(self, owner_id, guilds):
        options = [
            discord.SelectOption(
                label=guild.name[:100],
                value=str(guild.id),
                description=f"ID : {guild.id}"[:100]
            )
            for guild in guilds[:25]
        ]
        super().__init__(placeholder="Choisis le serveur à retirer...", min_values=1, max_values=1, options=options)
        self.owner_id = owner_id

    @auto_interaction_response
    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("❌ Ce menu est réservé au propriétaire du bot.", ephemeral=True)

        guild_id = int(self.values[0])
        guild = bot.get_guild(guild_id)
        if guild is None:
            return await interaction.response.send_message("❌ Ce serveur n'est plus accessible par AuraBot.", ephemeral=True)

        embed = discord.Embed(
            title="⚠️ Retirer AuraBot",
            description=(
                f"Tu es sur le point de faire quitter AuraBot du serveur **{guild.name}**.\n\n"
                "Cette action est immédiate et AuraBot ne sera plus présent sur ce serveur."
            ),
            color=discord.Color.red()
        )
        await interaction.response.send_message(
            embed=embed,
            view=ServeurRemoveConfirmView(self.owner_id, guild_id),
            ephemeral=True
        )


class ServeurRemoveView(discord.ui.View):
    def __init__(self, owner_id, guilds):
        super().__init__(timeout=180)
        self.add_item(ServeurRemoveSelect(owner_id, guilds))


@bot.tree.command(name="serveur", description="Affiche les serveurs ayant ajouté AuraBot (propriétaire uniquement).")
@auto_interaction_response
async def serveur(interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("❌ Cette commande est réservée au propriétaire du bot.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    saved = {row[0]: row for row in get_saved_bot_servers()}
    current_ids = {str(g.id) for g in bot.guilds}

    # Complète les serveurs déjà présents mais ajoutés avant l'activation du suivi.
    for guild in bot.guilds:
        gid = str(guild.id)
        row = saved.get(gid)
        if row is None or not row[2]:
            adder_id, adder_name, audit_time = await find_bot_adder(guild)
            if row is None:
                save_bot_server(guild, adder_id, adder_name, (audit_time or datetime.now()).isoformat())
            elif adder_id:
                save_bot_server(guild, adder_id, adder_name, (audit_time or datetime.now()).isoformat())

    rows = [r for r in get_saved_bot_servers() if r[0] in current_ids]
    if not rows:
        return await interaction.followup.send("📭 AuraBot n'est actuellement dans aucun serveur.", ephemeral=True)

    # Discord limite un embed à 25 fields ; on utilise plusieurs embeds si nécessaire.
    embeds = []
    for offset in range(0, len(rows), 10):
        embed = discord.Embed(
            title="🛰️ Serveurs d'AuraBot" if offset == 0 else f"🛰️ Serveurs d'AuraBot — suite {offset // 10 + 1}",
            description=f"**{len(rows)} serveur(s)** actuellement connecté(s).",
            color=discord.Color.blurple()
        )
        for guild_id, guild_name, adder_id, adder_name, joined_at in rows[offset:offset + 10]:
            guild = bot.get_guild(int(guild_id))
            if guild is None:
                continue
            try:
                dt = datetime.fromisoformat(joined_at) if joined_at else None
                when = dt.strftime("%d/%m/%Y à %H:%M") if dt else "Inconnue"
            except Exception:
                when = str(joined_at or "Inconnue")
            invite = await make_server_invite(guild)
            who = f"{adder_name} (`{adder_id}`)" if adder_id else "Inconnu (logs d'audit indisponibles)"
            link = f"[🔗 Rejoindre le serveur]({invite})" if invite else "🔗 Invitation indisponible"
            embed.add_field(
                name=f"🏠 {guild_name}",
                value=f"**Qui l'a ajouté :** {who}\n**Quand :** {when}\n{link}",
                inline=False
            )
        embeds.append(embed)

    # Le menu de retrait est réservé au propriétaire et permet de sélectionner un serveur.
    remove_view = ServeurRemoveView(OWNER_ID, [bot.get_guild(int(r[0])) for r in rows if bot.get_guild(int(r[0])) is not None])
    await interaction.followup.send(embeds=embeds, view=remove_view, ephemeral=True)

# =========================================================
# PROGRESSION V9 — SÉRIES / SUCCÈS / TITRES / QUÊTES
# =========================================================

LEAGUES = [
    (0, "Bronze", "🟫"),
    (1100, "Argent", "⬜"),
    (1200, "Or", "🟨"),
    (1350, "Platine", "🔷"),
    (1500, "Diamant", "💎"),
    (1700, "Aura", "🟣"),
]

ACHIEVEMENTS = {
    "first_win": ("Premier sang", "Gagne ton premier duel.", "⚔️", "Duelliste"),
    "wins_10": ("Combattant", "Gagne 10 duels.", "🏹", "Combattant"),
    "wins_50": ("Vétéran", "Gagne 50 duels.", "🏆", "Vétéran"),
    "streak_5": ("En feu", "Atteins une série de 5 victoires.", "🔥", "En feu"),
    "streak_10": ("Invincible", "Atteins une série de 10 victoires.", "👑", "Invincible"),
    "elo_1200": ("Rival", "Atteins 1200 ELO.", "⚡", "Rival"),
    "elo_1500": ("Champion", "Atteins 1500 ELO.", "💎", "Champion"),
    "elo_1700": ("Maître Aura", "Atteins 1700 ELO.", "🌌", "Maître Aura"),
}


def get_progress(user_id):
    with db_lock:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT * FROM player_progress WHERE user_id=?", (str(user_id),))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO player_progress(user_id) VALUES(?)", (str(user_id),))
            conn.commit()
            cur.execute("SELECT * FROM player_progress WHERE user_id=?", (str(user_id),))
            row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        conn.close()
        return dict(zip(cols, row))


def save_progress(data):
    with db_lock:
        conn = db_connect()
        conn.execute("""UPDATE player_progress SET duel_streak=?, best_duel_streak=?, elo_max=?, selected_title=? WHERE user_id=?""", (
            int(data.get("duel_streak",0)), int(data.get("best_duel_streak",0)),
            int(data.get("elo_max",1000)), data.get("selected_title"), str(data["user_id"])
        ))
        conn.commit(); conn.close()


def league_for_elo(elo):
    current = LEAGUES[0]
    for league in LEAGUES:
        if elo >= league[0]:
            current = league
    return current


def unlock_achievement(user_id, key):
    if key not in ACHIEVEMENTS:
        return False
    with db_lock:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM achievements WHERE user_id=? AND achievement=?", (str(user_id), key))
        if cur.fetchone():
            conn.close(); return False
        cur.execute("INSERT INTO achievements VALUES(?,?,?)", (str(user_id), key, datetime.now().isoformat()))
        conn.commit(); conn.close()
    return True


def get_achievements(user_id):
    with db_lock:
        conn=db_connect(); cur=conn.cursor()
        cur.execute("SELECT achievement FROM achievements WHERE user_id=? ORDER BY unlocked_at", (str(user_id),))
        rows=[r[0] for r in cur.fetchall()]; conn.close(); return rows


def update_duel_progress(user_id, won, elo):
    data=get_progress(user_id)
    if won:
        data["duel_streak"] += 1
        data["best_duel_streak"] = max(data["best_duel_streak"], data["duel_streak"])
    else:
        data["duel_streak"] = 0
    data["elo_max"] = max(data["elo_max"], elo)
    save_progress(data)
    user=get_user(user_id)
    wins=int(user.get("wins",0)) if user else 0
    if wins >= 1: unlock_achievement(user_id,"first_win")
    if wins >= 10: unlock_achievement(user_id,"wins_10")
    if wins >= 50: unlock_achievement(user_id,"wins_50")
    if data["duel_streak"] >= 5: unlock_achievement(user_id,"streak_5")
    if data["duel_streak"] >= 10: unlock_achievement(user_id,"streak_10")
    if elo >= 1200: unlock_achievement(user_id,"elo_1200")
    if elo >= 1500: unlock_achievement(user_id,"elo_1500")
    if elo >= 1700: unlock_achievement(user_id,"elo_1700")


def quest_defs():
    return {
        "win1": ("Gagner 1 duel", 1, 15, 1),
        "win3": ("Gagner 3 duels", 3, 35, 2),
        "play5": ("Jouer 5 duels", 5, 25, 1),
        "streak2": ("Faire une série de 2 victoires", 2, 30, 1),
    }


def quest_today(user_id):
    day=datetime.now().date().isoformat()
    defs=quest_defs()
    with db_lock:
        conn=db_connect(); cur=conn.cursor()
        for key in defs:
            cur.execute("INSERT OR IGNORE INTO quest_progress(user_id,quest_date,quest_key) VALUES(?,?,?)", (str(user_id),day,key))
        conn.commit()
        cur.execute("SELECT quest_key,progress,claimed FROM quest_progress WHERE user_id=? AND quest_date=?", (str(user_id),day))
        rows={r[0]: (r[1],r[2]) for r in cur.fetchall()}; conn.close()
    return rows


def progress_quests(user_id, duel_played=False, duel_won=False, streak=0):
    day=datetime.now().date().isoformat(); rows=quest_today(user_id)
    with db_lock:
        conn=db_connect(); cur=conn.cursor()
        if duel_played:
            cur.execute("UPDATE quest_progress SET progress=MIN(progress+1,5) WHERE user_id=? AND quest_date=? AND quest_key='play5'",(str(user_id),day))
        if duel_won:
            cur.execute("UPDATE quest_progress SET progress=MIN(progress+1,1) WHERE user_id=? AND quest_date=? AND quest_key='win1'",(str(user_id),day))
            cur.execute("UPDATE quest_progress SET progress=MIN(progress+1,3) WHERE user_id=? AND quest_date=? AND quest_key='win3'",(str(user_id),day))
        if streak >= 2:
            cur.execute("UPDATE quest_progress SET progress=MIN(2,?) WHERE user_id=? AND quest_date=? AND quest_key='streak2'",(streak,str(user_id),day))
        conn.commit(); conn.close()


def title_available(user_id, title):
    return any(ACHIEVEMENTS[k][3] == title for k in get_achievements(user_id))


@bot.tree.command(name="addrole", description="Donne un rôle à tous les humains, bots ou à tout le serveur.")
@app_commands.describe(role="Rôle à donner", cible="Qui doit recevoir le rôle")
@app_commands.choices(cible=[
    app_commands.Choice(name="👤 Humains uniquement", value="humans"),
    app_commands.Choice(name="🤖 Bots uniquement", value="bots"),
    app_commands.Choice(name="🌐 Tout le monde", value="all"),
])
@auto_interaction_response
async def addrole(interaction, role: discord.Role, cible: app_commands.Choice[str]):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
    me = interaction.guild.me
    if role.is_default() or role.managed:
        return await interaction.response.send_message("❌ Ce rôle ne peut pas être attribué par le bot.", ephemeral=True)
    if me is None or role >= me.top_role:
        return await interaction.response.send_message("❌ Le rôle doit être placé sous le rôle le plus haut d'AuraBot.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    members = interaction.guild.members
    if cible.value == "humans":
        targets = [m for m in members if not m.bot]
    elif cible.value == "bots":
        targets = [m for m in members if m.bot]
    else:
        targets = members
    added = 0
    skipped = 0
    for member in targets:
        if role in member.roles:
            skipped += 1
            continue
        try:
            await member.add_roles(role, reason=f"/addrole par {interaction.user}")
            added += 1
        except (discord.Forbidden, discord.HTTPException):
            skipped += 1
    await interaction.followup.send(
        f"✅ Rôle {role.mention} ajouté à **{added}** membre(s).\n"
        f"⏭️ Déjà présent ou impossible : **{skipped}**.",
        ephemeral=True
    )


@bot.tree.command(name="profil", description="Affiche ton profil AuraBot.")
@app_commands.describe(membre="Membre à afficher (facultatif)")
@auto_interaction_response
async def profil(interaction, membre: discord.Member=None):
    target=membre or interaction.user
    user=get_user(target.id)
    prog=get_progress(target.id)
    league=league_for_elo(user["elo"])
    title=prog.get("selected_title")
    embed=discord.Embed(title=f"👤 PROFIL — {target.display_name}", color=discord.Color.blurple())
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="🏆 Classement", value=f"{league[2]} **{league[1]}**\n{user['elo']} ELO", inline=True)
    embed.add_field(name="⚔️ Duels", value=f"**{user['wins']}** victoires • **{user['losses']}** défaites\nWinrate : **{(user['wins']/(user['wins']+user['losses'])*100 if user['wins']+user['losses'] else 0):.1f}%**", inline=True)
    embed.add_field(name="🔥 Série", value=f"Actuelle : **{prog['duel_streak']}**\nRecord : **{prog['best_duel_streak']}**", inline=True)
    embed.add_field(name="📈 Progression", value=f"ELO max : **{prog['elo_max']}**\nNiveau : **{user['level']}**\nXP : **{user['xp']}**", inline=True)
    embed.add_field(name="✨ Ressources", value=f"Aura : **{user['aura']}**\n🪙 Jetons : **{user['coins']}**\n⭐ Réputation : **{user.get('reputation',0)}**", inline=True)
    embed.add_field(name="⚔️ Améliorations de duel", value=f"⚔️ Épée : **niveau {int(user.get('sword', 1))}**\n🛡️ Armure : **niveau {int(user.get('armor', 1))}**", inline=True)
    embed.add_field(name="🏅 Titre", value=f"**{title or 'Aucun titre'}**", inline=True)
    mb_server, mb_global=await get_memberbattle_wins(interaction.guild.id,target.id)
    embed.add_field(name="🎮 Member Battles", value=f"Serveur : **{mb_server}**\nTous serveurs : **{mb_global}**", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="top_duels", description="Affiche le classement des duels du serveur.")
@app_commands.describe(categorie="Catégorie du classement")
@app_commands.choices(categorie=[
    app_commands.Choice(name="🏆 ELO",value="elo"),
    app_commands.Choice(name="⚔️ Victoires",value="wins"),
    app_commands.Choice(name="✨ Aura",value="aura"),
    app_commands.Choice(name="🔥 Série",value="streak"),
    app_commands.Choice(name="🎮 Member Battles",value="memberbattle"),
])
@auto_interaction_response
async def top_duels(interaction, categorie: app_commands.Choice[str]):
    rows=[]
    members={m.id:m for m in interaction.guild.members if not m.bot}
    for uid, member in members.items():
        user=get_user(uid)
        if not user: continue
        prog=get_progress(uid)
        if categorie.value=="memberbattle":
            server,_=await get_memberbattle_wins(interaction.guild.id,uid); value=server
        elif categorie.value=="streak": value=prog["duel_streak"]
        else: value=user.get(categorie.value,0)
        rows.append((value,member))
    rows.sort(key=lambda x:x[0],reverse=True)
    lines=[]
    for i,(value,member) in enumerate(rows[:10],1):
        medal={1:'🥇',2:'🥈',3:'🥉'}.get(i,f'**{i}.**')
        lines.append(f"{medal} {member.mention} — **{value}**")
    labels={"elo":"🏆 ELO","wins":"⚔️ Victoires","aura":"✨ Aura","streak":"🔥 Série","memberbattle":"🎮 Member Battles"}
    embed=discord.Embed(title=f"🏆 CLASSEMENT — {labels[categorie.value]}",description='\n'.join(lines) or 'Aucun joueur.',color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="succes", description="Affiche tes succès débloqués.")
@auto_interaction_response
async def succes(interaction):
    unlocked=get_achievements(interaction.user.id)
    lines=[]
    for key,(name,desc,emoji,title) in ACHIEVEMENTS.items():
        if key in unlocked: lines.append(f"{emoji} **{name}** — {desc} → titre **{title}**")
        else: lines.append(f"🔒 **{name}** — {desc}")
    await interaction.response.send_message(embed=discord.Embed(title="🏅 SUCCÈS",description='\n'.join(lines),color=discord.Color.blurple()),ephemeral=True)


@bot.tree.command(name="titre", description="Choisis un titre que tu as débloqué.")
@app_commands.describe(nom="Titre à équiper")
@auto_interaction_response
async def titre(interaction, nom: str):
    if not title_available(interaction.user.id,nom):
        await interaction.response.send_message("❌ Ce titre n'est pas encore débloqué. Consulte `/succes`.",ephemeral=True); return
    prog=get_progress(interaction.user.id); prog["selected_title"]=nom; save_progress(prog)
    await interaction.response.send_message(f"✅ Titre équipé : **{nom}**.",ephemeral=True)


@bot.tree.command(name="quetes", description="Affiche tes quêtes quotidiennes de duels.")
@auto_interaction_response
async def quetes(interaction):
    if not await require_tutorial(interaction): return
    rows=quest_today(interaction.user.id); defs=quest_defs(); lines=[]
    for key,(name,target,reward,aura) in defs.items():
        progress,claimed=rows.get(key,(0,0)); state='✅ Réclamée' if claimed else f'**{min(progress,target)}/{target}**'
        lines.append(f"🎯 **{name}** — {state}\n   🪙 +{reward} jetons • ✨ +{aura} Aura")
    class QuestView(discord.ui.View):
        def __init__(self): super().__init__(timeout=120)
        @discord.ui.button(label="Réclamer les quêtes terminées",style=discord.ButtonStyle.success,emoji="🎁")
        async def claim(self,inter,button):
            if inter.user.id!=interaction.user.id: return await inter.response.send_message("❌ Ce bouton ne t'appartient pas.",ephemeral=True)
            rows2=quest_today(inter.user.id); total_coins=total_aura=0; claimed=[]
            with db_lock:
                conn=db_connect(); cur=conn.cursor()
                for key,(name,target,reward,aura) in defs.items():
                    prog,done=rows2.get(key,(0,0))
                    if prog>=target and not done:
                        cur.execute("UPDATE quest_progress SET claimed=1 WHERE user_id=? AND quest_date=? AND quest_key=?",(str(inter.user.id),datetime.now().date().isoformat(),key)); total_coins+=reward; total_aura+=aura; claimed.append(key)
                conn.commit(); conn.close()
            if not claimed: return await inter.response.send_message("❌ Aucune quête terminée à réclamer.",ephemeral=True)
            u=get_user(inter.user.id); u['coins']+=total_coins; u['aura']+=total_aura; save_user(u)
            await inter.response.edit_message(content=f"🎁 Récompenses récupérées : **+{total_coins} 🪙** et **+{total_aura} Aura**.",embed=None,view=None)
    await interaction.response.send_message(embed=discord.Embed(title="🎯 QUÊTES QUOTIDIENNES",description='\n\n'.join(lines),color=discord.Color.blurple()),view=QuestView(),ephemeral=True)


# =========================================================
# SUGGESTIONS
# =========================================================

@bot.tree.command(name="suggestion", description="Envoie une suggestion dans le salon configuré.")
@app_commands.describe(texte="Ta suggestion")
@auto_interaction_response
async def suggestion(interaction, texte: str):
    cfg = get_guild_config(interaction.guild.id)
    channel_id = cfg.get("suggestions_channel_id")
    if not channel_id:
        return await interaction.response.send_message(
            "❌ Le salon des suggestions n'est pas configuré. Un administrateur doit le définir dans `/config` → Suggestions.",
            ephemeral=True
        )
    channel = interaction.guild.get_channel(int(channel_id))
    if channel is None:
        return await interaction.response.send_message("❌ Le salon des suggestions configuré est introuvable.", ephemeral=True)
    texte = texte.strip()
    if not texte:
        return await interaction.response.send_message("❌ Ta suggestion ne peut pas être vide.", ephemeral=True)
    if len(texte) > 1900:
        return await interaction.response.send_message("❌ Ta suggestion est trop longue (1900 caractères maximum).", ephemeral=True)

    embed = discord.Embed(
        title="💡 Nouvelle suggestion",
        description=texte,
        color=discord.Color.blurple()
    )
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text=f"Suggestion • {interaction.guild.name}")
    try:
        msg = await channel.send(embed=embed)
        for reaction_key, fallback in (("suggestion_up_reaction", "👍"), ("suggestion_down_reaction", "👎")):
            try:
                await msg.add_reaction(str(cfg.get(reaction_key) or fallback))
            except (discord.Forbidden, discord.HTTPException):
                pass
    except (discord.Forbidden, discord.HTTPException):
        return await interaction.response.send_message(
            "❌ Je ne peux pas publier dans le salon des suggestions. Vérifie mes permissions.",
            ephemeral=True
        )
    await interaction.response.send_message("✅ Ta suggestion a été envoyée.", ephemeral=True)


# =========================================================
# NIVEAU / ARGENT / TOPS
# =========================================================

@bot.tree.command(name="niveau", description="Affiche ton niveau et ta progression sur ce serveur.")
@app_commands.describe(membre="Membre à afficher (facultatif)")
@auto_interaction_response
async def niveau(interaction, membre: discord.Member=None):
    # Réponse immédiate pour éviter l'expiration pendant les lectures SQLite.
    await interaction.response.defer()
    target=membre or interaction.user
    cfg=get_guild_config(interaction.guild.id)
    data=get_server_member(interaction.guild.id,target.id)
    base=max(1,int(cfg.get("level_xp_base",100) or 100))
    needed=base*max(1,int(data.get("level",1)))
    xp=max(0,int(data.get("xp",0)))
    pct=min(100,int((xp/needed)*100)) if needed else 0
    bars=10; filled=min(bars,int((pct/100)*bars)); progress="█"*filled+"░"*(bars-filled)
    reward_type=str(cfg.get("level_reward_type") or "none")
    reward_amount=max(0,int(cfg.get("level_reward_amount",0) or 0))
    reward="Aucune" if reward_type=="none" or reward_amount<=0 else f"+{reward_amount} { {'argent':server_currency(cfg),'aura':'Aura','jetons':'Jetons'}.get(reward_type,reward_type) }"
    embed=discord.Embed(title=f"📈 NIVEAU — {target.display_name}",color=discord.Color.blurple())
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Niveau",value=f"**{data['level']}**",inline=True)
    embed.add_field(name="XP",value=f"**{xp} / {needed}**",inline=True)
    embed.add_field(name="Progression",value=f"`{progress}` **{pct}%**",inline=False)
    embed.add_field(name="Récompense au niveau suivant",value=reward,inline=False)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="argent", description="Affiche ton argent sur ce serveur.")
@app_commands.describe(membre="Membre à afficher (facultatif)")
@auto_interaction_response
async def argent(interaction, membre: discord.Member=None):
    await interaction.response.defer()
    target=membre or interaction.user
    cfg=get_guild_config(interaction.guild.id)
    data=get_server_member(interaction.guild.id,target.id)
    await interaction.followup.send(f"💵 **{target.display_name}** possède **{data['money']} {server_currency(cfg)}**.")


async def _server_money_rows(guild):
    rows=[]
    for member in guild.members:
        if member.bot: continue
        data=get_server_member(guild.id,member.id)
        rows.append((int(data.get("money",0)),member))
    rows.sort(key=lambda x:x[0],reverse=True)
    return rows


@bot.tree.command(name="topargent", description="Affiche le classement des membres ayant le plus d'argent.")
@auto_interaction_response
async def topargent(interaction):
    await interaction.response.defer()
    cfg=get_guild_config(interaction.guild.id)
    rows=await _server_money_rows(interaction.guild)
    lines=[]
    for i,(value,member) in enumerate(rows[:10],1):
        medal={1:"🥇",2:"🥈",3:"🥉"}.get(i,f"**{i}.**")
        lines.append(f"{medal} {member.mention} — **{value} {server_currency(cfg)}**")
    embed=discord.Embed(title="💵 TOP ARGENT",description="\n".join(lines) or "Aucun joueur.",color=discord.Color.gold())
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="topxp", description="Affiche le classement des membres ayant le plus d'XP.")
@auto_interaction_response
async def topxp(interaction):
    await interaction.response.defer()
    rows=[]
    for member in interaction.guild.members:
        if member.bot: continue
        data=get_server_member(interaction.guild.id,member.id)
        level=int(data.get("level",1)); xp=int(data.get("xp",0))
        # Le niveau reste prioritaire, puis l'XP actuelle pour départager.
        rows.append((level,xp,member))
    rows.sort(key=lambda x:(x[0],x[1]),reverse=True)
    lines=[]
    for i,(level,xp,member) in enumerate(rows[:10],1):
        medal={1:"🥇",2:"🥈",3:"🥉"}.get(i,f"**{i}.**")
        lines.append(f"{medal} {member.mention} — **niveau {level}** • **{xp} XP**")
    embed=discord.Embed(title="⭐ TOP XP",description="\n".join(lines) or "Aucun joueur.",color=discord.Color.gold())
    await interaction.followup.send(embed=embed)


# =========================================================
# GIVEAWAYS
# =========================================================

def get_giveaway(gid):
    with db_lock:
        c=db_connect(); c.row_factory=sqlite3.Row
        r=c.execute("SELECT * FROM giveaways WHERE giveaway_id=?",(int(gid),)).fetchone(); c.close()
    return dict(r) if r else None

def giveaway_entries(gid):
    with db_lock:
        c=db_connect(); r=c.execute("SELECT user_id FROM giveaway_entries WHERE giveaway_id=?",(int(gid),)).fetchall(); c.close()
    return [int(x[0]) for x in r]

def giveaway_conditions(g):
    x=[]
    if g.get('required_role_id'): x.append(f"Rôle requis : <@&{g['required_role_id']}>")
    if int(g.get('min_level') or 0)>0: x.append(f"Niveau minimum : **{g['min_level']}**")
    if g.get('forbidden_role_id'): x.append(f"Rôle interdit : <@&{g['forbidden_role_id']}>")
    if g.get('max_level') is not None: x.append(f"Niveau maximum : **{g['max_level']}**")
    return '\n'.join(x) if x else 'Aucune condition particulière.'

def giveaway_reward_text(g):
    parts = []
    if g.get("giveaway_reward_bot"):
        parts.append(f"🤖 **Récompense AuraBot :** {g['giveaway_reward_bot']}")
    if g.get("giveaway_reward_custom"):
        parts.append(f"🎁 **Récompense custom :** {g['giveaway_reward_custom']}")
    if not parts:
        parts.append(f"🎁 **Récompense :** {g.get('reward') or 'Aucune'}")
    return "\n".join(parts)

def giveaway_embed(g,ended=False,winners=None,count=0):
    if ended:
        text=giveaway_reward_text(g)+"\n\n**Giveaway terminé.**"
        text += "\n\n🏆 **Gagnant(s) :** "+(', '.join(f'<@{u}>' for u in winners) if winners else 'Aucun participant éligible.')
    else:
        ts=int(datetime.fromisoformat(g['ends_at']).timestamp())
        text=giveaway_reward_text(g)+f"\n\n⏰ Fin : <t:{ts}:R>\n👥 Participants : **{count}**\n🏆 Gagnants : **{g['winners_count']}**\n\n**Conditions**\n{giveaway_conditions(g)}"
    return discord.Embed(title=f"🎉 {g['title']}",description=text,color=discord.Color.gold() if not ended else discord.Color.dark_grey())

def giveaway_eligible(member,g):
    if member is None or member.bot: return False,'❌ Les bots ne peuvent pas participer.'
    if g.get('required_role_id') and not any(str(r.id)==str(g['required_role_id']) for r in member.roles): return False,'❌ Tu n’as pas le rôle requis.'
    if g.get('forbidden_role_id') and any(str(r.id)==str(g['forbidden_role_id']) for r in member.roles): return False,'❌ Tu possèdes un rôle interdit.'
    level=int(get_user(member.id).get('level',1)); minimum=int(g.get('min_level') or 0)
    if level<minimum: return False,f'❌ Niveau minimum : **{minimum}**.'
    if g.get('max_level') is not None and level>int(g['max_level']): return False,f'❌ Niveau maximum : **{g["max_level"]}**.'
    return True,None

class GiveawayView(discord.ui.View):
    def __init__(self,gid):
        super().__init__(timeout=None)
        self.gid=int(gid)
        button=discord.ui.Button(label='Participer',style=discord.ButtonStyle.success,emoji='🎟️',custom_id=f'aurabot:giveaway_join:{self.gid}')
        @auto_interaction_response
        async def callback(interaction):
            g=get_giveaway(self.gid)
            if not g or g['status']!='active':
                return await interaction.response.send_message('❌ Ce giveaway est terminé.',ephemeral=True)
            if datetime.fromisoformat(g['ends_at'])<=datetime.now():
                await end_giveaway(self.gid)
                return await interaction.response.send_message('❌ Ce giveaway vient de se terminer.',ephemeral=True)
            ok,reason=giveaway_eligible(interaction.user,g)
            if not ok: return await interaction.response.send_message(reason,ephemeral=True)
            with db_lock:
                c=db_connect(); cur=c.cursor()
                cur.execute('SELECT 1 FROM giveaway_entries WHERE giveaway_id=? AND user_id=?',(self.gid,str(interaction.user.id)))
                exists=cur.fetchone() is not None
                if not exists:
                    cur.execute('INSERT INTO giveaway_entries VALUES(?,?,?)',(self.gid,str(interaction.user.id),datetime.now().isoformat())); c.commit()
                c.close()
            if exists: return await interaction.response.send_message('ℹ️ Tu participes déjà à ce giveaway.',ephemeral=True)
            try: await interaction.message.edit(embed=giveaway_embed(g,count=len(giveaway_entries(self.gid))),view=self)
            except (discord.Forbidden,discord.HTTPException): pass
            await interaction.response.send_message('✅ Participation enregistrée !',ephemeral=True)
        button.callback=callback
        self.add_item(button)

def parse_bot_rewards(raw):
    """Parse les récompenses AuraBot utilisables dans les giveaways.
    Argent et XP sont propres au serveur ; jetons = monnaie des duels.
    """
    result = {"coins":0,"money":0,"xp":0,"aura":0,"sword":0,"armor":0,"reputation":0}
    if not raw:
        return result, []
    aliases = {
        "coin":"coins","coins":"coins","jeton":"coins","jetons":"coins","token":"coins","tokens":"coins",
        "argent":"money","money":"money",
        "xp":"xp","experience":"xp","exp":"xp",
        "aura":"aura",
        "sword":"sword","épée":"sword","epee":"sword",
        "armor":"armor","armure":"armor",
        "reputation":"reputation","rep":"reputation"
    }
    errors=[]
    for part in str(raw).split(","):
        part=part.strip()
        if not part: continue
        import re
        m=re.fullmatch(r"(\d+)\s*([A-Za-zÀ-ÿ]+)",part)
        if not m:
            errors.append(part); continue
        amount=int(m.group(1)); key=aliases.get(m.group(2).lower())
        if not key or amount<=0:
            errors.append(part); continue
        result[key]+=amount
    return result, errors

def bot_reward_display(reward):
    labels=[]
    for key,label in [
        ("coins","💠 Jetons"),("money","💵 Argent"),("xp","⭐ XP"),("aura","✨ Aura"),
        ("sword","⚔️ niveaux d'épée"),("armor","🛡️ niveaux d'armure"),("reputation","⭐ réputation")
    ]:
        if reward.get(key,0): labels.append(f"{reward[key]} {label}")
    return ", ".join(labels) if labels else "Aucune récompense bot"

def grant_giveaway_bot_reward(guild_id, user_id, raw):
    reward, errors=parse_bot_rewards(raw)
    user=get_user(user_id)
    if user is None:
        return reward, errors + ["profil introuvable"]
    if reward["coins"]:
        user["coins"] += reward["coins"]
    if reward["aura"]:
        user["aura"] += reward["aura"]
    user["sword"] = max(1, int(user.get("sword",1)) + reward["sword"])
    user["armor"] = max(1, int(user.get("armor",1)) + reward["armor"])
    user["reputation"] += reward["reputation"]
    save_user(user)
    member = get_server_member(guild_id, user_id)
    if reward["money"]:
        member["money"] += reward["money"]
    if reward["xp"]:
        cfg = get_guild_config(guild_id)
        base = max(1, int(cfg.get("level_xp_base", 100) or 100))
        member["xp"] += reward["xp"]
        while member["xp"] >= base * member["level"]:
            member["xp"] -= base * member["level"]
            member["level"] += 1
    save_server_member(member)
    return reward, errors


ADMIN_REWARD_CHOICES = {
    "argent": "Argent du serveur",
    "xp": "XP du serveur",
    "aura": "Aura",
    "jetons": "Jetons de duel"
}


def apply_admin_reward(guild_id, user_id, reward_type, amount):
    amount = max(0, int(amount))
    if reward_type == "argent":
        member = get_server_member(guild_id, user_id)
        member["money"] += amount
        save_server_member(member)
        return f"+{amount} {server_currency(get_guild_config(guild_id))}"
    if reward_type == "xp":
        levels = add_server_xp_direct(guild_id, user_id, amount)
        return f"+{amount} XP" + (f" • +{levels} niveau(x)" if levels else "")
    user = get_user(user_id)
    if user is None:
        return "profil introuvable"
    if reward_type == "aura":
        user["aura"] += amount
        save_user(user)
        return f"+{amount} Aura"
    if reward_type == "jetons":
        user["coins"] += amount
        save_user(user)
        return f"+{amount} Jetons"
    return "récompense inconnue"


@bot.tree.command(name="give", description="Donne de l'argent ou de l'XP du serveur à un membre.")
@app_commands.describe(
    type="Ressource à donner",
    membre="Membre concerné",
    montant="Quantité à donner"
)
@app_commands.choices(type=[
    app_commands.Choice(name="Argent du serveur", value="argent"),
    app_commands.Choice(name="XP du serveur", value="xp"),
])
@auto_interaction_response
async def give_serveur(
    interaction,
    type: app_commands.Choice[str],
    membre: discord.Member,
    montant: app_commands.Range[int, 1, 1000000]
):
    if not is_admin(interaction.user):
        return await interaction.response.send_message(
            "❌ Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )

    montant = int(montant)
    data = get_server_member(interaction.guild.id, membre.id)

    if type.value == "argent":
        data["money"] = max(0, int(data.get("money", 0))) + montant
        save_server_member(data)
        devise = server_currency(get_guild_config(interaction.guild.id))
        return await interaction.response.send_message(
            f"✅ **{montant} {devise}** donnés à {membre.mention}.\n"
            f"💵 Nouveau solde : **{data['money']}**.", ephemeral=True
        )

    niveaux = add_server_xp_direct(interaction.guild.id, membre.id, montant)
    data = get_server_member(interaction.guild.id, membre.id)
    msg = f"✅ **{montant} XP** donnés à {membre.mention}."
    if niveaux:
        msg += f"\n⬆️ **+{niveaux} niveau(x)**."
    msg += f"\n📈 XP actuel : **{data['xp']}** • Niveau **{data['level']}**."
    return await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="retirer", description="Retire de l'argent ou de l'XP du serveur à un membre.")
@app_commands.describe(
    type="Ressource à retirer",
    membre="Membre concerné",
    montant="Quantité à retirer"
)
@app_commands.choices(type=[
    app_commands.Choice(name="Argent du serveur", value="argent"),
    app_commands.Choice(name="XP du serveur", value="xp"),
])
@auto_interaction_response
async def retirer_serveur(
    interaction,
    type: app_commands.Choice[str],
    membre: discord.Member,
    montant: app_commands.Range[int, 1, 1000000]
):
    if not is_admin(interaction.user):
        return await interaction.response.send_message(
            "❌ Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )

    montant = int(montant)
    data = get_server_member(interaction.guild.id, membre.id)

    if type.value == "argent":
        ancien = max(0, int(data.get("money", 0)))
        retire = min(ancien, montant)
        data["money"] = ancien - retire
        save_server_member(data)
        devise = server_currency(get_guild_config(interaction.guild.id))
        return await interaction.response.send_message(
            f"✅ **{retire} {devise}** retirés à {membre.mention}.\n"
            f"💵 Nouveau solde : **{data['money']}**.", ephemeral=True
        )

    # Retrait d'XP avec descente de niveau si nécessaire.
    ancien_xp = max(0, int(data.get("xp", 0)))
    ancien_niveau = max(1, int(data.get("level", 1)))
    restant = montant
    niveau = ancien_niveau
    xp = ancien_xp
    base = max(1, int(get_guild_config(interaction.guild.id).get("level_xp_base", 100) or 100))

    total_before = ancien_xp
    for lv in range(ancien_niveau - 1, 0, -1):
        total_before += base * lv
    retire = min(montant, total_before)

    restant = retire
    while restant > 0 and niveau > 1:
        if restant <= xp:
            xp -= restant
            restant = 0
            break
        restant -= xp
        niveau -= 1
        xp = base * niveau

    if restant > 0:
        xp = max(0, xp - restant)

    data["level"] = max(1, niveau)
    data["xp"] = max(0, xp)
    save_server_member(data)

    return await interaction.response.send_message(
        f"✅ **{retire} XP** retirés à {membre.mention}.\n"
        f"📈 XP actuel : **{data['xp']}** • Niveau **{data['level']}**.",
        ephemeral=True
    )


@bot.tree.command(name="retirer_aura", description="Retire de l'Aura — propriétaire d'AuraBot uniquement.")
@app_commands.describe(
    membre="Membre concerné",
    montant="Quantité d'Aura à retirer"
)
@auto_interaction_response
async def retirer_aura(interaction, membre: discord.Member, montant: app_commands.Range[int, 1, 1000000]):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message(
            "❌ Cette commande est réservée au propriétaire d'AuraBot.", ephemeral=True
        )
    user = get_user(membre.id)
    if user is None:
        return await interaction.response.send_message("❌ Profil introuvable.", ephemeral=True)
    ancien = max(0, int(user.get("aura", 0)))
    retire = min(ancien, int(montant))
    user["aura"] = ancien - retire
    save_user(user)
    await interaction.response.send_message(
        f"✅ **{retire} Aura** retirée à {membre.mention}.\n"
        f"✨ Aura restante : **{user['aura']}**.", ephemeral=True
    )


@bot.tree.command(name="retirer_amelioration", description="Retire une amélioration de duel — propriétaire d'AuraBot uniquement.")
@app_commands.describe(
    type="Amélioration à retirer",
    membre="Membre concerné",
    niveaux="Nombre de niveaux à retirer"
)
@app_commands.choices(type=[
    app_commands.Choice(name="Épée", value="sword"),
    app_commands.Choice(name="Armure", value="armor"),
])
@auto_interaction_response
async def retirer_amelioration(interaction, type: app_commands.Choice[str], membre: discord.Member, niveaux: app_commands.Range[int, 1, 100]):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message(
            "❌ Cette commande est réservée au propriétaire d'AuraBot.", ephemeral=True
        )
    user = get_user(membre.id)
    if user is None:
        return await interaction.response.send_message("❌ Profil introuvable.", ephemeral=True)
    cle = type.value
    ancien = max(1, int(user.get(cle, 1)))
    retire = min(max(0, ancien - 1), int(niveaux))
    user[cle] = max(1, ancien - retire)
    save_user(user)
    nom = "Épée" if cle == "sword" else "Armure"
    await interaction.response.send_message(
        f"✅ **{retire} niveau(x)** d'{nom} retiré(s) à {membre.mention}.\n"
        f"{'⚔️' if cle == 'sword' else '🛡️'} Niveau restant : **{user[cle]}**.", ephemeral=True
    )


@bot.tree.command(name="retirer_jetons", description="Retire des jetons de duel — propriétaire d'AuraBot uniquement.")
@app_commands.describe(
    membre="Membre concerné",
    montant="Quantité de jetons à retirer"
)
@auto_interaction_response
async def retirer_jetons(
    interaction,
    membre: discord.Member,
    montant: app_commands.Range[int, 1, 1000000]
):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message(
            "❌ Cette commande est réservée au propriétaire d'AuraBot.", ephemeral=True
        )

    user = get_user(membre.id)
    if user is None:
        return await interaction.response.send_message("❌ Profil introuvable.", ephemeral=True)

    ancien = max(0, int(user.get("coins", 0)))
    retire = min(ancien, int(montant))
    user["coins"] = ancien - retire
    save_user(user)

    return await interaction.response.send_message(
        f"✅ **{retire} jetons de duel** retirés à {membre.mention}.\n"
        f"🪙 Solde restant : **{user['coins']}**.", ephemeral=True
    )


@bot.tree.command(name="drop", description="Crée un drop récupérable par le premier membre qui clique.")
@app_commands.describe(type="Récompense du drop", montant="Montant", duree="Durée du drop en secondes")
@app_commands.choices(type=[app_commands.Choice(name=v, value=k) for k,v in ADMIN_REWARD_CHOICES.items()])
@app_commands.default_permissions(administrator=True)
@auto_interaction_response
async def drop_reward(interaction, type: app_commands.Choice[str], montant: app_commands.Range[int,1,1000000], duree: app_commands.Range[int,5,300]=60):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Permission Administrateur requise.", ephemeral=True)
    view = DropView(interaction.guild.id, type.value, montant, interaction.user.id, int(duree))
    await interaction.response.send_message(
        f"🎁 **DROP !**\n**{ADMIN_REWARD_CHOICES[type.value]} : {montant}**\nClique sur le bouton pour le récupérer !",
        view=view
    )

async def end_giveaway(gid):
    g=get_giveaway(gid)
    if not g or g['status']!='active': return
    with db_lock:
        c=db_connect(); c.execute("UPDATE giveaways SET status='ended' WHERE giveaway_id=?",(int(gid),)); c.commit(); c.close()
    guild=bot.get_guild(int(g['guild_id'])); channel=guild.get_channel(int(g['channel_id'])) if guild else None
    eligible=[]
    if guild:
        for uid in giveaway_entries(gid):
            ok,_=giveaway_eligible(guild.get_member(uid),g)
            if ok: eligible.append(uid)
    winners=random.sample(eligible,min(int(g['winners_count']),len(eligible))) if eligible else []
    with db_lock:
        c=db_connect(); c.execute('UPDATE giveaways SET winner_ids=? WHERE giveaway_id=?',(','.join(map(str,winners)),int(gid))); c.commit(); c.close()
    reward_lines=[]
    for uid in winners:
        reward, errors=grant_giveaway_bot_reward(g["guild_id"], uid, g.get("giveaway_reward_bot") or "")
        if errors:
            print(f"⚠️ Récompense bot invalide giveaway #{gid} : {errors}")
        personal=[]
        display=bot_reward_display(reward)
        if display != "Aucune récompense bot": personal.append(display)
        if g.get("giveaway_reward_custom"):
            personal.append(f"🎁 {g['giveaway_reward_custom']}")
        if personal:
            reward_lines.append(f"<@{uid}> : " + " + ".join(personal))
    if channel:
        try:
            m=await channel.fetch_message(int(g['message_id'])); await m.edit(embed=giveaway_embed(g,ended=True,winners=winners),view=None)
            if winners:
                msg='🎉 **Giveaway terminé !**\n🏆 Gagnant(s) : '+', '.join(f'<@{u}>' for u in winners)
                if reward_lines: msg += "\n\n🎁 **Récompenses distribuées**\n" + "\n".join(reward_lines)
                await channel.send(msg)
            else: await channel.send(f"❌ Le giveaway **{g['title']}** est terminé, mais aucun participant n'est éligible.")
        except (discord.NotFound,discord.Forbidden,discord.HTTPException): pass

@tasks.loop(seconds=15)
async def giveaway_watcher():
    now=datetime.now().isoformat()
    with db_lock:
        c=db_connect(); rows=c.execute("SELECT giveaway_id FROM giveaways WHERE status='active' AND ends_at<=?",(now,)).fetchall(); c.close()
    for (gid,) in rows: await end_giveaway(gid)

@bot.tree.command(name='giveaway',description='Crée un giveaway avec récompenses AuraBot et/ou personnalisées.')
@app_commands.describe(
    titre='Titre du giveaway',
    recompense_bot='Récompenses AuraBot, ex: 100 coins, 10 aura',
    recompense_custom='Récompense personnalisée, ex: carte cadeau 10€',
    duree='Durée en minutes',
    gagnants='Nombre de gagnants',
    role_requis='Rôle obligatoire',
    niveau_min='Niveau minimum',
    role_interdit='Rôle interdit',
    niveau_max='Niveau maximum autorisé'
)
@app_commands.default_permissions(administrator=True)
@auto_interaction_response
async def giveaway(
    interaction,
    titre:str,
    recompense_bot:str=None,
    recompense_custom:str=None,
    duree:app_commands.Range[int,1,10080]=60,
    gagnants:app_commands.Range[int,1,20]=1,
    role_requis:discord.Role=None,
    niveau_min:app_commands.Range[int,0,10000]=0,
    role_interdit:discord.Role=None,
    niveau_max:app_commands.Range[int,0,10000]=None
):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "❌ Tu dois avoir la permission **Administrateur** pour créer un giveaway.",
            ephemeral=True
        )
    titre=titre.strip()[:100]
    recompense_bot=(recompense_bot or '').strip()[:500]
    recompense_custom=(recompense_custom or '').strip()[:500]
    if not titre: return await interaction.response.send_message('❌ Le titre est obligatoire.',ephemeral=True)
    if not recompense_bot and not recompense_custom:
        return await interaction.response.send_message('❌ Ajoute au moins une récompense AuraBot ou une récompense custom.',ephemeral=True)
    if recompense_bot:
        parsed,errors=parse_bot_rewards(recompense_bot)
        if errors:
            return await interaction.response.send_message('❌ Récompense AuraBot invalide : '+', '.join(f'`{x}`' for x in errors)+'\nExemples : `500 argent, 100 xp, 50 aura, 25 jetons`.',ephemeral=True)
        if not any(parsed.values()):
            return await interaction.response.send_message('❌ La récompense AuraBot ne contient aucun objet valide.',ephemeral=True)
    if role_requis and role_interdit and role_requis.id==role_interdit.id: return await interaction.response.send_message('❌ Le même rôle ne peut pas être requis et interdit.',ephemeral=True)
    if niveau_max is not None and niveau_max<niveau_min: return await interaction.response.send_message('❌ Le niveau maximum doit être supérieur ou égal au niveau minimum.',ephemeral=True)
    ends=datetime.now()+timedelta(minutes=int(duree))
    legacy_reward = recompense_custom or recompense_bot
    with db_lock:
        c=db_connect(); cur=c.cursor()
        cur.execute("INSERT INTO giveaways(guild_id,channel_id,title,reward,ends_at,winners_count,required_role_id,forbidden_role_id,min_level,max_level,created_by,giveaway_reward_bot,giveaway_reward_custom) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(str(interaction.guild.id),str(interaction.channel.id),titre,legacy_reward,ends.isoformat(),int(gagnants),str(role_requis.id) if role_requis else None,str(role_interdit.id) if role_interdit else None,int(niveau_min),int(niveau_max) if niveau_max is not None else None,str(interaction.user.id),recompense_bot or None,recompense_custom or None))
        gid=cur.lastrowid; c.commit(); c.close()
    g=get_giveaway(gid); await interaction.response.send_message(embed=giveaway_embed(g),view=GiveawayView(gid)); m=await interaction.original_response()
    with db_lock:
        c=db_connect(); c.execute('UPDATE giveaways SET message_id=? WHERE giveaway_id=?',(str(m.id),gid)); c.commit(); c.close()
    if not giveaway_watcher.is_running(): giveaway_watcher.start()

# =========================================================
# ERREUR GLOBALE DES VUES / BOUTONS / MODALS
# =========================================================

@bot.event
async def on_error(event, *args, **kwargs):
    import traceback
    print(f"\n❌ ERREUR GLOBALE DISCORD : {event}")
    traceback.print_exc()


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():
    global persistent_views_registered, commands_synced_once
    print(f"🤖 AuraBot connecté : {bot.user}")
    print(f"🆔 ID : {bot.user.id}")

    try:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Game(
                name="MP rodrax pour m'ajouter dans ton serveur"
            )
        )
    except Exception as e:
        print("⚠️ Impossible de définir le statut :", e)

    try:
        if not persistent_views_registered:
            for guild in bot.guilds:
                try:
                    bot.add_view(TicketPanelView(guild))
                except Exception as e:
                    print(f"⚠️ Vue tickets impossible pour {guild.id} : {e}")
            bot.add_view(TicketCloseView(0))
            bot.add_view(ConfessionReportView())
            bot.add_view(ConfessionPanelView())
        if not persistent_views_registered:
            for channel_id, owner_id in temp_voice_db_all():
                try:
                    if bot.get_channel(int(channel_id)):
                        bot.add_view(TempVoicePanel(int(channel_id), int(owner_id)))
                except Exception:
                    pass
            with db_lock:
                conn=db_connect()
                active_giveaways=[r[0] for r in conn.execute("SELECT giveaway_id FROM giveaways WHERE status='active'").fetchall()]
                conn.close()
            for gid in active_giveaways:
                try: bot.add_view(GiveawayView(int(gid)))
                except Exception: pass
            persistent_views_registered = True
        if not giveaway_watcher.is_running(): giveaway_watcher.start()
    except Exception as e:
        print("⚠️ Impossible d'enregistrer les vues persistantes :", repr(e))

    if commands_synced_once:
        return

    try:
        # IMPORTANT : ne pas enregistrer les mêmes commandes en scope serveur ET global.
        # Sinon Discord peut afficher chaque slash-command deux fois.
        GUILD_ID = 1522344351756325088
        guild = discord.Object(id=GUILD_ID)

        # Supprime explicitement les anciennes commandes qui ne doivent plus exister.
        # Cela force Discord à retirer les versions globales déjà publiées.
        for old_name in ("confession", "animation"):
            try:
                bot.tree.remove_command(old_name)
                bot.tree.remove_command(old_name, guild=guild)
            except Exception:
                pass

        # Nettoie uniquement les anciennes commandes propres au serveur de test.
        # Les commandes actuelles sont ensuite publiées une seule fois en global.
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)

        synced = await bot.tree.sync()
        names = [cmd.name for cmd in synced]
        print(f"✅ {len(synced)} commandes globales synchronisées.")
        print("🧹 Anciennes commandes serveur supprimées : plus de doublons de scope.")
        if "config" not in names:
            print("❌ /config n'a pas été enregistré. Commandes reçues :", names)
        else:
            print("⚙️ /config présent : OUI")
        commands_synced_once = True
    except Exception as e:
        print("❌ Erreur synchronisation commandes :", repr(e))


# =========================================================
# LANCEMENT
# =========================================================

if __name__ == "__main__":
    if not TOKEN:
        print(
            "❌ Variable d’environnement DISCORD_TOKEN manquante. "
            "Ajoute DISCORD_TOKEN dans les variables/secrets de ton hébergeur."
        )
    else:
        init_db()
        bot.run(TOKEN)
