import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
import os
import logging
import aiohttp
import time
from io import BytesIO
from PIL import Image, ImageOps, ImageFilter, ImageEnhance
import feedparser

# --------- CONFIGURATION ---------
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

TWITCH_CLIENT_ID = os.getenv('TWITCH_CLIENT_ID')
TWITCH_CLIENT_SECRET = os.getenv('TWITCH_CLIENT_SECRET')
TWITCH_ALERT_CHANNEL_ID = int(os.getenv('CHANNEL_ID', '0'))

# YouTube channels mapping: "name": "channel_id"
YOUTUBE_CHANNELS = {
    "channel": os.getenv('YOUTUBE_CHANNEL_ID'),
}

YOUTUBE_LONG_CHANNEL_ID = int(os.getenv('YOUTUBE_LONG_CHANNEL_ID', '0'))
YOUTUBE_SHORT_CHANNEL_ID = int(os.getenv('YOUTUBE_SHORT_CHANNEL_ID', '0'))

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --------- TWITCH ---------
twitch_token = None
twitch_token_expiry = 0
last_live_status = False

async def get_twitch_token():
    global twitch_token, twitch_token_expiry
    now = time.time()
    if twitch_token and now < twitch_token_expiry:
        return twitch_token

    url = "https://id.twitch.tv/oauth2/token"
    params = {
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, params=params) as resp:
            data = await resp.json()
            twitch_token = data["access_token"]
            twitch_token_expiry = now + data.get("expires_in", 3600) - 60
            print("🔑 Nouveau token Twitch obtenu.")
            return twitch_token

async def is_twitch_live(channel_name: str):
    token = await get_twitch_token()
    headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.twitch.tv/helix/streams?user_login={channel_name}", headers=headers) as resp:
            data = await resp.json()
            if "data" in data and len(data["data"]) > 0:
                return data["data"][0]
            return None

@tasks.loop(seconds=15)
async def check_twitch_live():
    global last_live_status
    tag = "@everyone"
    channel_name = "channel"
    live_data = await is_twitch_live(channel_name)
    channel = bot.get_channel(TWITCH_ALERT_CHANNEL_ID)

    if live_data and not last_live_status:
        last_live_status = True
        url = f"https://twitch.tv/{channel_name}"
        if channel:
            await channel.send(f"\n{channel_name} is streaming ! Come now 👇\n{url}\n{tag}")
            print(f"🔔 {channel_name} in direct !")

    elif not live_data and last_live_status:
        last_live_status = False
        print(f"❌ {channel_name} not in direct.")

# --------- YOUTUBE via RSS ---------
last_youtube_videos = {}

@tasks.loop(seconds=60)
async def check_youtube_rss():
    global last_youtube_videos

    for name, channel_id in YOUTUBE_CHANNELS.items():
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        feed = feedparser.parse(feed_url)
        if not feed.entries:
            continue

        latest = feed.entries[0]
        video_id = latest.yt_videoid
        title = latest.title
        url = latest.link
        author = latest.author

        if last_youtube_videos.get(name) == video_id:
            continue

        last_youtube_videos[name] = video_id

        # Détection simple des Shorts
        if "/shorts/" in url.lower() or "short" in title.lower():
            channel = bot.get_channel(YOUTUBE_SHORT_CHANNEL_ID)
            msg = f"📱 **{author}** published a new **Short** !\n{url}"
        else:
            channel = bot.get_channel(YOUTUBE_LONG_CHANNEL_ID)
            msg = f"🎬 **{author}** published a **new video** !\n{url}"

        if channel:
            await channel.send(msg)
            print(f"🔔 New vid detected : {author} — {title}")

# --------- FILTRES IMAGE / COMMANDE /pp ---------
def apply_filter(img: Image.Image, effect: str) -> Image.Image:
    effect = (effect or "").lower()
    has_alpha = img.mode in ("RGBA", "LA") or ("transparency" in img.info)
    if has_alpha:
        alpha = img.split()[-1]
        rgb = img.convert("RGB")
    else:
        alpha = None
        rgb = img.convert("RGB")

    if effect == "flat":
        flat = ImageOps.posterize(rgb, 2)
        flat = flat.quantize(colors=8, method=Image.MEDIANCUT).convert("RGB")
        result = flat
    elif effect == "invert":
        result = ImageOps.invert(rgb)
    elif effect == "blur":
        result = rgb.filter(ImageFilter.GaussianBlur(3))
    elif effect == "contrast":
        result = ImageEnhance.Contrast(rgb).enhance(2)
    elif effect in ("gray", "grayscale"):
        result = ImageOps.grayscale(rgb).convert("RGB")
    elif effect == "mirror":
        result = ImageOps.mirror(rgb)
    elif effect == "rotate":
        result = rgb.rotate(30, expand=True)
    else:
        result = rgb

    if alpha is not None:
        result = result.convert("RGBA")
        result.putalpha(alpha)
    return result

@bot.tree.command(name="pp", description="SHow user profiles picture with optional effect.")
@app_commands.describe(
    user="User to show the profile picture (optional)",
    effect="Effect to apply (flat, invert, blur, contrast, gray, mirror, rotate)"
)
async def pfp(interaction: discord.Interaction, user: discord.User = None, effect: str = None):
    user = user or interaction.user
    avatar_url = user.display_avatar.url
    async with aiohttp.ClientSession() as session:
        async with session.get(avatar_url) as resp:
            if resp.status != 200:
                await interaction.response.send_message("Impossible to get picture 😢")
                return
            data = await resp.read()

    img = Image.open(BytesIO(data)).convert("RGBA")
    img = apply_filter(img, effect)
    with BytesIO() as image_binary:
        img.save(image_binary, 'PNG')
        image_binary.seek(0)
        file = discord.File(fp=image_binary, filename="pp.png")

    eff = f" (filtre : `{effect}`)" if effect else ""
    await interaction.response.send_message(f"Here's the profile picture of **{user.display_name}**{eff} 👇", file=file)

# --------- ON_READY ---------
@bot.event
async def on_ready():
    print(f"✅ Connecté à {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"🔁 {len(synced)} commands sync !")
    except Exception as e:
        print(f"Error sync : {e}")

    if TWITCH_ALERT_CHANNEL_ID:
        check_twitch_live.start()
        print("📡 Twitch surv activated.")
    if YOUTUBE_CHANNELS:
        check_youtube_rss.start()
        print("📺 YT surv activated.")

# --------- LANCEMENT ---------
bot.run(DISCORD_TOKEN, log_handler=handler, log_level=logging.DEBUG)
