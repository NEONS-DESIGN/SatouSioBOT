import os
import random
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from module.color import Color
from module.embed import *
from module.music import *
from module.sqlite import sql_execution
from module.cookie_refresh import CookieManager

load_dotenv()
discordToken = os.getenv("discord_api")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents, help_command=None)

cookie_manager = CookieManager()

@tasks.loop(hours=24)
async def auto_cookie_refresh():
	# Botの稼働中にバックグラウンドでCookieを更新
	await cookie_manager.fetch_youtube_cookies()

class SimplePaginator(discord.ui.View):
	def __init__(self, embeds):
		super().__init__(timeout=180)
		self.embeds = embeds
		self.current_page = 0
	async def update_view(self, interaction: discord.Interaction):
		await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)
	@discord.ui.button(label="◀ 前へ", style=discord.ButtonStyle.primary)
	async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
		self.current_page = (self.current_page - 1) % len(self.embeds)
		await self.update_view(interaction)
	@discord.ui.button(label="次へ ▶", style=discord.ButtonStyle.primary)
	async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
		self.current_page = (self.current_page + 1) % len(self.embeds)
		await self.update_view(interaction)

@bot.event
async def on_ready():
	await bot.tree.sync()
	activity = discord.Activity(type=discord.ActivityType.playing, name="音楽再生BOTです。 /help")
	await bot.change_presence(activity=activity, status=discord.Status.online)
	print(f"{Color.GREEN}[READY]{Color.RESET} {bot.user.name} (ID: {bot.user.id}) としてログインしました。")

@bot.event
async def on_message(message: discord.Message):
	if message.author.bot:
		return
	if bot.user in message.mentions:
		await help_mention_embed(message)
	await bot.process_commands(message)

@bot.hybrid_command(name="help", description="コマンドやコマンドの使い方を表示します。")
async def bot_help(ctx: commands.Context):
	# 処理の遅延を通知
	await ctx.defer(ephemeral=True)
	try:
		embeds = help_pages()
		view = SimplePaginator(embeds)
		await ctx.send(embed=embeds[0], view=view, ephemeral=True)
	except Exception as e:
		# エラー処理
		await exception_embed(ctx, "help", e)
		print(f"{Color.RED}[ERROR]{Color.RESET} help:", e)

@bot.hybrid_command(name="p", description="曲を再生します（YouTube/ニコニコ/SoundCloud対応）")
@app_commands.describe(query="曲のURLかタイトルを入力してください。") # 引数名を query に合わせる
@commands.guild_only()
async def bot_play(ctx: commands.Context, *, query: str): # url から query に変更
	"""
	指定されたURLまたはタイトルから音楽を解析し、再生を開始する。
	注意: 引数名を変更した場合、関数内で参照している変数名もすべて統一する必要がある。
	"""
	if not ctx.author.voice:
		return await user_not_here_embed(ctx)
	await ctx.defer()
	try:
		if not ctx.guild.voice_client:
			await ensure_guild_data(ctx.guild.id)
			server_music_data[ctx.guild.id]["voice_client"] = await ctx.author.voice.channel.connect()
		elif ctx.guild.voice_client.channel != ctx.author.voice.channel:
			await ctx.guild.voice_client.move_to(ctx.author.voice.channel)
			server_music_data[ctx.guild.id]["voice_client"] = ctx.guild.voice_client
		# 内部で呼び出す関数の引数も query に変更
		await play_music(ctx, query, bot)
	except Exception as e:
		await exception_embed(ctx, "play", e)
		print(f"{Color.RED}[ERROR]{Color.RESET} play:", e)

@bot.hybrid_command(name="vol", description="音量を設定します(1~200)。")
@app_commands.describe(volume="音量を入力してください。")
@commands.guild_only()
async def bot_volume(ctx: commands.Context, volume: app_commands.Range[int, 1, 200]):
	await ctx.defer()
	try:
		guild_id = ctx.guild.id
		target_vol = volume / 100
		if ctx.guild.voice_client and ctx.guild.voice_client.source:
			ctx.guild.voice_client.source.volume = target_vol
		guild_search = await sql_execution(f"SELECT guild_id FROM serverData WHERE guild_id={guild_id};")
		if not guild_search:
			await sql_execution(f"INSERT INTO serverData (guild_id, volume) VALUES ({guild_id}, {target_vol});")
		else:
			await sql_execution(f"UPDATE serverData SET volume={target_vol} WHERE guild_id={guild_id};")

		await volume_set_embed(ctx, volume)
	except Exception as e:
		await exception_embed(ctx, "volume", e)

@bot.hybrid_command(name="loop", description="ループ再生を切り替えます。")
@commands.guild_only()
async def bot_loop(ctx: commands.Context):
	await ctx.defer()
	try:
		await ensure_guild_data(ctx.guild.id)
		data = server_music_data[ctx.guild.id]
		data["loop"] = not data["loop"]
		await loop_switch_embed(ctx, "有効" if data["loop"] else "無効")
	except Exception as e:
		await exception_embed(ctx, "loop", e)

@bot.hybrid_command(name="sh", description="キューの中身をシャッフルします。")
@commands.guild_only()
async def bot_shuffle(ctx: commands.Context):
	await ctx.defer()
	try:
		await ensure_guild_data(ctx.guild.id)
		queue = server_music_data[ctx.guild.id]["queue"]
		if not queue:
			return await empty_queue_embed(ctx)
		random.shuffle(queue)
		await shuffle_complete_embed(ctx)
	except Exception as e:
		await exception_embed(ctx, "shuffle", e)

@bot.hybrid_command(name="skip", description="現在の曲をスキップします。")
@commands.guild_only()
async def bot_skip(ctx: commands.Context):
	await ctx.defer()
	try:
		if ctx.guild.voice_client and ctx.guild.voice_client.is_playing():
			ctx.guild.voice_client.stop()
			await skip_music_embed(ctx)
		else:
			await not_playing_embed(ctx)
	except Exception as e:
		await exception_embed(ctx, "skip", e)

@bot.hybrid_command(name="leave", description="BOTを退出させます。")
@commands.guild_only()
async def bot_leave(ctx: commands.Context):
	if not ctx.guild.voice_client:
		return await not_connect_bot_embed(ctx)

	await ctx.defer()
	try:
		guild_id = ctx.guild.id
		await ctx.guild.voice_client.disconnect()
		if guild_id in server_music_data:
			del server_music_data[guild_id]
		await leave_embed(ctx)
	except Exception as e:
		await exception_embed(ctx, "leave", e)

@bot.hybrid_command(name="purge", description="チャンネルのメッセージを一括削除します。")
@app_commands.describe(limit="削除する件数を指定(1~50件)。未指定時は50件。")
@commands.has_permissions(manage_messages=True)
@commands.guild_only()
async def bot_purge(ctx: commands.Context, limit: app_commands.Range[int, 1, 50] = 50):
	# 処理の遅延を通知
	await ctx.defer(ephemeral=True)
	try:
		# 指定された件数(最大50)のメッセージを削除
		# 注意: BOTの権限不足や、Discordの仕様(14日以上前のメッセージ削除不可)によるエラーの可能性あり
		deleted = await ctx.channel.purge(limit=limit)
		await purge_complete_embed(ctx, len(deleted))
	except Exception as e:
		# 削除失敗時のエラーハンドリング
		await exception_embed(ctx, "purge", e)

if __name__ == "__main__":
	# WARNING以上に設定するとINFOが表示されなくなる
	# import logging
	# logging.getLogger('discord').setLevel(logging.WARNING)
	bot.run(discordToken)