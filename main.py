import os
import random
import discord
from discord import guild_only, Option
from discord.ext import commands, pages
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
    print("guild_count: ", len(bot.guilds))

@bot.listen()
async def on_message(message: discord.Message):
    # メッセージの送信者がbotだった場合は無視する
    if message.author.bot:
        return
    if bot.user in message.mentions:  # メンションされたか
        await message.channel.send(f'{message.author.mention} 助けが必要ですか？\n必要な場合は、</help:1140613857224687616> コマンドを打ってみてください。')

@bot.command(name="help", description="コマンドやコマンドの使い方を表示します。")
async def bot_help(ctx: discord.Integration):
    if ctx.author.bot:  # BOT自身に反応しなくする。
        return
    await ctx.defer(ephemeral=True)
    try:
        paginator = pages.Paginator(pages=help_pages(), loop_pages=True)
        await paginator.respond(ctx.interaction, ephemeral=True)
        return
    except Exception as e:
        await exception_embed(ctx, "help", e)
        print(f"\n{Color.RED}[ERROR]{Color.RESET}\n",e)
        return

@bot.command(name="p", description="YouTube・ニコニコ動画・SoundCloudなどの曲を再生できます。")
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

@bot.command(name="loop", description="キューリストをループ再生することができます。")
@guild_only()
async def bot_loop(ctx: discord.Integration):
    if ctx.author.bot:  # BOT自身に反応しなくする。
        return
    # 処理中表記
    await ctx.defer()
    try:
        await ensure_guild_data(ctx.guild.id)
        # ギルド情報取得
        guild_data = server_music_data[ctx.guild.id]
        guild_data["loop"] = not guild_data["loop"]
        state = "有効" if guild_data["loop"] else "無効"
        await loop_switch_embed(ctx, state)
        return
    except Exception as e:
        await exception_embed(ctx, "repeat", e)
        print(f"\n{Color.RED}[ERROR]{Color.RESET}\n",e)
        return

@bot.command(name="sh", description="(不安定)キューリストの中身の順番をシャッフルします。")
async def bot_shuffle(ctx):
    if ctx.author.bot:  # BOT自身に反応しなくする。
        return
    # 処理中表記
    await ctx.defer()
    try:
        # ギルド情報取得
        guild_id = ctx.guild.id
        await ensure_guild_data(guild_id)
        guild_data = server_music_data[guild_id]
        # キューの内容をリストに変換
        que_items = []
        id_items = []
        avatar_items = []
        url_items = []
        while not guild_data["queue"].empty():
            que_items.append(await guild_data["queue"].get())
            id_items.append(await guild_data["id"].get())
            avatar_items.append(await guild_data["avatar"].get())
            url_items.append(await guild_data["url"].get())
        # キューになにもない場合
        if not que_items:
            await empty_queue_embed(ctx)
            return
        # シャッフルして再びキューに戻す
        seed = random.randint(1, 10000)
        random.seed(seed)
        random.shuffle(que_items)
        for item in que_items:
            await guild_data["queue"].put(item)
        random.seed(seed)
        random.shuffle(id_items)
        for item in id_items:
            await guild_data["id"].put(item)
        random.seed(seed)
        random.shuffle(avatar_items)
        for item in avatar_items:
            await guild_data["avatar"].put(item)
        random.seed(seed)
        random.shuffle(url_items)
        for item in url_items:
            await guild_data["url"].put(item)
        await shuffle_complete_embed(ctx)
    except Exception as e:
        await exception_embed(ctx, "shuffle", e)
        print(f"\n{Color.RED}[ERROR]{Color.RESET}\n",e)
        return

@bot.command(name="skip", description="再生中の曲を停止し、次の曲を再生します。")
@guild_only()
async def bot_stop(ctx: commands.context):
    if ctx.author.bot:  # BOT自身に反応しなくする。
        return
    if ctx.author.voice is None:  # そのボイスチャンネルにコマンドを打ち込んだ人が接続していない場合
        await user_not_here_embed(ctx)
        return
    if ctx.guild.voice_client is None:  # 鯖のどの部屋にも居ない場合
        await ctx.author.voice.channel.connect()
    if ctx.guild.voice_client is not None:  # コマンド主と違うボイスチャンネルにいる場合
        await ctx.voice_client.move_to(ctx.author.voice.channel)
    # 処理中表記
    await ctx.defer()
    try:
        # ギルド情報取得
        guild_id = ctx.guild.id
        guild_data = server_music_data.get(guild_id)
        if guild_data and guild_data["voice_client"]:
            guild_data["voice_client"].stop()
            await skip_music_embed(ctx)
        else:
            await not_playing_embed(ctx)
    except Exception as e:
        await exception_embed(ctx, "skip", e)
        print(f"\n{Color.RED}[ERROR]{Color.RESET}\n",e)
        return

@bot.command(name="move", description="BOTを移動させます。")
@guild_only()
async def bot_move(ctx):
    if ctx.author.bot:  # BOT自身に反応しなくする。
        return
    # そのボイスチャンネルにコマンドを打ち込んだ人が接続していない場合
    if ctx.author.voice is None:
        await user_not_here_embed(ctx)
        return
    # BOTが接続していない場合
    if ctx.guild.voice_client is None:
        await not_connect_bot_embed(ctx)
        return
    try:
        # メッセージを送信したユーザーがいるボイスチャンネルに移動する
        await ctx.guild.voice_client.move_to(ctx.author.voice.channel)
        return
    except Exception as e:
        await exception_embed(ctx, "move", e)
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
        # 切断する
        await guild_data["voice_client"].disconnect()
        server_music_data[guild_id] = None
        await leave_embed(ctx)
        return
    except Exception as e:
        await exception_embed(ctx, "leave", e)
        print(f"\n{Color.RED}[ERROR]{Color.RESET}\n",e)
        return

@bot.command(name="履歴削除", description="テキストチャンネルの履歴を削除します。")
@guild_only()
async def bot_purge(ctx):
    if ctx.author.bot:  # BOT自身に反応しなくする。
        return
    try:
        await ctx.channel.purge()
        embed = discord.Embed(title="削除完了", color=0x1dd1a1)
        await ctx.respond(embed=embed)
        return
    except Exception as e:
        await exception_embed(ctx, "purge", e)
        print(f"\n{Color.RED}[ERROR]{Color.RESET}\n",e)
        return

# BOT起動
if __name__ == "__main__":
    bot.run(discordToken)