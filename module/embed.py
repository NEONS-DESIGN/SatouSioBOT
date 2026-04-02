import discord
from discord.ext import commands
from module.color import Embed
from module.other import play_time

# カラーコード定数
RED = Embed.RED
GREEN = Embed.LIGHT_GREEN
BLUE = Embed.BLUE
YELLOW = Embed.YELLOW

# ==========================================
# 共通のEmbed送信ヘルパー（一本化ロジック）
# ==========================================
async def _send_msg(ctx: commands.Context, title: str, description: str = None, color: int = GREEN, ephemeral: bool = False) -> discord.Message:
	"""シンプルなEmbedを生成し送信する内部関数。戻り値としてMessageオブジェクトを返す。"""
	embed = discord.Embed(title=title, description=description, color=color)
	return await ctx.send(embed=embed, ephemeral=ephemeral)

def _build_music_embed(ctx: commands.Context, info: dict, embed_title: str) -> discord.Embed:
	"""楽曲・プレイリスト追加用Embedの共通ベースを生成する内部関数"""
	title = info.get('title', 'Unknown Title')
	url = info.get('webpage_url', info.get('url', ''))
	thumbnail = info.get('thumbnail')

	embed = discord.Embed(title=embed_title, color=BLUE)

	# タイトルをリンク付きで表示
	field_value = f"[{title}]({url})"
	if len(field_value) > 1024: field_value = title[:1024]

	name_label = "プレイリスト名" if "プレイリスト" in embed_title else "タイトル"
	embed.add_field(name=name_label, value=field_value, inline=False)

	icon_url = ctx.author.display_avatar.url if ctx.author.display_avatar else None
	embed.set_footer(text=f"Requested by: {ctx.author.display_name}", icon_url=icon_url)

	if thumbnail:
		embed.set_image(url=thumbnail)

	return embed

# ==========================================
# ヘルプページ生成
# ==========================================
def help_pages():
	"""ヘルプ画面のEmbedリストを生成する。"""
	embed1 = discord.Embed(title="📖 コマンドヘルプ #1", color=GREEN)
	embed1.add_field(name="/p [URL・タイトル]", value="音楽を再生します（YouTube/ニコニコ/SoundCloud対応）。", inline=False)
	embed1.add_field(name="/skip", value="再生中の曲をスキップします。", inline=False)
	embed1.add_field(name="/vol [1-200]", value="再生音量を変更し、設定を保存します。", inline=False)
	embed1.add_field(name="/move", value="BOTを自分のいるボイスチャンネルへ移動させます。", inline=False)
	embed1.add_field(name="/loop", value="キューのループ再生を切り替えます。", inline=False)

	embed2 = discord.Embed(title="📖 コマンドヘルプ #2", color=GREEN)
	embed2.add_field(name="/sh", value="キューの中身をシャッフルします。", inline=False)
	embed2.add_field(name="/leave", value="BOTをボイスチャンネルから退出させ、キューをクリアします。", inline=False)
	embed2.add_field(name="/purge [件数]", value="チャンネルのメッセージを一括削除します（管理権限が必要）。", inline=False)

	return [embed1, embed2]

async def help_mention_embed(message: discord.Message):
	"""メンション受信時のヘルプ案内通知"""
	embed = discord.Embed(description="助けが必要ですか？\n必要な場合は、</help:1140613857224687616> コマンドを実行してください。", color=GREEN)
	await message.reply(embed=embed)

# ==========================================
# 通知・成功系Embed
# ==========================================
async def move_channel_embed(ctx: commands.Context):
	await _send_msg(ctx, "🚚 チャンネルを移動しました。", color=GREEN)

async def move_success_embed(ctx: commands.Context, channel: discord.VoiceChannel):
	await _send_msg(ctx, "🚚 チャンネル移動", f"**{channel.name}** に移動しました。", BLUE)

async def leave_embed(ctx: commands.Context):
	await _send_msg(ctx, "👋 退出しました。またね！", color=GREEN)

async def skip_music_embed(ctx: commands.Context):
	await _send_msg(ctx, "⏭️ 曲をスキップしました。", color=GREEN)

async def play_completed_embed(ctx: commands.Context):
	await _send_msg(ctx, "✅ 全てのトラックの再生が終了しました。", color=GREEN)

async def loop_switch_embed(ctx: commands.Context, state: str):
	await _send_msg(ctx, f"🔁 ループ再生を {state} にしました。", color=GREEN)

async def shuffle_complete_embed(ctx: commands.Context):
	await _send_msg(ctx, "🔀 キューをシャッフルしました。", color=GREEN)

async def volume_set_embed(ctx: commands.Context, volume: int):
	await _send_msg(ctx, f"🔊 再生音量を {volume}% に設定しました。", color=GREEN)

async def purge_complete_embed(ctx: commands.Context, count: int):
	await _send_msg(ctx, f"✅ {count} 件のメッセージを削除しました。", color=GREEN, ephemeral=True)

# --- キュー・プレイリスト追加 ---
async def playlist_added_embed(ctx: commands.Context, info: dict, count: int):
	embed = _build_music_embed(ctx, info, "📝 プレイリストをキューに追加")
	embed.add_field(name="追加曲数", value=f"{count} 曲", inline=True)
	await ctx.send(embed=embed)

async def queue_added_embed(ctx: commands.Context, info: dict, queue_pos: int):
	embed = _build_music_embed(ctx, info, "✅ キューに追加")
	duration = await play_time(info.get('duration', 0))
	embed.add_field(name="再生時間", value=duration, inline=True)
	embed.add_field(name="待機数", value=f"{queue_pos} 曲", inline=True)
	await ctx.send(embed=embed)

# ==========================================
# エラー・警告系Embed
# ==========================================
async def not_connect_bot_embed(ctx: commands.Context):
	await _send_msg(ctx, "ℹ️ BOTがボイスチャンネルに接続していません。", color=RED)

async def bot_not_in_vc_embed(ctx: commands.Context):
	await not_connect_bot_embed(ctx)

async def user_not_here_embed(ctx: commands.Context):
	await _send_msg(ctx, "⚠️ ボイスチャンネルに接続してから実行してください。", color=RED)

async def already_in_channel_embed(ctx: commands.Context):
	await _send_msg(ctx, "⚠️ 通知", "既にそのチャンネルに接続しています。", YELLOW)

async def not_playing_embed(ctx: commands.Context):
	await _send_msg(ctx, "🎵 現在、何も再生されていません。", color=RED)

async def playing_now_embed(ctx: commands.Context):
	await _send_msg(ctx, "🚫 再生中はこの操作を行えません。", color=RED)

async def empty_queue_embed(ctx: commands.Context):
	await _send_msg(ctx, "📝 キューが空です。曲を追加してください。", color=RED)

async def none_result_embed(ctx: commands.Context):
	await _send_msg(ctx, "🔎 検索結果が見つかりませんでした。", color=RED)

async def playback_error_embed(ctx: commands.Context, title: str):
	await _send_msg(ctx, "⚠️ 再生エラー", f"再生中にエラーが発生しました: {title}\n次の曲へスキップします。", RED)

async def load_error_embed(ctx: commands.Context, error: Exception):
	await _send_msg(ctx, "⚠️ 読み込みエラー", f"読み込みに失敗しました:\n```py\n{error}\n```", RED)

async def skip_error_embed(ctx: commands.Context, title: str):
	await _send_msg(ctx, "⚠️ スキップ", f"`{title}` の読み込みに失敗したためスキップします。", RED)

async def exception_embed(ctx: commands.Context, command_name: str, error: Exception):
	await _send_msg(ctx, f"❌ エラーが発生しました ({command_name})", f"管理者にお問い合わせください。\n```py\n{error}\n```", RED)

async def music_info_fallback_embed(ctx: commands.Context, title: str):
	await _send_msg(ctx, "🎵 再生中", f"{title[:50]}...\n(詳細情報の表示に失敗しました)", RED)

async def preparing_audio_embed(ctx: commands.Context) -> discord.Message:
	return await _send_msg(ctx, "⏳ 準備中", "音源を準備しています...", YELLOW)

# ==========================================
# 設定コマンド系Embed
# ==========================================
async def setting_help_embed(ctx: commands.Context):
	await _send_msg(ctx, "⚠️ 通知", "サブコマンドを指定してください。\n例: `/setting limit playlist 20`", YELLOW)

async def permission_error_embed(ctx: commands.Context):
	await _send_msg(ctx, "❌ 権限エラー", "このコマンドを実行する権限がありません。", RED)

async def admin_added_embed(ctx: commands.Context, user: discord.Member):
	await _send_msg(ctx, "✅ 権限追加", f"{user.mention} にBot操作権限を付与しました。", GREEN)

async def admin_removed_embed(ctx: commands.Context, user: discord.Member):
	await _send_msg(ctx, "✅ 権限剥奪", f"{user.mention} のBot操作権限を剥奪しました。", GREEN)

async def limit_range_error_embed(ctx: commands.Context):
	await _send_msg(ctx, "⚠️ 範囲エラー", "1から50の間で指定してください。", YELLOW)

async def limit_updated_embed(ctx: commands.Context, target: str, limit: int):
	await _send_msg(ctx, "✅ 設定更新", f"{target}を **{limit}** 曲に設定しました。", GREEN)

# ==========================================
# コントロール・キュー操作系Embed
# ==========================================
async def pause_embed(ctx: commands.Context):
	await _send_msg(ctx, "⏸️ 一時停止", "再生を一時停止しました。", YELLOW)

async def resume_embed(ctx: commands.Context):
	await _send_msg(ctx, "▶️ 再生再開", "再生を再開しました。", GREEN)

async def already_paused_embed(ctx: commands.Context):
	await _send_msg(ctx, "⚠️ 通知", "既に一時停止中です。", YELLOW)

async def already_playing_embed(ctx: commands.Context):
	await _send_msg(ctx, "⚠️ 通知", "既に再生中です。", YELLOW)

async def clear_queue_embed(ctx: commands.Context, count: int):
	await _send_msg(ctx, "🗑️ キュー削除", f"**{count}** 曲をキューから削除しました。", GREEN)

async def invalid_clear_range_embed(ctx: commands.Context):
	await _send_msg(ctx, "⚠️ 範囲エラー", "正しい数値を指定してください。\n例: `/clear 5` または `/clear 4 8`", YELLOW)

async def queue_list_pages(queue: list) -> list:
	"""キューのリストをページ分けしたEmbedのリストを生成する"""
	if not queue:
		return [discord.Embed(title="📝 キューリスト", description="キューは空です。", color=BLUE)]

	embeds = []
	tracks_per_page = 10
	total_pages = (len(queue) - 1) // tracks_per_page + 1

	for i in range(total_pages):
		embed = discord.Embed(title=f"📝 キューリスト ({i+1}/{total_pages}ページ)", color=BLUE)
		description = ""
		start_idx = i * tracks_per_page
		end_idx = start_idx + tracks_per_page

		for j, track in enumerate(queue[start_idx:end_idx], start=start_idx + 1):
			title = track.get('title', 'Unknown Title')
			if len(title) > 45:
				title = title[:42] + "..."

			duration_raw = track.get('duration', 0)
			duration = await play_time(duration_raw)
			description += f"**{j}.** {title} `[{duration}]`\n"

		embed.description = description
		embeds.append(embed)

	return embeds