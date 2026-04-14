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

# OSを自動判定して、最適な高速イベントループを適用する
if sys.platform == 'win32':
    try:
        import winloop
        asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
    except ImportError:
        pass # インストールされていない場合は標準のループを使用
else: # Unix系OSはuvloopを使用
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        pass

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
		self.message = None
		# ページ数が3ページ未満の場合は「最初へ」「最後へ」ボタンをUIから削除する
		if len(self.embeds) < 3:
			self.remove_item(self.first_button)
			self.remove_item(self.last_button)
		else:
			# 送信時の初期状態としてボタンの有効/無効を判定しておく
			self._update_button_states()
	def _update_button_states(self):
		"""現在のページに応じて「最初へ」「最後へ」ボタンの有効・無効を切り替える"""
		if len(self.embeds) >= 3:
			# 現在のページが0(最初)なら「最初へ」ボタンを無効化
			self.first_button.disabled = (self.current_page == 0)
			# 現在のページが最後のページなら「最後へ」ボタンを無効化
			self.last_button.disabled = (self.current_page == len(self.embeds) - 1)
	async def update_view(self, interaction: discord.Interaction):
		# 画面を更新する直前にボタンの状態を再計算する
		self._update_button_states()
		await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)
	async def on_timeout(self):
		"""タイムアウト(180秒)時に自動で実行される処理"""
		# View内のすべてのボタン要素を無効化(グレーアウト)する
		for child in self.children:
			child.disabled = True
		# 保持しているメッセージオブジェクトを更新して、無効化状態をDiscord上に反映する
		if self.message:
			try:
				await self.message.edit(view=self)
			except Exception:
				# メッセージが既に削除されている場合などのエラーを無視する
				pass
	@discord.ui.button(label="◀◀", style=discord.ButtonStyle.primary)
	async def first_button(self, interaction: discord.Interaction, button: discord.ui.Button):
		self.current_page = 0
		await self.update_view(interaction)
	@discord.ui.button(label="◀", style=discord.ButtonStyle.success)
	async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
		self.current_page = (self.current_page - 1) % len(self.embeds)
		await self.update_view(interaction)
	@discord.ui.button(label="▶", style=discord.ButtonStyle.success)
	async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
		self.current_page = (self.current_page + 1) % len(self.embeds)
		await self.update_view(interaction)
	@discord.ui.button(label="▶▶", style=discord.ButtonStyle.primary)
	async def last_button(self, interaction: discord.Interaction, button: discord.ui.Button):
		self.current_page = len(self.embeds) - 1
		await self.update_view(interaction)

# ==========================================
# イベントハンドラ
# ==========================================
@bot.event
async def on_ready():
	# Botの起動時にデータベースの初期化を行う
	await init_db()
	activity = discord.Activity(type=discord.ActivityType.playing, name="音楽再生BOTです。 /help", url="https://github.com/SatouSio/SatouSioBOT", details="コマンドの使い方は/helpで確認できます。", state="音楽再生中", assets={"large_image": "https://raw.githubusercontent.com/NEONS-DESIGN/SatouSioBOT/refs/heads/main/img/logo.png", "large_text": "SatouSioBOT"})
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

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
	# Bot自身が不意にVCから切断された場合のクリーンアップ処理
	if member.id == bot.user.id and before.channel is not None and after.channel is None:
		guild_id = member.guild.id
		if guild_id in server_music_data:
			server_music_data[guild_id].cleanup()
			del server_music_data[guild_id]
			logger.info(f"[CLEANUP] ギルド {guild_id} からBotが切断されたため、リソースをクリーンアップしました。")

# ==========================================
# スラッシュコマンド群
# ==========================================
@bot.hybrid_command(name="help", description="コマンドやコマンドの使い方を表示します。")
async def bot_help(ctx: commands.Context):
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
async def bot_play(ctx: commands.Context, *, query: str):
	if not ctx.author.voice:
		return await user_not_here_embed(ctx)
	await ctx.defer()
	try:
		if not ctx.guild.voice_client:
			player = await ensure_guild_data(ctx.guild.id, bot)
			player.voice_client = await ctx.author.voice.channel.connect()
		elif ctx.guild.voice_client.channel != ctx.author.voice.channel:
			await ctx.guild.voice_client.move_to(ctx.author.voice.channel)
			player = await ensure_guild_data(ctx.guild.id, bot)
			player.voice_client = ctx.guild.voice_client
		# メイン処理へ。がんばえ～～～～
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

@bot.hybrid_command(name="loop", description="現在入っているキューをループ再生します。もう一度実行するとループ解除します。")
@commands.guild_only()
async def bot_loop(ctx: commands.Context):
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
async def bot_shuffle(ctx: commands.Context):
	await ctx.defer()
	try:
		player = await ensure_guild_data(ctx.guild.id, bot)
		queue = player.queue
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
		# ボイスチャンネル退出前に再生中の音楽を明示的に強制停止する
		if ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused():
			ctx.guild.voice_client.stop()
		await ctx.guild.voice_client.disconnect()
		# ギルド単位のデータを完全破棄する
		if guild_id in server_music_data:
			server_music_data[guild_id].cleanup()
			del server_music_data[guild_id]
		await leave_embed(ctx)
	except Exception as e:
		await exception_embed(ctx, "leave", e)
		logger.error(f"leaveコマンド実行エラー: {e}")

@bot.hybrid_command(name="purge", description="チャンネルのメッセージを一括削除します。")
@app_commands.describe(limit="削除する件数を指定(1~50件)。未指定時は50件。")
@app_commands.rename(limit="件数")
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

@bot.hybrid_command(name="qlist", description="現在のキューに入っている曲のリストを表示します。")
@commands.guild_only()
async def bot_qlist(ctx: commands.Context):
	await ctx.defer()
	try:
		player = await ensure_guild_data(ctx.guild.id, bot)
		queue = player.queue
		if not queue:
			return await empty_queue_embed(ctx)
		# embed.pyで作成したページ生成関数を呼び出し
		embeds = await queue_list_pages(queue)
		# 1ページしかない場合はそのまま送信、複数ある場合はボタン付きページネーターを使用
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
async def bot_pause(ctx: commands.Context):
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
async def bot_resume(ctx: commands.Context):
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
async def bot_clear(ctx: commands.Context, start: int = None, end: int = None):
	await ctx.defer()
	try:
		player = await ensure_guild_data(ctx.guild.id, bot)
		queue = player.queue
		if not queue:
			return await empty_queue_embed(ctx)
		queue_length = len(queue)
		deleted_count = 0
		if start is None and end is None:
			# 引数なし: 全部削除
			deleted_count = len(queue)
			queue.clear()
		elif start is not None and end is None:
			# 引数1つ: 先頭から指定された数まで削除
			if start < 1:
				return await invalid_clear_range_embed(ctx)
			delete_end = min(start, queue_length)
			deleted_count = delete_end
			del queue[:delete_end]
		elif start is not None and end is not None:
			# 引数2つ: 範囲削除 (例: /clear 4 8)
			if start < 1 or end < start:
				return await invalid_clear_range_embed(ctx)
			# リストは0から始まるため、人間が指定した番号(1〜)をプログラム用のインデックス(0〜)に変換
			slice_start = start - 1
			slice_end = min(end, queue_length)
			if slice_start >= queue_length:
				return await invalid_clear_range_embed(ctx)
			deleted_count = slice_end - slice_start
			del queue[slice_start:slice_end]
		await clear_queue_embed(ctx, deleted_count)
	except Exception as e:
		await exception_embed(ctx, "clear", e)
		logger.error(f"clearコマンド実行エラー: {e}")

@bot.hybrid_command(name="replay", description="現在再生中の曲を最初から再生し直します。")
@commands.guild_only()
async def bot_replay(ctx: commands.Context):
	await ctx.defer()
	try:
		vc = ctx.guild.voice_client
		if not vc or not vc.is_connected():
			return await bot_not_in_vc_embed(ctx)
		player = await ensure_guild_data(ctx.guild.id, bot)
		# 現在再生中の曲データが存在するか確認
		if not player.current:
			return await not_playing_embed(ctx)
		# 現在の曲をキューの先頭（インデックス0）に挿入し直す
		current_track = player.current
		player.queue.insert(0, current_track)
		# ループ機能による重複追加を回避するため、一時的にcurrentを空にする
		player.current = None
		# 再生を強制停止する（自動的にplay_next_songが発火し、先頭に入れた曲が即座に再生される）
		vc.stop()
		await replay_embed(ctx)
	except Exception as e:
		await exception_embed(ctx, "replay", e)
		logger.error(f"replayコマンド実行エラー: {e}")

if __name__ == "__main__":
	bot.run(discordToken, log_handler=None)