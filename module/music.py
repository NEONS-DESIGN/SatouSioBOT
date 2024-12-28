import asyncio
import xml.etree.ElementTree as ET
from yt_dlp import YoutubeDL
import discord

from module.color import *
from module.options import YTDLP_OPTIONS, FFMPEG_OPTIONS
from module.other import *
from module.sqlite import sql_execution

ytdl = YoutubeDL(YTDLP_OPTIONS)

# サーバーごとのデータ管理する辞書
server_music_data = {}

# id = []
# avatar = []

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)

        # 各種データ摘出
        self.title = data.get("title")
        self.url = data.get("url")
        self.duration = data.get('duration')
        self.extractor = data.get("extractor")
        self.id = data.get("id")

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False, volume=0.5):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
        if "entries" in data:
            # プレイリストの最初だけ取得
            data = data["entries"][0]
        filename = data["url"] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTIONS), data=data, volume=volume)

class YTDLInfo():
    def __init__(self, data: dict):
        self.data = data
        # 各種データ摘出
        self.title = data.get("title")
        self.url = data.get("url")
        self.duration = data.get('duration')
        self.id = data.get("id")
    @classmethod
    def from_url(cls, url):
        data = ytdl.extract_info(url, download=False)
        if "entries" in data:
            # プレイリストの最初だけ取得
            data = data["entries"][0]
        return cls(data=data)

async def ensure_guild_data(guild_id):
    """
    サーバー(guild)のデータがなければ初期化します

    Args:
        guild_id (int): ギルドID
    """
    if guild_id not in server_music_data:
        server_music_data[guild_id] = {
            "queue": asyncio.Queue(),
            "id": [],
            "avatar": [],
            "voice_client": None,
            "current_player": None,
            "loop": False,  # ループ再生のフラグ
        }

async def play_next_song(ctx, guild_id, bot):
    """
    次の曲を再生

    Args:
        ctx (commands.context):
        guild_id (int):
        bot (discord.Bot):
    """
    guild_data = server_music_data[guild_id]
    if guild_data["loop"] and guild_data["current_player"]:
        # ループが有効な場合、現在の曲を再度キューに追加
        await guild_data["queue"].put(guild_data["current_player"])

    if not guild_data["queue"].empty():
        player = await guild_data["queue"].get()
        guild_data["current_player"] = player
        guild_data["voice_client"].play(
            player,
            after=lambda e: asyncio.run_coroutine_threadsafe(play_next_song(ctx, guild_id, bot), bot.loop).result(),
        )
        channel = guild_data["voice_client"].channel
        print(f"Playing in {channel}: {player.title}")
    else:
        await ctx.guild.voice_client.disconnect()
        embed = discord.Embed(title="全てのトラックの再生が終了しました。", color=Embed.LIGHT_GREEN)
        await ctx.send(embed=embed)
        return

async def play_music(ctx, url, bot):
    await ensure_guild_data(ctx.guild.id)
    guild_data = server_music_data[ctx.guild.id]
    voice_client = guild_data["voice_client"]

    async with ctx.typing():
        # データベース検索
        guild_db = await sql_execution(f"SELECT * FROM serverData WHERE guild_id={ctx.guild_id};")
        # 音量変更
        if not guild_db or guild_db[0][1] is None:
            volume = 0.25
        elif guild_db[0][1]:
                volume = guild_db[0][1]

        if guild_data["queue"].empty():
            player = await YTDLSource.from_url(url, loop=bot.loop, stream=False, volume=volume)
        else:
            player = YTDLInfo.from_url(url)
        await guild_data["queue"].put(player)
        guild_data["id"].append(ctx.author.name)
        guild_data["avatar"].append(ctx.author.avatar)

        if not voice_client.is_playing():
        # if not ctx.guild.voice_client.is_playing():
            await play_next_song(ctx, ctx.guild_id, bot)

        # 動画ID取得
        movie_id, type = await get_id(url)
        dst_path = f"./temp/{movie_id}.jpg"
        # サムネイルダウンロード
        # thumbnail_url = f"https://i.ytimg.com/vi/{player.id}/maxresdefault.jpg"
        thumbnail_url = f"https://i.ytimg.com/vi_webp/{player.id}/maxresdefault.webp"
        if type == "niconico":
            niconico_api = f"https://ext.nicovideo.jp/api/getthumbinfo/{movie_id}"
            req = urllib.request.Request(niconico_api)
            with urllib.request.urlopen(req) as response:
                xml_string = response.read()
            root = ET.fromstring(xml_string)
            thumbnail_url = root[0][3].text
        elif type == "title":
            dst_path = f"./temp/{player.id}.jpg"
            url = f"https://youtu.be/{player.id}"
        await download_file(thumbnail_url, dst_path)
        # サムネイル取り込み処理
        f_name = f"{player.id}.jpg"
        f_pass = f"./temp/{player.id}.jpg"
        thumbnail_img = discord.File(fp=f_pass, filename=f_name, spoiler=False)
        embed = discord.Embed(title="再生中", color=Embed.LIGHT_GREEN)
        embed.add_field(name="タイトル", value=f"[{player.title}]({url})", inline=False)
        # 再生時間
        time = await play_time(player.duration)
        qsize = guild_data["queue"].qsize()
        if qsize == 0:
            embed.add_field(name="再生時間", value=time, inline=False)
        else:
            embed.add_field(name="再生時間", value=time, inline=True)
            embed.add_field(name="待機曲", value=str(qsize)+" 件", inline=True)
        # リクエスト者情報
        embed.set_image(url=f"attachment://{f_name}")
        embed.set_footer(text=f"Requested by: {str(guild_data['id'].pop(0))}", icon_url=guild_data["avatar"].pop(0))

        await ctx.respond(embed=embed, file=thumbnail_img, view=Link(player.url))
        return