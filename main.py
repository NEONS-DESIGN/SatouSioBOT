import os
import discord
from discord import guild_only, Option
from discord.ext import commands
from dotenv import load_dotenv

from module.color import Color
from module.embed import *
from module.music import *

# .envファイルの読み込み
load_dotenv()
# Discord APIToken設定
discordToken = os.getenv("discord_api")

# Botの初期設定
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Bot(description="Starting up", intents=intents)

@bot.event  # 起動時に自動的に動くメソッド
async def on_ready():
    activity = discord.Activity(application_id=567032668668166181, type=2, name="/help", state="音楽再生BOTです。")
    # BOTのステータスを変更する
    await bot.change_presence(activity=activity, status=discord.Status.online)
    # BOTの情報表示
    print("name: " + bot.user.name)
    print("id: ", bot.user.id)
    print("count: ", len(bot.guilds))
    print("guilds: \n", bot.guilds, "\n")

@bot.listen()
async def on_message(message: discord.Message):
    # メッセージの送信者がbotだった場合は無視する
    if message.author.bot:
        return
    if bot.user in message.mentions:  # メンションされたか
        await message.channel.send(f'{message.author.mention} 助けが必要ですか？\n必要な場合は、</help:1140613857224687616> コマンドを打ってみてください。')

@bot.command(name="p", description="YouTube・ニコニコ動画・SoundCloudの曲を再生できます。")
@guild_only()
async def bot_play(ctx: commands.context, *, url: Option(str, description='曲のURLかタイトルを入力してください。', name="urlかタイトル名")):
    if ctx.author.bot:  # BOT自身に反応しなくする。
        return
    if ctx.author.voice is None:  # そのボイスチャンネルにコマンドを打ち込んだ人が接続していない場合
        await user_not_here_embed(ctx)
        return
    if ctx.guild.voice_client is None:  # 鯖のどの部屋にも居ない場合
        # await ctx.author.voice.channel.connect()
        server_music_data[ctx.guild.id]["voice_client"] = await ctx.author.voice.channel.connect()
        # channel = ctx.author.voice.channel
        # voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
        # if not voice_client:
        #     server_music_data[ctx.guild.id]["voice_client"] = await channel.connect()
        #     await ctx.send(f"Joined {channel}!")
    if ctx.guild.voice_client is not None:  # コマンド主と違うボイスチャンネルにいる場合
        await ctx.voice_client.move_to(ctx.author.voice.channel)

    # 処理中表記
    await ctx.defer()

    await ensure_guild_data(ctx.guild.id)

    try:
        return await play_music(ctx, url, bot)
    except Exception as e:
        await exception_embed(ctx, "music", e)
        return print(f"\n{Color.RED}[ERROR]{Color.RESET}\n",e)

# BOT起動
bot.run(discordToken)