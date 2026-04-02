import os
import random
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from module.embed import *
from module.logger import setup_daily_logger, get_bot_logger
from module.music import play_music, ensure_guild_data, server_music_data
from module.setting import setup_setting_commands
from module.sqlite import sql_execution

# 環境変数の読み込み
load_dotenv()
discordToken = os.getenv("discord_api")

# ロガーの初期化
setup_daily_logger()
logger = get_bot_logger()

# discord.py デフォルトのコンソール出力を有効化
# discord.utils.setup_logging(root=False)

# ==========================================
# Botクラスの定義 (setup_hookを利用)
# ==========================================
class SatouSioBot(commands.Bot):
	def __init__(self):
		intents = discord.Intents.default()
		intents.message_content = True
		super().__init__(command_prefix="/", intents=intents, help_command=None)

	async def setup_hook(self):
		"""Bot起動時の非同期セットアップ処理"""
		# 設定コマンドの登録
		setup_setting_commands(self)

		# スラッシュコマンドの同期
		await self.tree.sync()
		logger.info("スラッシュコマンドを同期しました。")

bot = SatouSioBot()

# ==========================================
# UIコンポーネント
# ==========================================
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

# ==========================================
# イベントハンドラ
# ==========================================
@bot.event
async def on_ready():
	activity = discord.Activity(type=discord.ActivityType.playing, name="音楽再生BOTです。 /help")
	await bot.change_presence(activity=activity, status=discord.Status.online)
	# loggerで綺麗に出力
	logger.info(f"[READY] {bot.user.name} (ID: {bot.user.id}) としてログインしました。")

@bot.event
async def on_message(message: discord.Message):
	if message.author.bot:
		return
	if bot.user in message.mentions:
		await help_mention_embed(message)
	await bot.process_commands(message)

# ==========================================
# スラッシュコマンド群
# ==========================================
@bot.hybrid_command(name="help", description="コマンドやコマンドの使い方を表示します。")
async def bot_help(ctx: commands.Context):
	await ctx.defer(ephemeral=True)
	try:
		embeds = help_pages()
		view = SimplePaginator(embeds)
		await ctx.send(embed=embeds, view=view, ephemeral=True)
	except Exception as e:
		await exception_embed(ctx, "help", e)
		logger.error(f"helpコマンド実行エラー: {e}")

@bot.hybrid_command(name="p", description="曲を再生します（YouTube/ニコニコ/SoundCloud対応）")
@app_commands.describe(query="曲のURLかタイトルを入力してください。")
@app_commands.rename(query="urlか曲名")
@commands.guild_only()
async def bot_play(ctx: commands.Context, *, query: str):
	if not ctx.author.voice:
		return await user_not_here_embed(ctx)
	await ctx.defer()
	try:
		if not ctx.guild.voice_client:
			await ensure_guild_data(ctx.guild.id, bot)
			server_music_data[ctx.guild.id]["voice_client"] = await ctx.author.voice.channel.connect()
		elif ctx.guild.voice_client.channel != ctx.author.voice.channel:
			await ctx.guild.voice_client.move_to(ctx.author.voice.channel)
			server_music_data[ctx.guild.id]["voice_client"] = ctx.guild.voice_client

		await play_music(ctx, query, bot)
	except Exception as e:
		await exception_embed(ctx, "play", e)
		logger.error(f"playコマンド実行エラー: {e}")

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

		# 安全なプレースホルダー(?)を使用したSQLの実行
		await sql_execution("INSERT OR IGNORE INTO serverData (guild_id) VALUES (?);", (guild_id,))
		await sql_execution("UPDATE serverData SET volume=? WHERE guild_id=?;", (target_vol, guild_id))

		await volume_set_embed(ctx, volume)
	except Exception as e:
		await exception_embed(ctx, "volume", e)
		logger.error(f"volumeコマンド実行エラー: {e}")

@bot.hybrid_command(name="loop", description="ループ再生を切り替えます。")
@commands.guild_only()
async def bot_loop(ctx: commands.Context):
	await ctx.defer()
	try:
		await ensure_guild_data(ctx.guild.id, bot)
		data = server_music_data[ctx.guild.id]
		data["loop"] = not data["loop"]
		await loop_switch_embed(ctx, "有効" if data["loop"] else "無効")
	except Exception as e:
		await exception_embed(ctx, "loop", e)
		logger.error(f"loopコマンド実行エラー: {e}")

@bot.hybrid_command(name="sh", description="キューの中身をシャッフルします。")
@commands.guild_only()
async def bot_shuffle(ctx: commands.Context):
	await ctx.defer()
	try:
		await ensure_guild_data(ctx.guild.id, bot)
		queue = server_music_data[ctx.guild.id]["queue"]
		if not queue:
			return await empty_queue_embed(ctx)
		random.shuffle(queue)
		await shuffle_complete_embed(ctx)
	except Exception as e:
		await exception_embed(ctx, "shuffle", e)
		logger.error(f"shuffleコマンド実行エラー: {e}")

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
		logger.error(f"skipコマンド実行エラー: {e}")

@bot.hybrid_command(name="move", description="Botを自分のいるボイスチャンネルに移動させます。")
@commands.guild_only()
async def bot_move(ctx: commands.Context):
	if not ctx.author.voice:
		return await user_not_here_embed(ctx)
	if not ctx.guild.voice_client:
		return await bot_not_in_vc_embed(ctx)
	if ctx.guild.voice_client.channel == ctx.author.voice.channel:
		return await already_in_channel_embed(ctx)

	try:
		await ctx.guild.voice_client.move_to(ctx.author.voice.channel)
		await ensure_guild_data(ctx.guild.id, bot)
		await move_success_embed(ctx, ctx.author.voice.channel)
	except Exception as e:
		await exception_embed(ctx, "move", e)
		logger.error(f"moveコマンド実行エラー: {e}")

@bot.hybrid_command(name="leave", description="BOTを退出させます。")
@commands.guild_only()
async def bot_leave(ctx: commands.Context):
	if not ctx.guild.voice_client:
		return await not_connect_bot_embed(ctx)

	await ctx.defer()
	try:
		guild_id = ctx.guild.id
		await ctx.guild.voice_client.disconnect()

		# キューとワーカーのクリーンアップ
		if guild_id in server_music_data:
			data = server_music_data[guild_id]
			if data.get("worker_task") and not data["worker_task"].done():
				data["worker_task"].cancel()
			del server_music_data[guild_id]

		await leave_embed(ctx)
	except Exception as e:
		await exception_embed(ctx, "leave", e)
		logger.error(f"leaveコマンド実行エラー: {e}")

@bot.hybrid_command(name="purge", description="チャンネルのメッセージを一括削除します。")
@app_commands.describe(limit="削除する件数を指定(1~50件)。未指定時は50件。")
@commands.has_permissions(manage_messages=True)
@commands.guild_only()
async def bot_purge(ctx: commands.Context, limit: app_commands.Range[int, 1, 50] = 50):
	await ctx.defer(ephemeral=True)
	try:
		deleted = await ctx.channel.purge(limit=limit)
		await purge_complete_embed(ctx, len(deleted))
	except Exception as e:
		await exception_embed(ctx, "purge", e)
		logger.error(f"purgeコマンド実行エラー: {e}")

if __name__ == "__main__":
	bot.run(discordToken)