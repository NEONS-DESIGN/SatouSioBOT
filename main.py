import os
import discord
from discord import guild_only, Option
from discord.ext import commands
from dotenv import load_dotenv

from module.color import Color
from module.embed import *
from module.music import *
from module.sqlite import sql_execution

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
        await ensure_guild_data(ctx.guild.id)
        server_music_data[ctx.guild.id]["voice_client"] = await ctx.author.voice.channel.connect()
    if ctx.guild.voice_client is not None:  # コマンド主と違うボイスチャンネルにいる場合
        await ctx.voice_client.move_to(ctx.author.voice.channel)
    # 処理中表記
    await ctx.defer()
    try:
        return await play_music(ctx, url, bot)
    except Exception as e:
        await exception_embed(ctx, "music", e)
        return print(f"\n{Color.RED}[ERROR]{Color.RESET}\n",e)

@bot.command(name="vol", description="再生する音量の設定ができます。")
@guild_only()
async def bot_volume(ctx: commands.context, volume: Option(int, description='1~200の間で音量を入力してください。', name='音量', min_value=1, max_value=200)):
    if ctx.author.bot:  # BOT自身に反応しなくする。
        return
    # 処理中表記
    await ctx.defer()
    try:
        # ギルド情報取得
        guild_id = int(ctx.guild_id)
        guild_search = await sql_execution(f"SELECT * FROM serverData WHERE guild_id={guild_id};")
        # 再生中の音量変更
        if ctx.guild.voice_client:
            if ctx.guild.voice_client.is_playing():
                ctx.guild.voice_client.source.volume = volume / 100
        # データベースへの書き込み
        if not guild_search:
            result = await sql_execution(f"INSERT INTO serverData (guild_id, volume) VALUES ({guild_id}, {volume / 100});")
        elif guild_search[0] != "":
            result = await sql_execution(f"UPDATE serverData SET volume={volume / 100} WHERE guild_id={guild_id};")
        embed = discord.Embed(title=f"再生音量を、 {volume}% に設定しました。", color=Embed.LIGHT_GREEN)
        await ctx.respond(embed=embed)
        return
    except Exception as e:
        await exception_embed(ctx, "volume", e)
        print(f"\n{Color.RED}[ERROR]{Color.RESET}\n",e)
        return

@bot.command(name="leave", description="BOTを退出させます。")
@guild_only()
async def bot_leave(ctx):
    if ctx.author.bot:  # BOT自身に反応しなくする。
        return
    # BOTが接続していない場合
    if ctx.guild.voice_client is None:
        await not_connect_bot_embed(ctx)
        return
    # 処理中表記
    await ctx.defer()
    try:
        # ギルド情報取得
        guild_id = int(ctx.guild_id)
        guild_data = server_music_data[guild_id]
        que = guild_data["queue"]
        if que.qsize() != 0:
            server_music_data.pop(guild_id)
        # 切断する
        await ctx.guild.voice_client.disconnect()
        await leave_embed(ctx)
        return
    except Exception as e:
        await exception_embed(ctx, "leave", e)
        print(f"\n{Color.RED}[ERROR]{Color.RESET}\n",e)
        return

# BOT起動
if __name__ == "__main__":
    bot.run(discordToken)