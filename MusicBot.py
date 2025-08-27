import os
import discord
from discord.ext import commands
from discord import app_commands, ui
from dotenv import load_dotenv
import yt_dlp
from collections import deque
import asyncio
import re
from spotipy import Spotify
from spotipy.oauth2 import SpotifyClientCredentials
import aiohttp
import requests
from datetime import timedelta
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

# =========================
# CARGA DE CONFIGURACIÓN
# =========================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
LASTFM_USER = os.getenv("LASTFM_USER")
FFMPEG_PATH = "ffmpeg"

if not os.path.isfile(FFMPEG_PATH):
    print(f"[DEBUG][AVISO] No se encontró FFmpeg en: {FFMPEG_PATH}")

# =========================
# CLIENTES EXTERNOS
# =========================
spotify = Spotify(auth_manager=SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET
))

# =========================
# ESTRUCTURAS DE ESTADO
# =========================
SONG_QUEUES: dict[str, deque] = {}
LOOP_MODE: dict[str, str] = {}
VOLUME: dict[str, float] = {}
CURRENT_SONG: dict[str, dict] = {}

# =========================
# BOT
# =========================
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# UTILIDADES
# =========================
async def search_ytdlp_async(query, ydl_opts):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))

def _extract(query, ydl_opts):
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            print(f"[DEBUG] yt_dlp info encontrada para query: {query}")
            return info
    except Exception as e:
        print(f"[ERROR][yt_dlp] {e}")
        return None

def format_duration(seconds):
    if seconds is None:
        return "Desconocido"
    try:
        return str(timedelta(seconds=int(seconds)))
    except Exception as e:
        print(f"[ERROR][format_duration] {e}")
        return "Desconocido"

def make_queue_item(from_info: dict):
    return {
        "url": from_info.get("url"),
        "title": from_info.get("title", "Sin título"),
        "webpage_url": from_info.get("webpage_url", from_info.get("original_url", from_info.get("url", ""))),
        "duration": from_info.get("duration"),
        "thumbnail": from_info.get("thumbnail", ""),
        "artist": from_info.get("uploader") or from_info.get("artist") or "Desconocido",
    }

def get_spotify_playlist_tracks(playlist_id: str):
    try:
        tracks = []
        response = spotify.playlist_items(playlist_id, additional_types=['track'], limit=100)
        tracks.extend(response['items'])
        while response['next']:
            response = spotify.next(response)
            tracks.extend(response['items'])
        print(f"[DEBUG] Spotify playlist tracks obtenidas: {len(tracks)}")
        return tracks
    except Exception as e:
        print(f"[ERROR][Spotify playlist] {e}")
        return []

# =========================
# EVENTOS
# =========================
@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
        print(f"[DEBUG] {bot.user} is online! Commands synced")
    except Exception as e:
        print(f"[ERROR][on_ready] {e}")

# =========================
# REPRODUCCIÓN
# =========================
async def start_playback(vc, guild_id, channel, start_seconds=0):
    current = CURRENT_SONG.get(guild_id)
    if not current:
        if SONG_QUEUES.get(guild_id):
            CURRENT_SONG[guild_id] = SONG_QUEUES[guild_id][0]
            current = CURRENT_SONG[guild_id]
        else:
            try:
                if vc:
                    await vc.disconnect()
            except Exception:
                pass
            SONG_QUEUES[guild_id] = deque()
            CURRENT_SONG.pop(guild_id, None)
            VOLUME.pop(guild_id, None)
            LOOP_MODE[guild_id] = "off"
            print(f"[DEBUG][start_playback] Nada que reproducir en guild {guild_id}")
            return

    url = current["url"]
    volume = VOLUME.get(guild_id, 0.5)
    before = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    if start_seconds > 0:
        before = f"-ss {int(start_seconds)} " + before

    def after(error):
        if error:
            print(f"[ERROR][after playback] {error}")
        fut = asyncio.run_coroutine_threadsafe(play_next(vc, guild_id, channel), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"[ERROR][after playback future] {e}")

    try:
        source = discord.FFmpegPCMAudio(
            url,
            executable=FFMPEG_PATH,
            before_options=before,
            options="-vn"
        )
        vc.play(discord.PCMVolumeTransformer(source, volume=volume), after=after)
        print(f"[DEBUG][start_playback] Reproduciendo: {current['title']} en guild {guild_id}")
    except Exception as e:
        print(f"[ERROR][start_playback] {e}")

    # Embed Now Playing
    try:
        embed = discord.Embed(
            title="🎵 Ahora suena",
            description=f"[{current['title']}]({current['webpage_url']})",
            color=0x1DB954
        )
        embed.add_field(name="Artista", value=current["artist"], inline=True)
        embed.add_field(name="Duración", value=format_duration(current["duration"]), inline=True)
        if current.get("thumbnail"):
            embed.set_thumbnail(url=current["thumbnail"])
        asyncio.create_task(channel.send(embed=embed))
    except Exception as e:
        print(f"[ERROR][start_playback embed] {e}")

async def play_next(vc, guild_id, channel):
    if not SONG_QUEUES.get(guild_id):
        try:
            if vc:
                await vc.disconnect()
        except Exception:
            pass
        CURRENT_SONG.pop(guild_id, None)
        VOLUME.pop(guild_id, None)
        LOOP_MODE[guild_id] = "off"
        print(f"[DEBUG][play_next] Cola vacía en guild {guild_id}")
        return

    CURRENT_SONG[guild_id] = SONG_QUEUES[guild_id][0]
    await start_playback(vc, guild_id, channel, start_seconds=0)

# =========================
# COMANDOS
# =========================
# ----- PLAY -----
@bot.tree.command(name="play", description="Play a song, playlist, or add to the queue.")
@app_commands.describe(song_query="Search term or URL from YouTube, Spotify, or SoundCloud")
async def play_cmd(interaction: discord.Interaction, song_query: str):
    print(f"[DEBUG] /play invoked by {interaction.user} with query: {song_query}")
    await interaction.response.defer()
    guild_id = str(interaction.guild_id)
    SONG_QUEUES.setdefault(guild_id, deque())
    LOOP_MODE.setdefault(guild_id, "off")
    VOLUME.setdefault(guild_id, 0.5)

    try:
        if interaction.guild.voice_client is None:
            if interaction.user.voice is None:
                await interaction.followup.send("¡Debes estar en un canal de voz!")
                return
            vc = await interaction.user.voice.channel.connect()
        else:
            vc = interaction.guild.voice_client
            if interaction.user.voice and vc.channel != interaction.user.voice.channel:
                await vc.move_to(interaction.user.voice.channel)
    except Exception as e:
        await interaction.followup.send(f"[ERROR][voice connect] {e}")
        print(f"[ERROR][voice connect] {e}")
        return

    try:
        # Soporte Spotify
        if "open.spotify.com" in song_query:
            # Track
            if "/track/" in song_query:
                track_id = re.search(r"track/([a-zA-Z0-9]+)", song_query)
                if track_id:
                    track = spotify.track(track_id.group(1))
                    query = f"{track['name']} {track['artists'][0]['name']}"
                    print(f"[DEBUG][Spotify Track] query: {query}")
                    song_query = query
            # Playlist
            elif "/playlist/" in song_query:
                playlist_id = re.search(r"playlist/([a-zA-Z0-9]+)", song_query)
                tracks = get_spotify_playlist_tracks(playlist_id.group(1))
                for t in tracks:
                    q = f"{t['track']['name']} {t['track']['artists'][0]['name']}"
                    ydl_opts = {"format": "bestaudio[abr<=96]/bestaudio", "noplaylist": True}
                    info = await search_ytdlp_async(f"ytsearch1:{q}", ydl_opts)
                    if info and 'entries' in info:
                        SONG_QUEUES[guild_id].append(make_queue_item(info['entries'][0]))
                await interaction.followup.send(f"✅ Playlist agregada: {len(tracks)} canciones")
                if not vc.is_playing():
                    await play_next(vc, guild_id, interaction.channel)
                return

        ydl_opts = {"format": "bestaudio[abr<=96]/bestaudio", "noplaylist": True}
        search_q = f"ytsearch1:{song_query}" if not song_query.startswith("http") else song_query
        results = await search_ytdlp_async(search_q, ydl_opts)
        if not results:
            await interaction.followup.send(f"No se encontró: {song_query}")
            print(f"[DEBUG][play] No results for query: {song_query}")
            return
        first = results['entries'][0] if 'entries' in results else results
        item = make_queue_item(first)
        SONG_QUEUES[guild_id].append(item)
        await interaction.followup.send(f"✅ Añadido: {item['title']}")
        print(f"[DEBUG][play] Añadido a cola: {item['title']}")
        if not vc.is_playing():
            await play_next(vc, guild_id, interaction.channel)
    except Exception as e:
        await interaction.followup.send(f"[ERROR][play] {e}")
        print(f"[ERROR][play] {e}")

# ----- SKIP -----
@bot.tree.command(name="skip", description="Skip current song")
async def skip_cmd(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ Canción saltada")
    else:
        await interaction.response.send_message("No se está reproduciendo nada")
    print(f"[DEBUG][/skip] invoked by {interaction.user}")

# ----- PAUSE -----
@bot.tree.command(name="pause", description="Pause the music")
async def pause_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Pausa activada")
    else:
        await interaction.response.send_message("No hay reproducción activa")
    print(f"[DEBUG][/pause] invoked by {interaction.user}")

# ----- RESUME -----
@bot.tree.command(name="resume", description="Resume the music")
async def resume_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Reanudado")
    else:
        await interaction.response.send_message("No hay reproducción en pausa")
    print(f"[DEBUG][/resume] invoked by {interaction.user}")

# ----- STOP -----
@bot.tree.command(name="stop", description="Stop the music and clear queue")
async def stop_cmd(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()
    SONG_QUEUES[guild_id] = deque()
    CURRENT_SONG.pop(guild_id, None)
    LOOP_MODE[guild_id] = "off"
    await interaction.response.send_message("⏹️ Reproducción detenida y cola vaciada")
    print(f"[DEBUG][/stop] invoked by {interaction.user}")

# ----- QUEUE -----
@bot.tree.command(name="queue", description="Show the song queue")
async def queue_cmd(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    queue = SONG_QUEUES.get(guild_id, deque())
    if not queue:
        await interaction.response.send_message("La cola está vacía")
        return
    desc = ""
    for i, song in enumerate(queue, 1):
        desc += f"{i}. {song['title']} - {song['artist']} [{format_duration(song['duration'])}]\n"
    embed = discord.Embed(title="🎶 Cola de reproducción", description=desc, color=0x1DB954)
    await interaction.response.send_message(embed=embed)
    print(f"[DEBUG][/queue] invoked by {interaction.user}")

# ----- CLEARQUEUE -----
@bot.tree.command(name="clearqueue", description="Clear the song queue")
async def clearqueue_cmd(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    SONG_QUEUES[guild_id] = deque()
    await interaction.response.send_message("🗑️ Cola vaciada")
    print(f"[DEBUG][/clearqueue] invoked by {interaction.user}")

# ----- VOLUME -----
@bot.tree.command(name="volume", description="Set or check volume")
@app_commands.describe(level="Volume level 0-100")
async def volume_cmd(interaction: discord.Interaction, level: int = None):
    guild_id = str(interaction.guild_id)
    vc = interaction.guild.voice_client
    if vc is None:
        await interaction.response.send_message("No hay reproducción activa")
        return
    if level is None:
        vol = VOLUME.get(guild_id, 50)
        await interaction.response.send_message(f"🔊 Volumen actual: {vol}%")
    else:
        VOLUME[guild_id] = max(0, min(level/100, 1))
        if vc.source:
            vc.source.volume = VOLUME[guild_id]
        await interaction.response.send_message(f"🔊 Volumen ajustado a: {level}%")
    print(f"[DEBUG][/volume] invoked by {interaction.user}, level={level}")

# ----- NOWPLAYING -----
@bot.tree.command(name="nowplaying", description="Show currently playing song")
async def nowplaying_cmd(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    current = CURRENT_SONG.get(guild_id)
    if not current:
        await interaction.response.send_message("No se está reproduciendo nada")
        return
    embed = discord.Embed(title="🎵 Ahora suena", description=f"[{current['title']}]({current['webpage_url']})", color=0x1DB954)
    embed.add_field(name="Artista", value=current["artist"], inline=True)
    embed.add_field(name="Duración", value=format_duration(current["duration"]), inline=True)
    if current.get("thumbnail"):
        embed.set_thumbnail(url=current["thumbnail"])
    await interaction.response.send_message(embed=embed)
    print(f"[DEBUG][/nowplaying] invoked by {interaction.user}")

# =========================
# DUMMY SERVER PARA RENDER
# =========================
PORT = int(os.environ.get("PORT", 10000))
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot corriendo")

def start_dummy_server():
    server = HTTPServer(("0.0.0.0", PORT), DummyHandler)
    print(f"[DEBUG] Dummy server corriendo en puerto {PORT}")
    server.serve_forever()

Thread(target=start_dummy_server, daemon=True).start()

# =========================
# RUN BOT
# =========================
try:
    bot.run(TOKEN)
except Exception as e:
    print(f"[ERROR][bot.run] {e}")
