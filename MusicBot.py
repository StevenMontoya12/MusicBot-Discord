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
import imageio_ffmpeg as ffmpeg

from datetime import timedelta

# =========================
# CARGA DE CONFIGURACIÓN
# =========================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

# Opcional para Last.fm (para /lastfm)
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")   # Recomendado
LASTFM_USER = os.getenv("LASTFM_USER")         # Recomendado

# Ruta estática a FFmpeg (ajústala a tu sistema)
FFMPEG_PATH = ffmpeg.get_ffmpeg_exe()

# Aviso si la ruta no existe (no bloquea la ejecución)
if not os.path.isfile(FFMPEG_PATH):
    print(f"[AVISO] No se encontró FFmpeg en: {FFMPEG_PATH}. Verifica la ruta o ponlo en el PATH.")

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
# Cola por servidor: deque de dicts con metadata completa de la pista
# { url, title, webpage_url, duration, thumbnail, artist }
SONG_QUEUES: dict[str, deque] = {}

# Modo de loop por servidor: "off" | "one" | "all"
LOOP_MODE: dict[str, str] = {}

# Volumen actual por servidor (0.0 - 1.0)
VOLUME: dict[str, float] = {}

# Canción actual por servidor (dict con la misma forma que los items de la cola)
CURRENT_SONG: dict[str, dict] = {}

# =========================
# BOT
# =========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# UTILIDADES
# =========================
async def search_ytdlp_async(query, ydl_opts):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))

def _extract(query, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(query, download=False)

def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "Desconocido"
    try:
        seconds = int(seconds)
        return str(timedelta(seconds=seconds))
    except Exception:
        return "Desconocido"

def make_queue_item(from_info: dict) -> dict:
    """Convierte la info cruda de yt_dlp en un objeto estándar para la cola."""
    return {
        "url": from_info.get("url"),
        "title": from_info.get("title", "Sin título"),
        "webpage_url": from_info.get("webpage_url", from_info.get("original_url", from_info.get("url", ""))),
        "duration": from_info.get("duration"),
        "thumbnail": from_info.get("thumbnail", ""),
        "artist": from_info.get("uploader") or from_info.get("artist") or "Desconocido",
    }

def get_spotify_playlist_tracks(playlist_id: str):
    tracks = []
    response = spotify.playlist_items(playlist_id, additional_types=['track'], limit=100)
    tracks.extend(response['items'])
    while response['next']:
        response = spotify.next(response)
        tracks.extend(response['items'])
    return tracks

# =========================
# EVENTOS
# =========================
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"{bot.user} is online!")

# =========================
# REPRODUCCIÓN
# =========================
async def start_playback(vc: discord.VoiceClient, guild_id: str, channel: discord.abc.Messageable, start_seconds: int = 0):
    """
    Inicia/reinicia la reproducción de la canción actual (CURRENT_SONG[guild_id])
    con soporte de 'after' para encadenar a la siguiente canción y respetar el loop.
    """
    current = CURRENT_SONG.get(guild_id)
    if not current:
        # Si no hay actual, intenta tomar de la cola
        if SONG_QUEUES.get(guild_id):
            CURRENT_SONG[guild_id] = SONG_QUEUES[guild_id][0]
            current = CURRENT_SONG[guild_id]
        else:
            # Nada que reproducir
            try:
                if vc:
                    await vc.disconnect()
            except Exception:
                pass
            SONG_QUEUES[guild_id] = deque()
            CURRENT_SONG.pop(guild_id, None)
            VOLUME.pop(guild_id, None)
            LOOP_MODE[guild_id] = "off"
            return

    url = current["url"]
    volume = VOLUME.get(guild_id, 0.5)

    before = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    if start_seconds and start_seconds > 0:
        before = f"-ss {int(start_seconds)} " + before

    def after(error):
        if error:
            print(f"[after] Error en reproducción: {error}")

        # Al finalizar, mover cola según loop
        mode = LOOP_MODE.get(guild_id, "off")
        try:
            if mode == "off":
                # Sacar la actual
                if SONG_QUEUES.get(guild_id):
                    try:
                        # Si la actual coincide con el primer elemento, popleft.
                        if SONG_QUEUES[guild_id] and SONG_QUEUES[guild_id][0] is current:
                            SONG_QUEUES[guild_id].popleft()
                        else:
                            # Si no coincide, aún así intentamos remover por igualdad.
                            if current in SONG_QUEUES[guild_id]:
                                SONG_QUEUES[guild_id].remove(current)
                    except Exception:
                        pass
            elif mode == "all":
                # Rotar (mover la primera al final)
                if SONG_QUEUES.get(guild_id) and len(SONG_QUEUES[guild_id]) > 0:
                    SONG_QUEUES[guild_id].rotate(-1)
            elif mode == "one":
                # No mover la cola; dejamos la misma canción en cabeza
                pass
        except Exception as e:
            print(f"[after] Error gestionando loop/cola: {e}")

        # Llamar recursivo a play_next
        fut = asyncio.run_coroutine_threadsafe(play_next(vc, guild_id, channel), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"[after] Error al continuar reproducción: {e}")

    # Construir la fuente de audio y reproducir
    try:
        # 🔧 Cambio clave: options="-vn" (sin forzar libopus)
        source = discord.FFmpegPCMAudio(
            url,
            executable=FFMPEG_PATH,
            before_options=before,
            options="-vn"
        )
        vc.play(discord.PCMVolumeTransformer(source, volume=volume), after=after)
    except Exception as e:
        print(f"[start_playback] Error al iniciar reproducción: {e}")
        # Intentar saltar a la siguiente
        fut = asyncio.run_coroutine_threadsafe(play_next(vc, guild_id, channel), bot.loop)
        try:
            fut.result()
        except Exception as er:
            print(f"[start_playback] Error al encadenar: {er}")
        return

    # Enviar Embed "Ahora suena"
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
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[start_playback] Error al enviar embed: {e}")

async def play_next(vc: discord.VoiceClient, guild_id: str, channel: discord.abc.Messageable):
    """
    Obtiene el siguiente elemento que debería sonar (según el loop) y llama a start_playback.
    """
    # Si la cola está vacía, desconectarse y limpiar
    if not SONG_QUEUES.get(guild_id) or len(SONG_QUEUES[guild_id]) == 0:
        try:
            if vc:
                await vc.disconnect()
        except Exception:
            pass
        SONG_QUEUES[guild_id] = deque()
        CURRENT_SONG.pop(guild_id, None)
        VOLUME.pop(guild_id, None)
        LOOP_MODE[guild_id] = "off"
        return

    # La canción "actual" debe ser el primer elemento de la cola
    CURRENT_SONG[guild_id] = SONG_QUEUES[guild_id][0]
    # Iniciar reproducción desde 0s
    await start_playback(vc, guild_id, channel, start_seconds=0)

# =========================
# COMANDOS
# =========================
@bot.tree.command(name="play", description="Play a song, playlist, or add to the queue.")
@app_commands.describe(song_query="Search term or URL from YouTube, Spotify, or SoundCloud")
async def play(interaction: discord.Interaction, song_query: str):
    await interaction.response.defer()

    # Conectar/encaminar al canal de voz
    if interaction.guild.voice_client is None:
        if interaction.user.voice is None:
            await interaction.followup.send("¡Debes estar en un canal de voz para reproducir música!")
            return
        vc = await interaction.user.voice.channel.connect()
    else:
        vc = interaction.guild.voice_client
        if interaction.user.voice and vc.channel != interaction.user.voice.channel:
            await vc.move_to(interaction.user.voice.channel)

    ydl_options = {
        "format": "bestaudio[abr<=96]/bestaudio",
        "noplaylist": True,
        "cookiefile": "cookies.txt"  # archivo que subiste a la raíz de tu proyecto
    }


    guild_id = str(interaction.guild_id)
    SONG_QUEUES.setdefault(guild_id, deque())
    LOOP_MODE.setdefault(guild_id, "off")
    VOLUME.setdefault(guild_id, 0.5)

    # Soporte Spotify (track o playlist)
    if "open.spotify.com" in song_query:
        # Track individual
        if "/track/" in song_query:
            track_id = re.search(r"track/([a-zA-Z0-9]+)", song_query)
            if track_id:
                try:
                    track = spotify.track(track_id.group(1))
                    query = f"{track['name']} {track['artists'][0]['name']}"
                    # Búsqueda en YouTube con ese query
                    results = await search_ytdlp_async(f"ytsearch1:{query}", ydl_options)
                    if not results:
                        await interaction.followup.send(f"No se encontró resultado para: {query}")
                        return
                    first = results['entries'][0] if 'entries' in results else results
                    item = make_queue_item(first)
                    SONG_QUEUES[guild_id].append(item)
                    await interaction.followup.send(f"✅ Añadido a la cola: **{item['title']}**")
                except Exception as e:
                    await interaction.followup.send(f"Error con Spotify track: {e}")
                    return

        # Playlist completa
        elif "/playlist/" in song_query:
            playlist_id = re.search(r"playlist/([a-zA-Z0-9]+)", song_query)
            if playlist_id:
                try:
                    items = get_spotify_playlist_tracks(playlist_id.group(1))
                    added = 0
                    for entry in items:
                        track = entry.get('track')
                        if not track:
                            continue
                        query = f"{track['name']} {track['artists'][0]['name']}"
                        try:
                            results = await search_ytdlp_async(f"ytsearch1:{query}", ydl_options)
                            if not results:
                                continue
                            first = results['entries'][0] if 'entries' in results else results
                            SONG_QUEUES[guild_id].append(make_queue_item(first))
                            added += 1
                        except Exception:
                            continue
                    await interaction.followup.send(f"✅ Añadidas {added} canciones desde la playlist de Spotify.")
                except Exception as e:
                    await interaction.followup.send(f"Error al procesar la playlist de Spotify: {e}")
                    return

        # Si era Spotify, empezamos a reproducir si no hay nada sonando
        vc = interaction.guild.voice_client
        if vc and not vc.is_playing():
            await play_next(vc, guild_id, interaction.channel)
        return

    # Si no es Spotify: búsqueda o URL directa
    try:
        search_q = f"ytsearch1:{song_query}" if not song_query.startswith("http") else song_query
        results = await search_ytdlp_async(search_q, ydl_options)
    except Exception as e:
        await interaction.followup.send(f"Error while searching: {str(e)}")
        return

    if not results:
        await interaction.followup.send(f"No results for: {song_query}")
        return

    first = results['entries'][0] if 'entries' in results else results
    item = make_queue_item(first)
    SONG_QUEUES[guild_id].append(item)
    await interaction.followup.send(f"{'Reproduciendo ahora' if not vc.is_playing() else '✅ Añadido a la cola'}: **{item['title']}**")

    if not vc.is_playing():
        await play_next(vc, guild_id, interaction.channel)

@bot.tree.command(name="nowplaying", description="Muestra info detallada de la canción actual")
async def nowplaying_cmd(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    current = CURRENT_SONG.get(guild_id)
    if not current:
        await interaction.response.send_message("No hay canción sonando ahora.")
        return

    embed = discord.Embed(
        title="🎶 Ahora sonando",
        description=f"[{current['title']}]({current['webpage_url']})",
        color=0x00ccff
    )
    embed.add_field(name="Artista", value=current.get("artist", "Desconocido"), inline=True)
    embed.add_field(name="Duración", value=format_duration(current.get("duration")), inline=True)
    if current.get("thumbnail"):
        embed.set_thumbnail(url=current["thumbnail"])
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="seek", description="Avanza o retrocede en la canción actual a un tiempo (segundos)")
@app_commands.describe(seconds="Tiempo en segundos al que deseas ir (ej. 90 para 1:30)")
async def seek_cmd(interaction: discord.Interaction, seconds: int):
    vc = interaction.guild.voice_client
    guild_id = str(interaction.guild_id)
    current = CURRENT_SONG.get(guild_id)

    if not vc or not current:
        await interaction.response.send_message("No hay canción en reproducción.")
        return

    # Reiniciar la reproducción en el offset solicitado
    try:
        vc.stop()
    except Exception:
        pass

    await start_playback(vc, guild_id, interaction.channel, start_seconds=max(0, int(seconds)))
    await interaction.response.send_message(f"⏩ Saltado a {format_duration(seconds)}")

@bot.tree.command(name="loop", description="Configura el modo de repetición (off/one/all)")
@app_commands.describe(mode="off/one/all")
async def loop_cmd(interaction: discord.Interaction, mode: str):
    if mode not in ["off", "one", "all"]:
        await interaction.response.send_message("Opciones válidas: `off`, `one`, `all`")
        return
    LOOP_MODE[str(interaction.guild_id)] = mode
    icon = {"off": "❌", "one": "🔂", "all": "🔁"}[mode]
    await interaction.response.send_message(f"{icon} Loop configurado en **{mode}**")

@bot.tree.command(name="skip", description="Salta la canción actual")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()  # Dispara el after y encadena
        await interaction.response.send_message("⏭️ Saltado")
    else:
        await interaction.response.send_message("No hay nada sonando.")

@bot.tree.command(name="pause", description="Pausa la reproducción")
async def pause_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Pausado")
    else:
        await interaction.response.send_message("No hay nada sonando.")

@bot.tree.command(name="resume", description="Reanuda la reproducción")
async def resume_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Reanudado")
    else:
        await interaction.response.send_message("No hay nada pausado.")

@bot.tree.command(name="stop", description="Detiene la reproducción, limpia la cola y desconecta")
async def stop_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    gid = str(interaction.guild_id)
    if vc:
        SONG_QUEUES.pop(gid, None)
        CURRENT_SONG.pop(gid, None)
        VOLUME.pop(gid, None)
        LOOP_MODE.pop(gid, None)
        try:
            vc.stop()
        except Exception:
            pass
        await vc.disconnect()
        await interaction.response.send_message("🛑 Detenido y desconectado")
    else:
        await interaction.response.send_message("No estoy conectado.")

@bot.tree.command(name="queue", description="Muestra la cola de canciones actuales")
async def queue_cmd(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    queue = SONG_QUEUES.get(guild_id, deque())
    if not queue:
        await interaction.response.send_message("La cola está vacía.")
        return

    message = "**Cola de canciones:**\n"
    for i, item in enumerate(queue):
        title = item.get("title", "Sin título")
        artist = item.get("artist", "Desconocido")
        message += f"{i + 1}. {title} — {artist}\n"

    # Partir si excede 2000 chars
    if len(message) > 2000:
        chunks = [message[i:i+2000] for i in range(0, len(message), 2000)]
        await interaction.response.send_message(chunks[0])
        for chunk in chunks[1:]:
            await interaction.channel.send(chunk)
    else:
        await interaction.response.send_message(message)

@bot.tree.command(name="clearqueue", description="Limpia la cola de canciones")
async def clearqueue_cmd(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if guild_id in SONG_QUEUES:
        SONG_QUEUES[guild_id].clear()
    await interaction.response.send_message("Cola limpiada ✅")

@bot.tree.command(name="volume", description="Ajusta el volumen. Rango: 1-100")
@app_commands.describe(level="Nivel de volumen entre 1 y 100")
async def volume_cmd(interaction: discord.Interaction, level: int):
    if level < 1 or level > 100:
        await interaction.response.send_message("Por favor ingresa un valor entre 1 y 100")
        return

    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("No estoy conectado a un canal de voz")
        return

    guild_id = str(interaction.guild_id)
    vol = level / 100
    VOLUME[guild_id] = vol

    # Cambiar volumen de la fuente actual
    if vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = vol

    await interaction.response.send_message(f"🔊 Volumen ajustado a {level}%")

# ====== LETRAS CON PAGINACIÓN ======
class LyricsView(ui.View):
    def __init__(self, lyrics_chunks):
        super().__init__(timeout=180)
        self.lyrics_chunks = lyrics_chunks
        self.current_page = 0

    async def update_message(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=f"```{self.lyrics_chunks[self.current_page]}```", view=self)

    @ui.button(label="Anterior", style=discord.ButtonStyle.primary)
    async def previous(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await self.update_message(interaction)
        else:
            await interaction.response.defer()

    @ui.button(label="Siguiente", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page < len(self.lyrics_chunks) - 1:
            self.current_page += 1
            await self.update_message(interaction)
        else:
            await interaction.response.defer()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        # Puedes editar el mensaje para deshabilitar los botones si lo deseas

@bot.tree.command(name="letra", description="Busca la letra de una canción.")
@app_commands.describe(artista="Nombre del artista", cancion="Título de la canción")
async def letra(interaction: discord.Interaction, artista: str, cancion: str):
    await interaction.response.defer()

    artist_url = artista.replace(" ", "%20")
    title_url = cancion.replace(" ", "%20")

    url = f"https://api.lyrics.ovh/v1/{artist_url}/{title_url}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    lyrics = data.get("lyrics", "Letra no encontrada.")
                else:
                    lyrics = "No encontré la letra. Asegúrate de que el nombre sea correcto."
    except Exception:
        lyrics = "Hubo un error al consultar la API de letras."

    # Dividir letra en trozos de máximo ~1800 caracteres
    max_chunk_len = 1800
    lyrics_chunks = []
    text = lyrics
    while len(text) > max_chunk_len:
        cut_index = text.rfind('\n', 0, max_chunk_len)
        if cut_index == -1:
            cut_index = max_chunk_len
        lyrics_chunks.append(text[:cut_index])
        text = text[cut_index:]
    lyrics_chunks.append(text)

    header = f"**Letra de {cancion.title()} - {artista.title()}**:\n"

    # Enviar con paginación si hay varias páginas
    if len(lyrics_chunks) > 1:
        view = LyricsView(lyrics_chunks)
        await interaction.followup.send(content=header + f"```{lyrics_chunks[0]}```", view=view)
    else:
        await interaction.followup.send(content=header + f"```{lyrics_chunks[0]}```")

# ====== LAST.FM INTEGRACIÓN ======
@bot.tree.command(name="lastfm", description="Muestra tu última canción escuchada en Last.fm")
async def lastfm_cmd(interaction: discord.Interaction):
    if not LASTFM_API_KEY or not LASTFM_USER:
        await interaction.response.send_message("Configura LASTFM_API_KEY y LASTFM_USER en tu .env para usar este comando.")
        return

    try:
        url = (
            "http://ws.audioscrobbler.com/2.0/"
            f"?method=user.getrecenttracks&user={LASTFM_USER}"
            f"&api_key={LASTFM_API_KEY}&format=json&limit=1"
        )
        resp = requests.get(url, timeout=10).json()
        track = resp["recenttracks"]["track"][0]
        title = track.get("name", "Desconocido")
        artist = track.get("artist", {}).get("#text", "Desconocido")
        img_list = track.get("image", [])
        img = img_list[-1]["#text"] if img_list else ""

        embed = discord.Embed(
            title="🎧 Última canción en Last.fm",
            description=f"**{title}** — {artist}",
            color=0xff0000
        )
        if img:
            embed.set_thumbnail(url=img)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"No se pudo consultar Last.fm: {e}")

# ====== HELP ======
@bot.tree.command(name="help", description="Muestra todos los comandos disponibles")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="📜 Comandos de Música", color=discord.Color.blue())
    embed.add_field(name="/play <canción/url>", value="Reproduce una canción o añade a la cola (YouTube/Spotify).", inline=False)
    embed.add_field(name="/skip", value="Salta la canción actual.", inline=False)
    embed.add_field(name="/pause", value="Pausa la canción que se está reproduciendo.", inline=False)
    embed.add_field(name="/resume", value="Reanuda la canción pausada.", inline=False)
    embed.add_field(name="/stop", value="Detiene la reproducción, limpia la cola y desconecta.", inline=False)
    embed.add_field(name="/loop <off/one/all>", value="Configura el modo repetición: sin loop, repetir una, o repetir toda la cola.", inline=False)
    embed.add_field(name="/queue", value="Muestra la cola de canciones (se divide si es muy larga).", inline=False)
    embed.add_field(name="/clearqueue", value="Limpia la cola de canciones.", inline=False)
    embed.add_field(name="/volume <1-100>", value="Ajusta el volumen de la música.", inline=False)
    embed.add_field(name="/nowplaying", value="Muestra info detallada de la canción actual (título, artista, duración y miniatura).", inline=False)
    embed.add_field(name="/seek <segundos>", value="Avanza o retrocede a un tiempo específico de la canción actual.", inline=False)
    embed.add_field(name="/letra <artista> <canción>", value="Busca la letra con paginación.", inline=False)
    embed.add_field(name="/lastfm", value="Muestra tu última canción escuchada en Last.fm.", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)



from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 10000))

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot corriendo")

def start_dummy_server():
    server = HTTPServer(("0.0.0.0", PORT), DummyHandler)
    server.serve_forever()

Thread(target=start_dummy_server, daemon=True).start()
print(f"Bot corriendo en Render (dummy port {PORT})")

# Luego tu bot
bot.run(TOKEN)

