import os
import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import asyncio
from dotenv import load_dotenv
from collections import deque

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

music_players = {}

REACTS = {
    "⏮️": "prev",
    "▶️": "resume",
    "⏹️": "stop",
    "⏭️": "skip",
    "🔁": "loop",
    "⭐": "fav",
    "❌": "exit"
}

class MusicPlayer:
    def __init__(self, guild):
        self.guild = guild
        self.queue = deque()
        self.current = None
        self.previous = None  # ← เพิ่มตัวแปรเก็บเพลงก่อนหน้า
        self.loop = False
        self.voice_client = None
        self.message = None
        self.text_channel = None

    async def play_next(self):
        if self.loop and self.current:
            # เก็บเพลงปัจจุบันไว้ที่ท้ายคิวเมื่ออยู่ในโหมด loop
            self.queue.append(self.current)
        else:
            # ถ้าไม่อยู่ในโหมด loop เก็บเพลงปัจจุบันเป็นเพลงก่อนหน้า
            self.previous = self.current

        if not self.queue:
            # ไม่มีเพลงถัดไป → เก็บ current เหมือนเดิม
            if self.message:
                msg = await self.text_channel.send("❌ No more songs in the queue.")
                await asyncio.sleep(5)
                await msg.delete()
            return

        # มีเพลงถัดไป → เก็บเพลงก่อนหน้าไว้
        self.previous = self.current
        self.current = self.queue.popleft()

        source = discord.FFmpegPCMAudio(
            self.current['url'],
            before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            options="-vn",
            executable="bin\\ffmpeg.exe"
        )
        self.voice_client.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(self.play_next(), bot.loop)
        )
        await self.send_embed()

    async def send_embed(self):
        embed = discord.Embed(title="🎵 Now Playing", color=0x1db954)
    
        # เพลงกำลังเล่น
        current_title = f"**🎶 {self.current['title']}**" if self.current else "*None*"
        embed.description = current_title

        embed.set_thumbnail(url=self.current.get("thumbnail", ""))

        embed.add_field(name="Loop", value=str(self.loop), inline=True)

        # เพลงถัดไปใน queue
        if self.queue:
            queue_text = ""
            for idx, song in enumerate(self.queue, 1):
                title = song.get("title", "Unknown title")
                queue_text += f"*{idx}. {title}*\n"  # แสดงชื่อแบบตัวเอียง (สีอ่อน)
            embed.add_field(name="🎧 Up Next", value=queue_text, inline=False)
        else:
            embed.add_field(name="🎧 Up Next", value="*No songs in queue.*", inline=False)

        # แก้ไขหรือส่ง embed ใหม่
        if self.message:
            await self.message.edit(embed=embed)
        else:
            self.message = await self.text_channel.send(embed=embed)
            for emoji in REACTS:
                await self.message.add_reaction(emoji)

async def ytdlp_search(query):
    ydl_opts = {"format": "bestaudio[abr<=96]", "quiet": True, "default_search": "auto", "noplaylist": False,"download": True}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))

def _extract(query, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
        return info['entries'] if "entries" in info else [info]

# ✅ Slash Command /play
@tree.command(name="play", description="Play a song or playlist in your voice channel")
async def slash_play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=False)
    vc = interaction.user.voice
    if not vc:
        return await interaction.followup.send("❌ You must be in a voice channel.", ephemeral=True)

    player = music_players.get(interaction.guild.id)
    if not player:
        player = MusicPlayer(interaction.guild)
        music_players[interaction.guild.id] = player

    if not player.voice_client or not player.voice_client.is_connected():
        player.voice_client = await vc.channel.connect()

    results = await ytdlp_search(query)
    for r in results:
        player.queue.append({
            "title": r["title"],
            "url": r["url"],
            "thumbnail": r.get("thumbnail", "")
        })

    player.text_channel = interaction.channel
    if not player.voice_client.is_playing() and not player.voice_client.is_paused():
        await player.play_next()
        msg = await interaction.followup.send(f"▶️ Playing: {results[0]['title']}", ephemeral=True)
        asyncio.sleep(5)
        await msg.delete()
    elif player.voice_client.is_paused():
        player.voice_client.resume()
        await interaction.followup.send("▶️ Resumed playback.", ephemeral=True)
    else:
        msg = await interaction.followup.send(f"🎶 Added to queue: {results[0]['title']}", ephemeral=True)
        asyncio.sleep(10)
        await msg.delete()
    
    # อัพเดต embed หลังจากเพิ่มเพลงเข้าคิว
    await player.send_embed()

# ✅ Reaction Event
@bot.event
async def on_reaction_add(reaction, user):
    if user.bot or reaction.message.author != bot.user:
        return
    for player in music_players.values():
        if player.message and reaction.message.id == player.message.id:
            action = REACTS.get(str(reaction.emoji))
            if action:
                await handle_action(action, player, user, reaction.message)
                try:
                    await reaction.message.remove_reaction(reaction.emoji, user)
                except discord.Forbidden:
                    pass  # ไม่มีสิทธิ์ลบ

# ✅ Reaction Handlers
async def handle_action(action, player, user, message):
    if action == "prev":
        if player.loop and not player.previous and player.queue:
            # ถ้าอยู่ในโหมด loop และไม่มีเพลงก่อนหน้า → เล่นเพลงสุดท้ายในคิว
            player.previous = player.current
            player.current = player.queue.pop()
        elif player.previous:
            # ถ้ามีเพลงก่อนหน้า → เล่นเพลงก่อนหน้า
            if player.current:
                player.queue.appendleft(player.current)
            player.current = player.previous
            player.previous = None
        else:
            # ไม่มีเพลงก่อนหน้าและไม่อยู่ในโหมด loop → ไม่ทำอะไร
            return  
            
        # หยุดและ reconnect voice client    
        if player.voice_client:
            await player.voice_client.disconnect()
        player.voice_client = await user.voice.channel.connect()   
        await player.play_next()
        
    elif action == "resume":
        if player.voice_client.is_paused():
            player.voice_client.resume()
            msg = await player.text_channel.send("▶️ Resumed playback.")
            await asyncio.sleep(5)
            await msg.delete()
        elif not player.voice_client.is_playing():
            await player.play_next()  # ถ้าไม่ได้ pause แต่ไม่มีเพลงเล่น → เล่นถัดไป
            
    elif action == "stop":
        if player.voice_client.is_playing():
            player.voice_client.pause()
            await player.send_embed()
            msg = await player.text_channel.send("⏹️ Stopped playback.")
            await asyncio.sleep(5)
            await msg.delete()
        else:
            msg = await player.text_channel.send("⏹️ No currently song playing.")
            await asyncio.sleep(5)
            await msg.delete()
            
    elif action == "skip":
        player.voice_client.stop()
        
    elif action == "loop":
        player.loop = not player.loop
        await player.send_embed()  # อัปเดต embed แสดง loop ใหม่
        
    elif action == "fav":
        msg = await player.text_channel.send(f"⭐ Favorite: {player.current['title']}")
        await asyncio.sleep(5)
        await msg.delete()
        
    elif action == "exit":
        await player.voice_client.disconnect()
        music_players.pop(player.guild.id, None)
        msg = await player.text_channel.send("❌ Bot exited voice channel.")
        await asyncio.sleep(5)
        await msg.delete()
        for guild in bot.guilds:
            for channel in guild.text_channels:
                if not channel.permissions_for(guild.me).read_message_history:
                    continue  # ข้ามถ้าไม่มีสิทธิ์อ่านประวัติ
                if not channel.permissions_for(guild.me).manage_messages:
                    continue  # ข้ามถ้าไม่มีสิทธิ์ลบข้อความ
                try:
                    async for msg in channel.history(limit=50):
                        if msg.author.id == bot.user.id:
                            await msg.delete()
                except discord.Forbidden:
                    print(f"❌ ไม่มีสิทธิ์ลบในห้อง {channel.name} ({channel.id})")
                except discord.HTTPException as e:
                    print(f"⚠️ ลบข้อความไม่สำเร็จ: {e}")

# ✅ Ready
@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ {bot.user} is ready and commands are synced.".encode('ascii', 'ignore').decode())
    print(f"Connected to {len(bot.guilds)} guilds.")
    
@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ {bot.user} is ready.".encode('ascii', 'ignore').decode())

    for guild in bot.guilds:
        for channel in guild.text_channels:
            if not channel.permissions_for(guild.me).read_message_history:
                continue  # ข้ามถ้าไม่มีสิทธิ์อ่านประวัติ
            if not channel.permissions_for(guild.me).manage_messages:
                continue  # ข้ามถ้าไม่มีสิทธิ์ลบข้อความ

            try:
                async for msg in channel.history(limit=50):
                    if msg.author.id == bot.user.id:
                        await msg.delete()
            except discord.Forbidden:
                print(f"❌ ไม่มีสิทธิ์ลบในห้อง {channel.name} ({channel.id})")
            except discord.HTTPException as e:
                print(f"⚠️ ลบข้อความไม่สำเร็จ: {e}")

# ✅ Run the bot
bot.run(TOKEN)

# 1.กดเล่นเพลงก่อนหน้ายังไม่ใส่เงื่อนไข
# 2.กดเล่นเพลงถัดไปเล่นได้
# 3.กดหยุด กดเล่นเล่นได้
# 4.Need to unsent msg after exit stop no song 
# 5.Need to test loop system and fav
# 6.Make condition for checking avilable song in queue and current song
# 7.fix some bug when play next song    