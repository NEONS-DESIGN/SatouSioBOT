import asyncio
import os
import random
import sys
import time
from collections.abc import Coroutine
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from module.embed import (
	already_in_channel_embed, already_paused_embed, already_playing_embed,
	clear_queue_embed, empty_queue_embed, exception_embed, guild_only_embed, help_mention_embed,
	help_pages, invalid_argument_embed, invalid_clear_range_embed, leave_embed, loop_switch_embed,
	move_success_embed, not_connect_bot_embed, not_playing_embed, pause_embed, permission_error_embed,
	purge_complete_embed, queue_list_pages, replay_embed, resume_embed, shuffle_complete_embed,
	skip_music_embed, speed_set_embed, user_not_here_embed, volume_set_embed,
)
from module.logger import get_bot_logger, perf, setup_daily_logger
from module.music import (
	SPEED_MAX, SPEED_MIN, apply_speed, discard_player, get_player, play_music, requeue_track,
	server_music_data, shutdown_process_pool, spawn, warmup_process_pool,
)
from module.options import BASE_DIR
from module.setting import NotBotAdmin, setup_setting_commands
from module.sqlite import close_db, init_db, save_guild_setting

logger = get_bot_logger()

# ページネーションの操作受付時間(秒)
PAGINATOR_TIMEOUT = 120
# 「最初へ」「最後へ」ボタンを表示する最小ページ数
PAGINATOR_JUMP_MIN_PAGES = 3
# /purge の最大削除件数
PURGE_MAX = 50
# /vol で指定できる音量(%)の範囲
VOLUME_MIN, VOLUME_MAX = 1, 200

# ==========================================
# Bot本体
# ==========================================
class SatouSioBot(commands.Bot):
	def __init__(self) -> None:
		intents = discord.Intents.default()
		intents.message_content = True
		super().__init__(command_prefix="/", intents=intents, help_command=None)
		# メンション時のヘルプ案内に使う /help のコマンドメンション (同期後に </help:ID> へ更新)
		self.help_mention = "`/help`"

	async def setup_hook(self) -> None:
		"""起動時の非同期セットアップ: DB初期化 → 抽出ワーカー準備(並行) → 設定コマンド登録 → スラッシュコマンド同期"""
		await init_db()
		spawn(warmup_process_pool(), name="warmup_process_pool")
		setup_setting_commands(self)
		synced = await self.tree.sync()
		if help_command := discord.utils.get(synced, name="help"):
			self.help_mention = help_command.mention
		logger.info("スラッシュコマンドを同期しました。")

	async def close(self) -> None:
		"""終了時にプレイヤー・DB接続・プロセスプールを解放する"""
		logger.info("シャットダウン処理を開始します...")
		try:
			for guild_id in list(server_music_data):
				discard_player(guild_id)
			await close_db()
			shutdown_process_pool()
		finally:
			await super().close()
		logger.info("リソースを解放しました。")

bot = SatouSioBot()

# ==========================================
# ページネーション UI
# ==========================================
class SimplePaginator(discord.ui.View):
	"""
	複数のEmbedをページ送りで表示するUI。
	- PAGINATOR_JUMP_MIN_PAGES 未満の場合は「最初へ」「最後へ」ボタンを非表示にする
	- タイムアウト後はすべてのボタンを無効化する (message を外から設定しておくこと)
	"""
	def __init__(self, embeds: list[discord.Embed]) -> None:
		super().__init__(timeout=PAGINATOR_TIMEOUT)
		self.embeds = embeds
		self.current_page = 0
		self.message: discord.Message | None = None
		self._has_jump_buttons = len(embeds) >= PAGINATOR_JUMP_MIN_PAGES
		if self._has_jump_buttons:
			self._sync_buttons()
		else:
			self.remove_item(self.first_button)
			self.remove_item(self.last_button)

	def _sync_buttons(self) -> None:
		"""現在ページに応じて両端ボタンの有効/無効を更新する"""
		self.first_button.disabled = self.current_page == 0
		self.last_button.disabled = self.current_page == len(self.embeds) - 1

	async def _show(self, interaction: discord.Interaction, page: int) -> None:
		"""指定ページ (範囲外は丸める) を表示する"""
		self.current_page = min(max(page, 0), len(self.embeds) - 1)
		if self._has_jump_buttons:
			self._sync_buttons()
		await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

	async def on_timeout(self) -> None:
		"""タイムアウト時: 全ボタンをグレーアウトして編集"""
		for child in self.children:
			child.disabled = True
		if self.message:
			try:
				await self.message.edit(view=self)
			except discord.HTTPException:
				pass

	@discord.ui.button(label="❚◀", style=discord.ButtonStyle.primary)
	async def first_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
		await self._show(interaction, 0)

	@discord.ui.button(label="◀", style=discord.ButtonStyle.success)
	async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
		await self._show(interaction, self.current_page - 1)

	@discord.ui.button(label="▶", style=discord.ButtonStyle.success)
	async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
		await self._show(interaction, self.current_page + 1)

	@discord.ui.button(label="▶❚", style=discord.ButtonStyle.primary)
	async def last_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
		await self._show(interaction, len(self.embeds) - 1)

async def send_pages(ctx: commands.Context, embeds: list[discord.Embed], *, ephemeral: bool = False) -> None:
	"""Embed が1枚ならそのまま、複数ならページネーション付きで送信する"""
	if len(embeds) == 1:
		await ctx.send(embed=embeds[0], ephemeral=ephemeral)
		return
	view = SimplePaginator(embeds)
	view.message = await ctx.send(embed=embeds[0], view=view, ephemeral=ephemeral)

# ==========================================
# イベントハンドラ
# ==========================================
@bot.event
async def on_ready() -> None:
	activity = discord.Activity(type=discord.ActivityType.playing, name="音楽再生BOTです。 /help")
	await bot.change_presence(activity=activity, status=discord.Status.online)
	logger.info(f"{bot.user.name} (ID: {bot.user.id}) としてログインしました。")

@bot.event
async def on_message(message: discord.Message) -> None:
	if message.author.bot:
		return
	if bot.user in message.mentions:
		await help_mention_embed(message, bot.help_mention)
	await bot.process_commands(message)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
	"""BotがVCから切断された際のリソースクリーンアップ"""
	if member.id != bot.user.id:
		return
	# 切断直後に新しい接続が始まっている場合 (/p による再接続) は、新しいプレイヤーを破棄しない
	if member.guild.voice_client is not None:
		return
	if before.channel is not None and after.channel is None and member.guild.id in server_music_data:
		discard_player(member.guild.id)
		logger.info(f"[CLEANUP] ギルド {member.guild.id} からBotが切断されたため、リソースをクリーンアップしました。")

def _describe_input_error(error: Exception) -> str:
	"""引数エラーを利用者向けの日本語説明に変換する"""
	if isinstance(error, commands.RangeError):
		return f"{error.minimum}〜{error.maximum} の範囲で指定してください。(入力値: {error.value})"
	if isinstance(error, commands.MissingRequiredArgument):
		return f"引数 `{error.param.name}` を指定してください。"
	if isinstance(error, commands.BadArgument):
		return "引数の形式が正しくありません。"
	return "コマンドの使い方は /help を確認してください。"

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
	"""全コマンド共通のエラーハンドラ。利用者起因のエラーは案内を表示し、想定外のエラーはログに記録する"""
	# スラッシュ実行時は HybridCommandError に包まれて届く
	if isinstance(error, commands.HybridCommandError):
		error = error.original
	if isinstance(error, commands.CommandNotFound):
		return
	if isinstance(error, (commands.MissingPermissions, NotBotAdmin)):
		await permission_error_embed(ctx)
	elif isinstance(error, commands.NoPrivateMessage):
		await guild_only_embed(ctx)
	elif isinstance(error, (commands.UserInputError, app_commands.TransformerError)):
		await invalid_argument_embed(ctx, _describe_input_error(error))
	elif isinstance(error, (commands.CommandInvokeError, app_commands.CommandInvokeError)):
		name = ctx.command.qualified_name if ctx.command else "unknown"
		logger.error(f"{name} コマンド実行エラー", exc_info=error.original)
		await exception_embed(ctx, name, error.original)
	else:
		logger.error(f"コマンドエラー: {error!r}")

# ==========================================
# スラッシュコマンド群
# ==========================================
async def _timed(label: str, coro: Coroutine[Any, Any, Any]) -> Any:
	"""coro を実行し、所要時間を perf ログに出す"""
	t = time.perf_counter()
	result = await coro
	perf(label, (time.perf_counter() - t) * 1000)
	return result

async def _connect_voice(channel: discord.VoiceChannel | discord.StageChannel) -> discord.VoiceProtocol:
	"""channel に接続する。同時要求で既に接続済みになっていれば既存の接続を返す"""
	try:
		return await channel.connect()
	except discord.ClientException:
		if channel.guild.voice_client is not None:
			return channel.guild.voice_client
		raise

@bot.hybrid_command(name="help", description="コマンドやコマンドの使い方を表示します。")
async def bot_help(ctx: commands.Context) -> None:
	await ctx.defer(ephemeral=True)
	await send_pages(ctx, help_pages(), ephemeral=True)

@bot.hybrid_command(name="p", description="曲を再生します（YouTube/ニコニコ/SoundCloudなど対応）")
@app_commands.describe(query="曲のURLかタイトルを入力してください。")
@app_commands.rename(query="urlか曲名")
@commands.guild_only()
async def bot_play(ctx: commands.Context, *, query: str) -> None:
	t_request = time.perf_counter()
	voice = ctx.author.voice
	if not voice or not voice.channel:
		return await user_not_here_embed(ctx)
	# 応答の保留・VC 接続/移動・情報取得を並行して進める (完了は play_music 内で待つ)
	defer_task = spawn(_timed("応答保留(defer)", ctx.defer()), name="defer")
	vc = ctx.guild.voice_client
	voice_task = None
	if vc is None:
		voice_task = spawn(_timed("VC接続", _connect_voice(voice.channel)), name="vc_connect")
	elif vc.channel != voice.channel:
		voice_task = spawn(_timed("VC移動", vc.move_to(voice.channel)), name="vc_move")
	await play_music(ctx, query, defer_task=defer_task, voice_task=voice_task, t_request=t_request)

@bot.hybrid_command(name="vol", description=f"音量を設定します({VOLUME_MIN}~{VOLUME_MAX})。")
@app_commands.describe(volume="音量を入力してください。")
@commands.guild_only()
async def bot_volume(ctx: commands.Context, volume: commands.Range[int, VOLUME_MIN, VOLUME_MAX]) -> None:
	await ctx.defer()
	target_vol = volume / 100
	vc = ctx.guild.voice_client
	if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
		vc.source.volume = target_vol
	# プレイヤーが存在する場合のみ音量キャッシュを更新する (無ければ次回再生時にDBから読む)
	if player := server_music_data.get(ctx.guild.id):
		player.volume = target_vol
	await save_guild_setting(ctx.guild.id, "volume", target_vol)
	await volume_set_embed(ctx, volume)

@bot.hybrid_command(name="speed", description=f"再生速度を変更します({SPEED_MIN}~{SPEED_MAX}倍)。退出・再生終了で1倍に戻ります。")
@app_commands.describe(rate="再生速度の倍率を入力してください。", keep_pitch="音の高さを維持するか (既定: 維持する)")
@app_commands.rename(rate="倍率", keep_pitch="ピッチ維持")
@commands.guild_only()
async def bot_speed(ctx: commands.Context, rate: commands.Range[float, SPEED_MIN, SPEED_MAX], keep_pitch: bool = True) -> None:
	vc = ctx.guild.voice_client
	if not vc or not vc.is_connected():
		return await not_connect_bot_embed(ctx)
	await ctx.defer()
	# VC 接続中のプレイヤーに保持し、退出・切断・再生終了でプレイヤーごと破棄させる (移動では維持される)
	player = get_player(ctx.guild.id)
	player.speed = round(rate, 2)
	player.keep_pitch = keep_pitch
	await apply_speed(ctx.guild, player)
	await speed_set_embed(ctx, player.speed, player.keep_pitch)

@bot.hybrid_command(name="loop", description="現在入っているキューをループ再生します。もう一度実行するとループ解除します。")
@commands.guild_only()
async def bot_loop(ctx: commands.Context) -> None:
	await ctx.defer()
	player = get_player(ctx.guild.id)
	player.loop = not player.loop
	await loop_switch_embed(ctx, "有効" if player.loop else "無効")

@bot.hybrid_command(name="sh", description="キューの中身をシャッフルします。")
@commands.guild_only()
async def bot_shuffle(ctx: commands.Context) -> None:
	await ctx.defer()
	player = server_music_data.get(ctx.guild.id)
	if not player or not player.queue:
		return await empty_queue_embed(ctx)
	tracks = list(player.queue)
	random.shuffle(tracks)
	player.queue.clear()
	player.queue.extend(tracks)
	player.prefetch()
	await shuffle_complete_embed(ctx)

@bot.hybrid_command(name="skip", description="現在の曲をスキップします。")
@commands.guild_only()
async def bot_skip(ctx: commands.Context) -> None:
	await ctx.defer()
	vc = ctx.guild.voice_client
	if vc and (vc.is_playing() or vc.is_paused()):
		# 停止すると再生終了コールバック経由で次の曲へ進む
		vc.stop()
		await skip_music_embed(ctx)
	else:
		await not_playing_embed(ctx)

@bot.hybrid_command(name="move", description="Botを自分のいるボイスチャンネルに移動させます。")
@commands.guild_only()
async def bot_move(ctx: commands.Context) -> None:
	if not ctx.author.voice or not ctx.author.voice.channel:
		return await user_not_here_embed(ctx)
	vc = ctx.guild.voice_client
	if not vc:
		return await not_connect_bot_embed(ctx)
	if vc.channel == ctx.author.voice.channel:
		return await already_in_channel_embed(ctx)
	await ctx.defer()
	await vc.move_to(ctx.author.voice.channel)
	await move_success_embed(ctx, ctx.author.voice.channel)

@bot.hybrid_command(name="leave", description="BOTを退出させます。")
@commands.guild_only()
async def bot_leave(ctx: commands.Context) -> None:
	vc = ctx.guild.voice_client
	if not vc:
		return await not_connect_bot_embed(ctx)
	await ctx.defer()
	# 先にプレイヤーを破棄し、停止で発火する次曲処理が動かないようにする
	discard_player(ctx.guild.id)
	vc.stop()
	await vc.disconnect()
	await leave_embed(ctx)

@bot.hybrid_command(name="purge", description="チャンネルのメッセージを一括削除します。")
@app_commands.describe(limit=f"削除する件数を指定(1~{PURGE_MAX}件)。未指定時は{PURGE_MAX}件。")
@app_commands.rename(limit="件数")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def bot_purge(ctx: commands.Context, limit: commands.Range[int, 1, PURGE_MAX] = PURGE_MAX) -> None:
	await ctx.defer(ephemeral=True)
	deleted = await ctx.channel.purge(limit=limit)
	await purge_complete_embed(ctx, len(deleted))

@bot.hybrid_command(name="qlist", description="現在のキューに入っている曲のリストを表示します。")
@commands.guild_only()
async def bot_qlist(ctx: commands.Context) -> None:
	await ctx.defer()
	player = server_music_data.get(ctx.guild.id)
	if not player or not player.queue:
		return await empty_queue_embed(ctx)
	await send_pages(ctx, queue_list_pages(player.queue))

@bot.hybrid_command(name="pause", description="現在再生中の曲を一時停止します。")
@commands.guild_only()
async def bot_pause(ctx: commands.Context) -> None:
	await ctx.defer()
	vc = ctx.guild.voice_client
	if not vc or not vc.is_connected():
		return await not_connect_bot_embed(ctx)
	if vc.is_paused():
		return await already_paused_embed(ctx)
	if not vc.is_playing():
		return await not_playing_embed(ctx)
	vc.pause()
	await pause_embed(ctx)

@bot.hybrid_command(name="resume", description="一時停止中の曲を再開します。")
@commands.guild_only()
async def bot_resume(ctx: commands.Context) -> None:
	await ctx.defer()
	vc = ctx.guild.voice_client
	if not vc or not vc.is_connected():
		return await not_connect_bot_embed(ctx)
	if vc.is_playing():
		return await already_playing_embed(ctx)
	if not vc.is_paused():
		return await not_playing_embed(ctx)
	vc.resume()
	await resume_embed(ctx)

@bot.hybrid_command(name="clear", description="キューに入っている曲を削除します。")
@app_commands.describe(start="削除する件数、または削除を開始する番号", end="削除を終了する番号(範囲指定時)")
@app_commands.rename(start="件数または開始番号", end="終了番号")
@commands.guild_only()
async def bot_clear(ctx: commands.Context, start: int | None = None, end: int | None = None) -> None:
	await ctx.defer()
	player = server_music_data.get(ctx.guild.id)
	if not player or not player.queue:
		return await empty_queue_embed(ctx)
	tracks = list(player.queue)
	q_len = len(tracks)
	if start is None and end is None:
		# 引数なし: 全削除
		begin, stop = 0, q_len
	elif end is None:
		# 引数1つ: 先頭から start 件削除
		if start < 1:
			return await invalid_clear_range_embed(ctx)
		begin, stop = 0, min(start, q_len)
	else:
		# 引数2つ: start〜end の範囲を削除 (1-indexed)
		if start is None or start < 1 or end < start or start > q_len:
			return await invalid_clear_range_embed(ctx)
		begin, stop = start - 1, min(end, q_len)
	removed = tracks[begin:stop]
	del tracks[begin:stop]
	for track in removed:
		if (task := track["fetch_task"]) is not None and not task.done():
			task.cancel()
	player.queue.clear()
	player.queue.extend(tracks)
	player.prefetch()
	await clear_queue_embed(ctx, len(removed))

@bot.hybrid_command(name="replay", description="現在再生中の曲を最初から再生し直します。")
@commands.guild_only()
async def bot_replay(ctx: commands.Context) -> None:
	await ctx.defer()
	vc = ctx.guild.voice_client
	if not vc or not vc.is_connected():
		return await not_connect_bot_embed(ctx)
	player = server_music_data.get(ctx.guild.id)
	if not player or not player.current or not (vc.is_playing() or vc.is_paused()):
		return await not_playing_embed(ctx)
	# 現在の曲をストリームURLを再解決する形でキュー先頭に積み直し、停止して次曲処理に再生させる
	player.queue.appendleft(requeue_track(player.current))
	player.current = None
	player.prefetch()
	vc.stop()
	await replay_embed(ctx)

# ==========================================
# 起動
# ==========================================
async def _run_bot(token: str) -> None:
	"""Bot を起動し、終了時 (Ctrl+C 含む) に close() まで確実に実行する"""
	async with bot:
		await bot.start(token)

def _run_with_fast_loop(coro: Coroutine[Any, Any, None]) -> None:
	"""OS に応じた高速イベントループ (win32: winloop / その他: uvloop) で実行する。無ければ標準ループ"""
	runner = asyncio.run
	try:
		if sys.platform == "win32":
			import winloop
			runner = winloop.run
		else:
			import uvloop
			runner = uvloop.run
	except ImportError:
		pass
	runner(coro)

def main() -> None:
	"""環境変数・ロガーを準備して Bot を起動する"""
	load_dotenv(BASE_DIR / ".env")
	token = os.getenv("discord_api", "")
	setup_daily_logger()
	if not token:
		logger.error(".env に discord_api (Botトークン) が設定されていません。")
		sys.exit(1)
	try:
		_run_with_fast_loop(_run_bot(token))
	except KeyboardInterrupt:
		logger.info("Ctrl+C を受け付けたため終了しました。")

if __name__ == "__main__":
	main()
