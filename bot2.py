import os
import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import asyncio
from dotenv import load_dotenv
from collections import deque
import sqlite3
from datetime import timedelta
import re
import sys
import logging

# Set up logging
logging.basicConfig(filename='bot.log', level=logging.INFO, 
                    format='%(asctime)s:%(levelname)s:%(message)s')

# Set console encoding to UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
intents = discord.Intents.all()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, reconnect=True)
tree = bot.tree

music_players = {}
CACHE_DIR = "cache"
MAX_CACHE_SIZE = 1 * 1024 * 1024 * 1024  # 1GB
MAX_SKIP_ATTEMPTS = 3  # Limit skips per song

REACTS = {
    "⏮️": "prev",
    "▶️": "resume",
    "⏹️": "stop",
    "⏭️": "skip",
    "🔁": "loop",
    "⭐": "fav",
    "❌": "exit"
}

# Initialize SQLite database
def init_db():
    conn = sqlite3.connect("favorites.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS favorites
                 (user_id INTEGER, song_title TEXT, song_url TEXT, thumbnail TEXT)''')
    conn.commit()
    conn.close()

# Add favorite song to database
def add_favorite(user_id, song_title, song_url, thumbnail):
    conn = sqlite3.connect("favorites.db")
    c = conn.cursor()
    c.execute("INSERT INTO favorites (user_id, song_title, song_url, thumbnail) VALUES (?, ?, ?, ?)",
              (user_id, song_title, song_url, thumbnail))
    conn.commit()
    conn.close()
    logging.info(f"Added favorite: {song_title} with URL {song_url} for user {user_id}")

# Get favorite songs for a user
def get_favorites(user_id):
    conn = sqlite3.connect("favorites.db")
    c = conn.cursor()
    c.execute("SELECT song_title, song_url, thumbnail FROM favorites WHERE user_id = ?", (user_id,))
    favorites = c.fetchall()
    conn.close()
    return [{"title": row[0], "url": row[1], "thumbnail": row[2]} for row in favorites]

# Cache management
def ensure_cache_dir():
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)

def get_cache_size():
    total_size = 0
    for dirpath, _, filenames in os.walk(CACHE_DIR):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp)
    return total_size

def clear_oldest_cache():
    files = [(os.path.join(CACHE_DIR, f), os.path.getmtime(os.path.join(CACHE_DIR, f)))
             for f in os.listdir(CACHE_DIR) if os.path.isfile(os.path.join(CACHE_DIR, f))]
    if files:
        oldest_file = min(files, key=lambda x: x[1])[0]
        os.remove(oldest_file)

def sanitize_filename(filename):
    return re.sub(r'[<>:"/\\|?*]', '_', filename)

class MusicPlayer:
    def __init__(self, guild):
        self.guild = guild
        self.queue = deque()
        self.current = None
        self.previous = None
        self.loop = False
        self.voice_client = None
        self.message = None
        self.text_channel = None
        self.skip_attempts = {}
        self.is_exiting = False

    async def play_next(self):
        if self.is_exiting:
            logging.info("play_next skipped due to exit flag")
            return

        if self.loop and self.current:
            self.queue.append(self.current)
        else:
            self.previous = self.current

        if not self.queue:
            self.current = None
            if self.message:
                try:
                    msg = await self.text_channel.send("❌ No more songs in the queue.")
                    await asyncio.sleep(5)
                    await msg.delete()
                except discord.Forbidden:
                    logging.error(f"No permission to send/delete message in {self.text_channel.name}")
            return

        self.current = self.queue.popleft()
        song_title = self.current['title']
        cache_path = os.path.join(CACHE_DIR, f"{sanitize_filename(song_title)}.mp3")

        # Log duration and URLs for debugging
        duration = self.current.get("duration")
        original_url = self.current.get("webpage_url")
        logging.info(f"Playing {song_title}: duration={duration}, original_url={original_url}, stream_url={self.current['url']}")

        # Check skip attempts
        self.skip_attempts[song_title] = self.skip_attempts.get(song_title, 0) + 1
        if self.skip_attempts[song_title] > MAX_SKIP_ATTEMPTS:
            try:
                await self.text_channel.send(f"❌ Skipped {song_title} after {MAX_SKIP_ATTEMPTS} failed attempts.")
                self.skip_attempts[song_title] = 0
                await self.play_next()
            except discord.Forbidden:
                logging.error(f"No permission to send message in {self.text_channel.name}")
            return

        try:
            if os.path.exists(cache_path):
                source = discord.FFmpegPCMAudio(
                    cache_path,
                    before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                    options="-vn",
                    executable="bin\\ffmpeg.exe"
                )
            else:
                ydl_opts = {"format": "bestaudio/best", "quiet": True, "geo_bypass": True}
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(self.current['url'], download=False)
                        self.current['url'] = info['url']
                        self.current['duration'] = info.get('duration', self.current.get('duration', 0))
                        self.current['webpage_url'] = info.get('webpage_url', self.current.get('webpage_url', self.current['url']))
                        logging.info(f"Extracted new URL for {song_title}: {self.current['url']}")
                except yt_dlp.utils.DownloadError as e:
                    logging.error(f"Failed to extract URL for {song_title}: {str(e)}")
                    try:
                        await self.text_channel.send(f"❌ Failed to play {song_title}. Skipping...")
                        await asyncio.sleep(5)
                        await self.play_next()
                    except discord.Forbidden:
                        logging.error(f"No permission to send message in {self.text_channel.name}")
                    return

                source = discord.FFmpegPCMAudio(
                    self.current['url'],
                    before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                    options="-vn",
                    executable="bin\\ffmpeg.exe"
                )
                if get_cache_size() < MAX_CACHE_SIZE:
                    ydl_opts = {"outtmpl": cache_path, "format": "bestaudio[ext=mp3]", "quiet": True}
                    try:
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([self.current['url']])
                        logging.info(f"Cached {song_title} at {cache_path}")
                    except yt_dlp.utils.DownloadError as e:
                        logging.error(f"Failed to cache {song_title}: {str(e)}")
                else:
                    clear_oldest_cache()
                    try:
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([self.current['url']])
                        logging.info(f"Cached {song_title} at {cache_path} after clearing oldest")
                    except yt_dlp.utils.DownloadError as e:
                        logging.error(f"Failed to cache {song_title}: {str(e)}")

            if not self.voice_client or not self.voice_client.is_connected():
                logging.error(f"Voice client not connected in guild {self.guild.id}")
                return

            self.voice_client.play(
                source,
                after=lambda e: asyncio.run_coroutine_threadsafe(self.play_next(), bot.loop)
            )
            self.skip_attempts[song_title] = 0
            await self.send_embed()
        except Exception as e:
            logging.error(f"Error playing {song_title}: {str(e)}")
            try:
                await self.text_channel.send(f"❌ Failed to play {song_title}. Skipping...")
                await asyncio.sleep(5)
                await self.play_next()
            except discord.Forbidden:
                logging.error(f"No permission to send message in {self.text_channel.name}")

    async def send_embed(self):
        if not self.text_channel.permissions_for(self.guild.me).send_messages:
            logging.error(f"No permission to send embed in {self.text_channel.name}")
            return

        embed = discord.Embed(title="🎵 Now Playing", color=discord.Color.from_rgb(29, 185, 84))
        current_title = f"**🎶 {self.current['title']}**" if self.current else "*None*"
        embed.description = current_title
        duration = self.current.get("duration", 0) if self.current else 0
        duration_text = str(timedelta(seconds=int(duration))) if duration and isinstance(duration, (int, float)) else "Unknown"
        embed.add_field(name="Duration", value=duration_text, inline=True)
        embed.add_field(name="Loop", value="🔁 Enabled" if self.loop else "Disabled", inline=True)
        embed.set_thumbnail(url=self.current.get("thumbnail", "") if self.current else "")
        
        if self.queue:
            queue_text = ""
            for idx, song in enumerate(self.queue, 1):
                title = song.get("title", "Unknown title")
                queue_text += f"*{idx}. {title}*\n"
            embed.add_field(name="🎧 Up Next", value=queue_text[:1024], inline=False)
        else:
            embed.add_field(name="🎧 Up Next", value="*No songs in queue.*", inline=False)

        try:
            if self.message:
                await self.message.edit(embed=embed)
            else:
                self.message = await self.text_channel.send(embed=embed)
                for emoji in REACTS:
                    await self.message.add_reaction(emoji)
        except discord.Forbidden:
            logging.error(f"No permission to send/edit embed in {self.text_channel.name}")

async def ytdlp_search(query):
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "default_search": "auto",
        "noplaylist": False,
        "download": False,
        "geo_bypass": True,
        "extract_flat": False
    }
    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))
        logging.info(f"Extracted {len(results)} songs from query: {query}")
        return results
    except yt_dlp.utils.DownloadError as e:
        logging.error(f"Error in ytdlp_search for query {query}: {str(e)}")
        return []

def _extract(query, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                entries = info['entries']
                logging.info(f"Found {len(entries)} entries in playlist for query: {query}")
            else:
                entries = [info]
                logging.info(f"Single entry for query: {query}")
            return [{"title": entry["title"], "url": entry["url"], "thumbnail": entry.get("thumbnail", ""), 
                     "duration": entry.get("duration", 0), "webpage_url": entry.get("webpage_url", entry["url"])} 
                    for entry in entries]
        except yt_dlp.utils.DownloadError as e:
            logging.error(f"Error extracting info for query {query}: {str(e)}")
            return []

@tree.command(name="play", description="Play a song or playlist in your voice channel")
async def slash_play(interaction: discord.Interaction, query: str):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        logging.error(f"Failed to defer interaction for /play in guild {interaction.guild.id}")
        return

    vc = interaction.user.voice
    if not vc:
        try:
            await interaction.followup.send("❌ You must be in a voice channel.", ephemeral=True)
        except discord.errors.NotFound:
            logging.error(f"Failed to send followup in guild {interaction.guild.id}")
        return

    if not interaction.channel.permissions_for(interaction.guild.me).send_messages:
        logging.error(f"No permission to send messages in {interaction.channel.name} ({interaction.guild.id})")
        try:
            await interaction.followup.send("❌ Bot lacks permission to send messages in this channel.", ephemeral=True)
        except discord.errors.NotFound:
            pass
        return

    if not vc.channel.permissions_for(interaction.guild.me).connect:
        try:
            await interaction.followup.send("❌ Bot lacks permission to connect to the voice channel.", ephemeral=True)
        except discord.errors.NotFound:
            pass
        return

    player = music_players.get(interaction.guild.id)
    if not player:
        player = MusicPlayer(interaction.guild)
        music_players[interaction.guild.id] = player

    if not player.voice_client or not player.voice_client.is_connected():
        try:
            player.voice_client = await vc.channel.connect()
        except discord.Forbidden:
            try:
                await interaction.followup.send("❌ Bot lacks permission to join the voice channel.", ephemeral=True)
            except discord.errors.NotFound:
                pass
            return
        except discord.ClientException:
            logging.error(f"Failed to connect to voice channel in guild {interaction.guild.id}")
            return

    results = await ytdlp_search(query)
    if not results:
        try:
            await interaction.followup.send("❌ No results found for the query.", ephemeral=True)
        except discord.errors.NotFound:
            pass
        return

    for r in results:
        player.queue.append(r)

    player.text_channel = interaction.channel
    if not player.voice_client.is_playing() and not player.voice_client.is_paused():
        await player.play_next()
        try:
            msg = await interaction.followup.send(f"▶️ Playing: {results[0]['title']}", ephemeral=True)
            await asyncio.sleep(5)
            await msg.delete()
        except (discord.errors.NotFound, discord.Forbidden):
            pass
    elif player.voice_client.is_paused():
        player.voice_client.resume()
        try:
            await interaction.followup.send("▶️ Resumed playback.", ephemeral=True)
        except discord.errors.NotFound:
            pass
    else:
        try:
            msg = await interaction.followup.send(f"🎶 Added {len(results)} song(s) to queue.", ephemeral=True)
            await asyncio.sleep(5)
            await msg.delete()
        except (discord.errors.NotFound, discord.Forbidden):
            pass
    
    await player.send_embed()

@tree.command(name="fav", description="Play your favorite songs")
async def slash_fav(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        logging.error(f"Failed to defer interaction for /fav in guild {interaction.guild.id}")
        return

    vc = interaction.user.voice
    if not vc:
        try:
            await interaction.followup.send("❌ You must be in a voice channel.", ephemeral=True)
        except discord.errors.NotFound:
            pass
        return

    if not interaction.channel.permissions_for(interaction.guild.me).send_messages:
        logging.error(f"No permission to send messages in {interaction.channel.name} ({interaction.guild.id})")
        try:
            await interaction.followup.send("❌ Bot lacks permission to send messages in this channel.", ephemeral=True)
        except discord.errors.NotFound:
            pass
        return

    if not vc.channel.permissions_for(interaction.guild.me).connect:
        try:
            await interaction.followup.send("❌ Bot lacks permission to connect to the voice channel.", ephemeral=True)
        except discord.errors.NotFound:
            pass
        return

    player = music_players.get(interaction.guild.id)
    if not player:
        player = MusicPlayer(interaction.guild)
        music_players[interaction.guild.id] = player

    if not player.voice_client or not player.voice_client.is_connected():
        try:
            player.voice_client = await vc.channel.connect()
        except discord.Forbidden:
            try:
                await interaction.followup.send("❌ Bot lacks permission to join the voice channel.", ephemeral=True)
            except discord.errors.NotFound:
                pass
            return
        except discord.ClientException:
            logging.error(f"Failed to connect to voice channel in guild {interaction.guild.id}")
            return

    favorites = get_favorites(interaction.user.id)
    if not favorites:
        try:
            await interaction.followup.send("❌ You have no favorite songs.", ephemeral=True)
        except discord.errors.NotFound:
            pass
        return

    # Re-extract favorites to get streamable URLs
    for song in favorites:
        ydl_opts = {"format": "bestaudio/best", "quiet": True, "geo_bypass": True}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(song['url'], download=False)
                player.queue.append({
                    "title": song['title'],
                    "url": info['url'],
                    "thumbnail": song['thumbnail'],
                    "duration": info.get('duration', 0),
                    "webpage_url": song['url']
                })
        except yt_dlp.utils.DownloadError as e:
            logging.error(f"Failed to extract favorite song {song['title']}: {str(e)}")
            continue

    player.text_channel = interaction.channel
    if not player.voice_client.is_playing() and not player.voice_client.is_paused():
        await player.play_next()
        try:
            msg = await interaction.followup.send(f"▶️ Playing favorite: {favorites[0]['title']}", ephemeral=True)
            await asyncio.sleep(5)
            await msg.delete()
        except (discord.errors.NotFound, discord.Forbidden):
            pass
    else:
        try:
            msg = await interaction.followup.send(f"🎶 Added {len(favorites)} favorite song(s) to queue.", ephemeral=True)
            await asyncio.sleep(5)
            await msg.delete()
        except (discord.errors.NotFound, discord.Forbidden):
            pass

    await player.send_embed()

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot or reaction.message.author != bot.user:
        return
    for player in music_players.values():
        if player.message and reaction.message.id == player.message.id:
            if str(reaction.emoji) not in REACTS:
                try:
                    await reaction.message.remove_reaction(reaction.emoji, user)
                except discord.Forbidden:
                    pass
                return
            action = REACTS.get(str(reaction.emoji))
            if action:
                await handle_action(action, player, user, reaction.message)
                try:
                    await reaction.message.remove_reaction(reaction.emoji, user)
                except discord.Forbidden:
                    pass

async def handle_action(action, player, user, message):
    if not user.voice or not user.voice.channel:
        try:
            msg = await player.text_channel.send("❌ You must be in a voice channel.", ephemeral=True)
            await asyncio.sleep(5)
            await msg.delete()
        except discord.Forbidden:
            logging.error(f"No permission to send message in {player.text_channel.name}")
        return

    if not player.text_channel.permissions_for(player.guild.me).send_messages:
        logging.error(f"No permission to send messages in {player.text_channel.name} ({player.guild.id})")
        return

    if action == "prev":
        if player.loop and not player.previous and player.queue:
            player.previous = player.current
            player.current = player.queue.pop()
        elif player.previous:
            if player.current:
                player.queue.appendleft(player.current)
            player.current = player.previous
            player.previous = None
        else:
            return
        if player.voice_client:
            try:
                await player.voice_client.disconnect()
            except discord.ClientException:
                pass
        try:
            player.voice_client = await user.voice.channel.connect()
            await player.play_next()
        except discord.Forbidden:
            try:
                await player.text_channel.send("❌ Bot lacks permission to join the voice channel.", ephemeral=True)
            except discord.Forbidden:
                pass
        except discord.ClientException:
            logging.error(f"Failed to reconnect to voice channel in guild {player.guild.id}")

    elif action == "resume":
        if player.voice_client.is_paused():
            player.voice_client.resume()
            try:
                msg = await player.text_channel.send("▶️ Resumed playback.")
                await asyncio.sleep(5)
                await msg.delete()
            except discord.Forbidden:
                logging.error(f"No permission to send message in {player.text_channel.name}")
        elif not player.voice_client.is_playing():
            await player.play_next()

    elif action == "stop":
        if player.voice_client.is_playing():
            player.voice_client.pause()
            await player.send_embed()
            try:
                msg = await player.text_channel.send("⏹️ Stopped playback.")
                await asyncio.sleep(5)
                await msg.delete()
            except discord.Forbidden:
                logging.error(f"No permission to send message in {player.text_channel.name}")
        else:
            try:
                msg = await player.text_channel.send("⏹️ No song currently playing.")
                await asyncio.sleep(5)
                await msg.delete()
            except discord.Forbidden:
                logging.error(f"No permission to send message in {player.text_channel.name}")

    elif action == "skip":
        if player.voice_client:
            player.voice_client.stop()

    elif action == "loop":
        player.loop = not player.loop
        await player.send_embed()

    elif action == "fav":
        if player.current:
            add_favorite(user.id, player.current['title'], player.current['webpage_url'], player.current.get('thumbnail', ''))
            try:
                msg = await player.text_channel.send(f"⭐ Added to favorites: {player.current['title']}")
                await asyncio.sleep(5)
                await msg.delete()
            except discord.Forbidden:
                logging.error(f"No permission to send message in {player.text_channel.name}")

    elif action == "exit":
        player.is_exiting = True
        if player.voice_client:
            if player.voice_client.is_playing() or player.voice_client.is_paused():
                player.voice_client.stop()
            try:
                await player.voice_client.disconnect()
            except discord.ClientException:
                pass
        player.queue.clear()
        player.current = None
        player.previous = None
        player.skip_attempts.clear()
        try:
            async for msg in player.text_channel.history(limit=100):
                if msg.author.id == bot.user.id:
                    await msg.delete()
        except discord.Forbidden:
            logging.error(f"No permission to delete messages in {player.text_channel.name} ({player.guild.id})")
        except discord.HTTPException as e:
            logging.error(f"Failed to delete messages: {str(e)}")
        music_players.pop(player.guild.id, None)
        try:
            msg = await player.text_channel.send("❌ Bot exited voice channel.")
            await asyncio.sleep(5)
            await msg.delete()
        except discord.Forbidden:
            logging.error(f"No permission to send message in {player.text_channel.name}")
        logging.info(f"Bot exited voice channel in guild {player.guild.id}")

@bot.event
async def on_ready():
    init_db()
    ensure_cache_dir()
    await tree.sync()
    print(f"✅ {bot.user} is ready and commands are synced.")
    logging.info(f"Bot {bot.user} started")
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if not channel.permissions_for(guild.me).read_message_history:
                continue
            if not channel.permissions_for(guild.me).manage_messages:
                continue
            try:
                async for msg in channel.history(limit=50):
                    if msg.author.id == bot.user.id:
                        await msg.delete()
            except discord.Forbidden:
                print(f"❌ No permission to delete in {channel.name} ({channel.id})")
            except discord.HTTPException as e:
                print(f"⚠️ Failed to delete messages: {e}")

bot.run(TOKEN)