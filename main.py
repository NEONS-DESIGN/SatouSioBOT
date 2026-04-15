import asyncio
import os
import random
import sys
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from module.embed import *
from module.logger import setup_daily_logger, get_bot_logger
from module.music import play_music, ensure_guild_data, server_music_data
from module.setting import setup_setting_commands
from module.sqlite import init_db, sql_execution

# OS別の高速イベントループを適用する
# win32: winloop / その他: uvloop / どちらもなければ標準ループ
def _apply_fast_event_loop() -> None:
	if sys.platform == "win32":
		try:
			import winloop
			asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
		except ImportError:
			pass
	else:
		try:
			import uvloop
			asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
		except ImportError:
			pass

_apply_fast_event_loop()

load_dotenv()
_DISCORD_TOKEN: str = os.getenv("discord_api", "")

setup_daily_logger()
logger = get_bot_logger()

# ==========================================
# Bot本体
# ==========================================
class SatouSioBot(commands.Bot):
	def __init__(self) -> None:
		intents = discord.Intents.default()
		intents.message_content = True
		super().__init__(command_prefix="/", intents=intents, help_command=None)
	async def setup_hook(self) -> None:
		"""起動時の非同期セットアップ: 設定コマンド登録 → スラッシュコマンド同期"""
		setup_setting_commands(self)
		await self.tree.sync()
		logger.info("スラッシュコマンドを同期しました。")

bot = SatouSioBot()

# ==========================================
# ページネーション UI
# ==========================================
class SimplePaginator(discord.ui.View):
	"""
	複数のEmbedをページ送りで表示するUI。
	- 3ページ未満の場合は「最初へ」「最後へ」ボタンを非表示にする
	- タイムアウト(120秒)後はすべてのボタンを無効化する
	"""
	def __init__(self, embeds: list[discord.Embed]) -> None:
		super().__init__(timeout=120)
		self.embeds = embeds
		self.current_page = 0
		self.message: discord.Message | None = None
		if len(embeds) < 3:
			self.remove_item(self.first_button)
			self.remove_item(self.last_button)
		else:
			self._sync_buttons()
	def _sync_buttons(self) -> None:
		"""現在ページに応じて両端ボタンの有効/無効を更新する"""
		self.first_button.disabled = self.current_page == 0
		self.last_button.disabled = self.current_page == len(self.embeds) - 1
	async def _update(self, interaction: discord.Interaction) -> None:
		if len(self.embeds) >= 3:
			self._sync_buttons()
		await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)
	async def on_timeout(self) -> None:
		"""タイムアウト時: 全ボタンをグレーアウトして編集"""
		for child in self.children:
			child.disabled = True
		if self.message:
			try:
				await self.message.edit(view=self)
			except discord.NotFound:
				pass
	@discord.ui.button(label="❚◀", style=discord.ButtonStyle.primary)
	async def first_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
		self.current_page = 0
		await self._update(interaction)
	@discord.ui.button(label="◀", style=discord.ButtonStyle.success)
	async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
		self.current_page = (self.current_page - 1) % len(self.embeds)
		await self._update(interaction)
	@discord.ui.button(label="▶", style=discord.ButtonStyle.success)
	async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
		self.current_page = (self.current_page + 1) % len(self.embeds)
		await self._update(interaction)
	@discord.ui.button(label="▶❚", style=discord.ButtonStyle.primary)
	async def last_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
		self.current_page = len(self.embeds) - 1
		await self._update(interaction)

# ==========================================
# イベントハンドラ
# ==========================================
@bot.event
async def on_ready() -> None:
	await init_db()
	activity = discord.Activity(
		type=discord.ActivityType.playing,
		name="音楽再生BOTです。 /help",
	)
	await bot.change_presence(activity=activity, status=discord.Status.online)
	logger.info(f"{bot.user.name} (ID: {bot.user.id}) としてログインしました。")

@bot.event
async def on_message(message: discord.Message) -> None:
	if message.author.bot:
		return
	if bot.user in message.mentions:
		await help_mention_embed(message)
	await bot.process_commands(message)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState,) -> None:
	"""BotがVCから予期せず切断された際のリソースクリーンアップ"""
	if member.id != bot.user.id:
		return
	if before.channel is not None and after.channel is None:
		guild_id = member.guild.id
		if player := server_music_data.pop(guild_id, None):
			player.cleanup()
			logger.info(f"[CLEANUP] ギルド {guild_id} からBotが切断されたため、リソースをクリーンアップしました。")

# ==========================================
# スラッシュコマンド群
# ==========================================
@bot.hybrid_command(name="help", description="コマンドやコマンドの使い方を表示します。")
async def bot_help(ctx: commands.Context) -> None:
	await ctx.defer(ephemeral=True)
	try:
		embeds = help_pages()
		view = SimplePaginator(embeds)
		message = await ctx.send(embed=embeds[0], view=view, ephemeral=True)
		view.message = message
	except Exception as e:
		await exception_embed(ctx, "help", e)
		logger.error(f"helpコマンド実行エラー: {e}")

@bot.hybrid_command(name="p", description="曲を再生します（YouTube/ニコニコ/SoundCloudなど対応）")
@app_commands.describe(query="曲のURLかタイトルを入力してください。")
@app_commands.rename(query="urlか曲名")
@commands.guild_only()
async def bot_play(ctx: commands.Context, *, query: str) -> None:
	if not ctx.author.voice:
		return await user_not_here_embed(ctx)
	await ctx.defer()
	try:
		vc = ctx.guild.voice_client
		if not vc:
			player = await ensure_guild_data(ctx.guild.id, bot)
			player.voice_client = await ctx.author.voice.channel.connect()
		elif vc.channel != ctx.author.voice.channel:
			await vc.move_to(ctx.author.voice.channel)
			player = await ensure_guild_data(ctx.guild.id, bot)
			player.voice_client = vc
		await play_music(ctx, query, bot)
	except Exception as e:
		await exception_embed(ctx, "play", e)
		logger.error(f"playコマンド実行エラー: {e}")

@bot.hybrid_command(name="vol", description="音量を設定します(1~200)。")
@app_commands.describe(volume="音量を入力してください。")
@commands.guild_only()
async def bot_volume(ctx: commands.Context, volume: app_commands.Range[int, 1, 200]) -> None:
	await ctx.defer()
	try:
		guild_id = ctx.guild.id
		target_vol = volume / 100
		vc = ctx.guild.voice_client
		if vc and vc.source:
			vc.source.volume = target_vol
		await sql_execution("INSERT OR IGNORE INTO server_data (guild_id) VALUES (?);", (guild_id,))
		await sql_execution("UPDATE server_data SET volume=? WHERE guild_id=?;", (target_vol, guild_id))
		await volume_set_embed(ctx, volume)
	except Exception as e:
		await exception_embed(ctx, "volume", e)
		logger.error(f"volumeコマンド実行エラー: {e}")

@bot.hybrid_command(name="loop", description="現在入っているキューをループ再生します。もう一度実行するとループ解除します。")
@commands.guild_only()
async def bot_loop(ctx: commands.Context) -> None:
	await ctx.defer()
	try:
		player = await ensure_guild_data(ctx.guild.id, bot)
		player.loop = not player.loop
		await loop_switch_embed(ctx, "有効" if player.loop else "無効")
	except Exception as e:
		await exception_embed(ctx, "loop", e)
		logger.error(f"loopコマンド実行エラー: {e}")

@bot.hybrid_command(name="sh", description="キューの中身をシャッフルします。")
@commands.guild_only()
async def bot_shuffle(ctx: commands.Context) -> None:
	await ctx.defer()
	try:
		player = await ensure_guild_data(ctx.guild.id, bot)
		if not player.queue:
			return await empty_queue_embed(ctx)
		# dequeはrandom.shuffleに対応していないのでリスト変換 → シャッフル → 戻す
		tmp = list(player.queue)
		random.shuffle(tmp)
		player.queue.clear()
		player.queue.extend(tmp)
		await shuffle_complete_embed(ctx)
	except Exception as e:
		await exception_embed(ctx, "shuffle", e)
		logger.error(f"shuffleコマンド実行エラー: {e}")

@bot.hybrid_command(name="skip", description="現在の曲をスキップします。")
@commands.guild_only()
async def bot_skip(ctx: commands.Context) -> None:
	await ctx.defer()
	try:
		vc = ctx.guild.voice_client
		if vc and vc.is_playing():
			vc.stop()
			await skip_music_embed(ctx)
		else:
			await not_playing_embed(ctx)
	except Exception as e:
		await exception_embed(ctx, "skip", e)
		logger.error(f"skipコマンド実行エラー: {e}")

@bot.hybrid_command(name="move", description="Botを自分のいるボイスチャンネルに移動させます。")
@commands.guild_only()
async def bot_move(ctx: commands.Context) -> None:
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
async def bot_leave(ctx: commands.Context) -> None:
	if not ctx.guild.voice_client:
		return await not_connect_bot_embed(ctx)
	await ctx.defer()
	try:
		guild_id = ctx.guild.id
		vc = ctx.guild.voice_client
		if vc.is_playing() or vc.is_paused():
			vc.stop()
		await vc.disconnect()
		if player := server_music_data.pop(guild_id, None):
			player.cleanup()
		await leave_embed(ctx)
	except Exception as e:
		await exception_embed(ctx, "leave", e)
		logger.error(f"leaveコマンド実行エラー: {e}")

@bot.hybrid_command(name="purge", description="チャンネルのメッセージを一括削除します。")
@app_commands.describe(limit="削除する件数を指定(1~50件)。未指定時は50件。")
@app_commands.rename(limit="件数")
@commands.has_permissions(manage_messages=True)
@commands.guild_only()
async def bot_purge(ctx: commands.Context, limit: app_commands.Range[int, 1, 50] = 50) -> None:
	await ctx.defer(ephemeral=True)
	try:
		deleted = await ctx.channel.purge(limit=limit)
		await purge_complete_embed(ctx, len(deleted))
	except Exception as e:
		await exception_embed(ctx, "purge", e)
		logger.error(f"purgeコマンド実行エラー: {e}")

@bot.hybrid_command(name="qlist", description="現在のキューに入っている曲のリストを表示します。")
@commands.guild_only()
async def bot_qlist(ctx: commands.Context) -> None:
	await ctx.defer()
	try:
		player = await ensure_guild_data(ctx.guild.id, bot)
		if not player.queue:
			return await empty_queue_embed(ctx)
		embeds = await queue_list_pages(player.queue)
		if len(embeds) == 1:
			await ctx.send(embed=embeds[0])
		else:
			view = SimplePaginator(embeds)
			message = await ctx.send(embed=embeds[0], view=view)
			view.message = message
	except Exception as e:
		await exception_embed(ctx, "qlist", e)
		logger.error(f"qlistコマンド実行エラー: {e}")

@bot.hybrid_command(name="pause", description="現在再生中の曲を一時停止します。")
@commands.guild_only()
async def bot_pause(ctx: commands.Context) -> None:
	await ctx.defer()
	try:
		vc = ctx.guild.voice_client
		if not vc or not vc.is_connected():
			return await bot_not_in_vc_embed(ctx)
		if vc.is_paused():
			return await already_paused_embed(ctx)
		if vc.is_playing():
			vc.pause()
			await pause_embed(ctx)
		else:
			await not_playing_embed(ctx)
	except Exception as e:
		await exception_embed(ctx, "pause", e)
		logger.error(f"pauseコマンド実行エラー: {e}")

@bot.hybrid_command(name="resume", description="一時停止中の曲を再開します。")
@commands.guild_only()
async def bot_resume(ctx: commands.Context) -> None:
	await ctx.defer()
	try:
		vc = ctx.guild.voice_client
		if not vc or not vc.is_connected():
			return await bot_not_in_vc_embed(ctx)
		if vc.is_playing():
			return await already_playing_embed(ctx)
		if vc.is_paused():
			vc.resume()
			await resume_embed(ctx)
		else:
			await not_playing_embed(ctx)
	except Exception as e:
		await exception_embed(ctx, "resume", e)
		logger.error(f"resumeコマンド実行エラー: {e}")

@bot.hybrid_command(name="clear", description="キューに入っている曲を削除します。")
@app_commands.describe(start="削除する件数、または削除を開始する番号", end="削除を終了する番号(範囲指定時)")
@app_commands.rename(start="件数または開始番号", end="終了番号")
@commands.guild_only()
async def bot_clear(ctx: commands.Context, start: int = None, end: int = None) -> None:
	await ctx.defer()
	try:
		player = await ensure_guild_data(ctx.guild.id, bot)
		if not player.queue:
			return await empty_queue_embed(ctx)
		q_len = len(player.queue)
		q_list = list(player.queue)
		if start is None and end is None:
			# 引数なし: 全削除
			deleted = q_len
			q_list.clear()
		elif start is not None and end is None:
			# 引数1つ: 先頭からstart件削除
			if start < 1:
				return await invalid_clear_range_embed(ctx)
			cut = min(start, q_len)
			deleted = cut
			del q_list[:cut]
		else:
			# 引数2つ: start〜end範囲削除 (1-indexed)
			if start < 1 or end < start:
				return await invalid_clear_range_embed(ctx)
			s = start - 1
			e = min(end, q_len)
			if s >= q_len:
				return await invalid_clear_range_embed(ctx)
			deleted = e - s
			del q_list[s:e]
		player.queue.clear()
		player.queue.extend(q_list)
		await clear_queue_embed(ctx, deleted)
	except Exception as e:
		await exception_embed(ctx, "clear", e)
		logger.error(f"clearコマンド実行エラー: {e}")

@bot.hybrid_command(name="replay", description="現在再生中の曲を最初から再生し直します。")
@commands.guild_only()
async def bot_replay(ctx: commands.Context) -> None:
	await ctx.defer()
	try:
		vc = ctx.guild.voice_client
		if not vc or not vc.is_connected():
			return await bot_not_in_vc_embed(ctx)
		player = await ensure_guild_data(ctx.guild.id, bot)
		if not player.current:
			return await not_playing_embed(ctx)
		# 現在の曲をキュー先頭に積み直す
		# stream_urlを消してワーカーに再取得させる（TTLキャッシュ経由）
		loop_track = {**player.current, "ready_event": asyncio.Event(), "is_fetching": False, "stream_url": None, "error": None}
		player.queue.appendleft(loop_track)
		player.current = None
		vc.stop()  # after_playingが発火し play_next_song が即座に起動する
		await replay_embed(ctx)
	except Exception as e:
		await exception_embed(ctx, "replay", e)
		logger.error(f"replayコマンド実行エラー: {e}")

if __name__ == "__main__":
	bot.run(_DISCORD_TOKEN, log_handler=None)