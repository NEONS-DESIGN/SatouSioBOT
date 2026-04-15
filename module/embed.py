import collections
import discord
from discord.ext import commands

from module.color import Embed as EmbedColor
from module.utils import play_time, shorten_url

# カラー定数 (module/color.py の Embed クラスから参照)
_RED    = EmbedColor.RED
_GREEN  = EmbedColor.GREEN
_BLUE   = EmbedColor.BLUE
_YELLOW = EmbedColor.YELLOW

# 再生中サムネイルのフォールバック画像URL
_FALLBACK_THUMBNAIL = "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?q=80&w=1024&auto=format&fit=crop"


# ==========================================
# 内部ヘルパー
# ==========================================
async def _send(ctx: commands.Context, title: str, description: str | None = None, color: int = _GREEN, ephemeral: bool = False, edit_msg: discord.Message | None = None) -> discord.Message:
	"""
	シンプルなEmbedを送信または編集する共通関数。
	- edit_msgが渡された場合はそのメッセージを編集する
	- 編集に失敗した場合は新規送信にフォールバックする
	"""
	embed = discord.Embed(title=title, description=description, color=color)
	if edit_msg:
		try:
			return await edit_msg.edit(embed=embed)
		except (discord.NotFound, discord.HTTPException):
			pass
	return await ctx.send(embed=embed, ephemeral=ephemeral)

def _music_embed_base(ctx: commands.Context, info: dict, title: str) -> discord.Embed:
	"""
	楽曲・プレイリスト追加通知用Embedのベースを生成する内部関数。
	- タイトル・URL・サムネイル・フッターを共通でセットする
	"""
	track_title = info.get("title", "Unknown Title")
	url = info.get("webpage_url") or info.get("url", "")
	thumbnail = info.get("thumbnail")
	embed = discord.Embed(title=title, color=_BLUE)
	label = "プレイリスト名" if "プレイリスト" in title else "タイトル"
	field_value = f"[{track_title}]({url})" if url else track_title
	if len(field_value) > 1024:
		field_value = track_title[:1024]
	embed.add_field(name=label, value=field_value, inline=False)
	icon_url = ctx.author.display_avatar.url if ctx.author.display_avatar else None
	embed.set_footer(text=f"Requested by: {ctx.author.display_name}", icon_url=icon_url)
	if thumbnail:
		embed.set_image(url=thumbnail)
	return embed


# ==========================================
# ヘルプ
# ==========================================
def help_pages() -> list[discord.Embed]:
	"""
	ヘルプ画面を3ページのEmbedリストとして生成する。
	- Page1: 再生・基本操作
	- Page2: キュー・音量
	- Page3: BOT管理・設定
	"""
	p1 = discord.Embed(title="📖 コマンドヘルプ #1 (再生・基本操作)", color=_GREEN)
	p1.add_field(name="/help",             value="このヘルプ画面を表示します。", inline=False)
	p1.add_field(name="/p [URL・タイトル]", value="音楽を再生します（YouTube/ニコニコ/SoundCloudなど対応）。", inline=False)
	p1.add_field(name="/pause",            value="再生中の曲を一時停止します。", inline=False)
	p1.add_field(name="/resume",           value="一時停止中の曲の再生を再開します。", inline=False)
	p1.add_field(name="/replay",           value="現在再生中の曲を最初から再生し直します。", inline=False)
	p1.add_field(name="/skip",             value="再生中の曲をスキップします。", inline=False)

	p2 = discord.Embed(title="📖 コマンドヘルプ #2 (キュー・音量)", color=_GREEN)
	p2.add_field(name="/qlist",              value="現在のキューに入っている曲のリストを表示します。", inline=False)
	p2.add_field(name="/clear [開始] [終了]", value="キューの曲を削除します。引数なしで全件削除、範囲指定も可能です。", inline=False)
	p2.add_field(name="/loop",               value="キューのループ再生を切り替えます。", inline=False)
	p2.add_field(name="/sh",                 value="キューの中身をシャッフルします。", inline=False)
	p2.add_field(name="/vol [1-200]",        value="再生音量を変更し、設定を保存します。", inline=False)

	p3 = discord.Embed(title="📖 コマンドヘルプ #3 (BOT管理・設定)", color=_GREEN)
	p3.add_field(name="/move",                         value="BOTを自分のいるボイスチャンネルへ移動させます。", inline=False)
	p3.add_field(name="/leave",                        value="BOTをボイスチャンネルから退出させ、キューをクリアします。", inline=False)
	p3.add_field(name="/purge [件数]",                  value="チャンネルのメッセージを一括削除します（管理権限が必要）。", inline=False)
	p3.add_field(name="/setting admin [add/remove]",   value="BOT操作権限の付与・剥奪を行います。", inline=False)
	p3.add_field(name="/setting limit [queue/playlist]", value="上限(キュー・プレイリスト)の設定を行います。", inline=False)

	return [p1, p2, p3]

async def help_mention_embed(message: discord.Message) -> None:
	"""メンション受信時のヘルプ案内"""
	embed = discord.Embed(
		description="助けが必要ですか？\n必要な場合は、</help:1140613857224687616> コマンドを実行してください。",
		color=_GREEN,
	)
	await message.reply(embed=embed)


# ==========================================
# 通知・成功系
# ==========================================
async def move_success_embed(ctx: commands.Context, channel: discord.VoiceChannel) -> None:
	await _send(ctx, "🚚 チャンネル移動", f"**{channel.name}** に移動しました。", _BLUE)

async def leave_embed(ctx: commands.Context) -> None:
	await _send(ctx, "👋 退出しました。またね！")

async def skip_music_embed(ctx: commands.Context) -> None:
	await _send(ctx, "⏭️ 曲をスキップしました。")

async def play_completed_embed(ctx: commands.Context) -> None:
	await _send(ctx, "✅ 全てのトラックの再生が終了しました。")

async def loop_switch_embed(ctx: commands.Context, state: str) -> None:
	await _send(ctx, f"🔁 ループ再生を {state} にしました。")

async def shuffle_complete_embed(ctx: commands.Context) -> None:
	await _send(ctx, "🔀 キューをシャッフルしました。")

async def volume_set_embed(ctx: commands.Context, volume: int) -> None:
	await _send(ctx, f"🔊 再生音量を {volume}% に設定しました。")

async def purge_complete_embed(ctx: commands.Context, count: int) -> None:
	await _send(ctx, f"✅ {count} 件のメッセージを削除しました。", ephemeral=True)

async def replay_embed(ctx: commands.Context) -> None:
	await _send(ctx, "🔄 リプレイ", "現在の曲を最初から再生し直します。")

async def pause_embed(ctx: commands.Context) -> None:
	await _send(ctx, "⏸️ 一時停止", "再生を一時停止しました。", _YELLOW)

async def resume_embed(ctx: commands.Context) -> None:
	await _send(ctx, "▶️ 再生再開", "再生を再開しました。")

async def clear_queue_embed(ctx: commands.Context, count: int) -> None:
	await _send(ctx, "🗑️ キュー削除", f"**{count}** 曲をキューから削除しました。")


# ==========================================
# 楽曲追加・再生情報
# ==========================================
async def playlist_added_embed(ctx: commands.Context, info: dict, count: int, edit_msg: discord.Message | None = None,) -> None:
	"""プレイリストをキューに追加した際の通知Embed"""
	embed = _music_embed_base(ctx, info, "📝 プレイリストをキューに追加")
	embed.add_field(name="追加曲数", value=f"{count} 曲", inline=True)
	if edit_msg:
		try:
			await edit_msg.edit(embed=embed)
			return
		except (discord.NotFound, discord.HTTPException):
			pass
	await ctx.send(embed=embed)

async def queue_added_embed(ctx: commands.Context, info: dict, queue_pos: int) -> None:
	"""単曲をキューに追加した際の通知Embed"""
	embed = _music_embed_base(ctx, info, "✅ キューに追加")
	duration = await play_time(info.get("duration", 0))
	embed.add_field(name="再生時間", value=duration, inline=True)
	embed.add_field(name="待機数",   value=f"{queue_pos} 曲", inline=True)
	await ctx.send(embed=embed)

async def music_info_embed(ctx: commands.Context, player: any, queue_count: int, wait_msg: discord.Message | None = None,) -> None:
	"""
	再生中の楽曲情報をEmbedで送信する。
	- wait_msgが渡された場合はそのメッセージを編集して幽霊エラーを防ぐ
	- 失敗時はフォールバック表示に切り替える
	"""
	try:
		embed = discord.Embed(title="🎵 再生中", color=_GREEN)
		title_str = str(player.title)
		if len(title_str) > 100:
			title_str = title_str[:97] + "..."
		short_url = await shorten_url(player.display_url)
		field_value = f"[{title_str}]({short_url})"
		if len(field_value) > 1024:
			field_value = title_str[:1024]
		embed.add_field(name="タイトル", value=field_value, inline=False)
		duration = await play_time(player.data.get("duration") or 0)
		embed.add_field(name="再生時間", value=duration, inline=True)
		embed.add_field(name="待機数",   value=f"{queue_count} 曲", inline=True)
		icon_url = ctx.author.display_avatar.url if ctx.author.display_avatar else None
		embed.set_footer(text=f"Requested by: {ctx.author.display_name}", icon_url=icon_url)
		embed.set_image(url=player.data.get("thumbnail") or _FALLBACK_THUMBNAIL)
		if wait_msg:
			try:
				await wait_msg.edit(embed=embed)
				return
			except (discord.NotFound, discord.HTTPException):
				pass
		await ctx.send(embed=embed)
	except Exception as e:
		from module.logger import get_bot_logger
		get_bot_logger().error(f"music_info_embed エラー: {e}")
		try:
			await music_info_fallback_embed(ctx, player.title if player else "Unknown Title")
		except Exception:
			pass

async def preparing_audio_embed(ctx: commands.Context) -> discord.Message:
	"""音源準備中のウェイトメッセージを送信して、そのMessageオブジェクトを返す"""
	return await _send(ctx, "⏳ 準備中", "音源を準備しています...", _YELLOW)


# ==========================================
# エラー・警告系
# ==========================================
async def not_connect_bot_embed(ctx: commands.Context) -> None:
	await _send(ctx, "ℹ️ BOTがボイスチャンネルに接続していません。", color=_RED)

async def bot_not_in_vc_embed(ctx: commands.Context) -> None:
	await not_connect_bot_embed(ctx)

async def user_not_here_embed(ctx: commands.Context) -> None:
	await _send(ctx, "⚠️ ボイスチャンネルに接続してから実行してください。", color=_RED)

async def already_in_channel_embed(ctx: commands.Context) -> None:
	await _send(ctx, "⚠️ 通知", "既にそのチャンネルに接続しています。", _YELLOW)

async def not_playing_embed(ctx: commands.Context) -> None:
	await _send(ctx, "🎵 現在、何も再生されていません。", color=_RED)

async def empty_queue_embed(ctx: commands.Context) -> None:
	await _send(ctx, "📝 キューが空です。曲を追加してください。", color=_RED)

async def playback_error_embed(ctx: commands.Context, title: str) -> None:
	await _send(ctx, "⚠️ 再生エラー", f"再生中にエラーが発生しました: {title}\n次の曲へスキップします。", _RED)

async def load_error_embed(ctx: commands.Context, error: Exception, edit_msg: discord.Message | None = None) -> None:
	await _send(ctx, "⚠️ 読み込みエラー", f"読み込みに失敗しました:\n```py\n{error}\n```", _RED, edit_msg=edit_msg)

async def skip_error_embed(ctx: commands.Context, title: str, edit_msg: discord.Message | None = None) -> None:
	await _send(ctx, "⚠️ スキップ", f"`{title}` の読み込みに失敗したためスキップします。", _RED, edit_msg=edit_msg)

async def exception_embed(ctx: commands.Context, command_name: str, error: Exception) -> None:
	await _send(ctx, f"❌ エラーが発生しました ({command_name})", f"管理者にお問い合わせください。\n```py\n{error}\n```", _RED)

async def music_info_fallback_embed(ctx: commands.Context, title: str) -> None:
	await _send(ctx, "🎵 再生中", f"{title[:50]}...\n(詳細情報の表示に失敗しました)", _RED)

async def already_paused_embed(ctx: commands.Context) -> None:
	await _send(ctx, "⚠️ 通知", "既に一時停止中です。", _YELLOW)

async def already_playing_embed(ctx: commands.Context) -> None:
	await _send(ctx, "⚠️ 通知", "既に再生中です。", _YELLOW)

async def invalid_clear_range_embed(ctx: commands.Context) -> None:
	await _send(ctx, "⚠️ 範囲エラー", "正しい数値を指定してください。\n例: `/clear 5` または `/clear 4 8`", _YELLOW)


# ==========================================
# 設定コマンド系
# ==========================================
async def setting_help_embed(ctx: commands.Context) -> None:
	await _send(ctx, "⚠️ 通知", "サブコマンドを指定してください。\n例: `/setting limit playlist 20`", _YELLOW)

async def permission_error_embed(ctx: commands.Context) -> None:
	await _send(ctx, "❌ 権限エラー", "このコマンドを実行する権限がありません。", _RED)

async def admin_added_embed(ctx: commands.Context, user: discord.Member) -> None:
	await _send(ctx, "✅ 権限追加", f"{user.mention} にBot操作権限を付与しました。", _GREEN)

async def admin_removed_embed(ctx: commands.Context, user: discord.Member) -> None:
	await _send(ctx, "✅ 権限剥奪", f"{user.mention} のBot操作権限を剥奪しました。", _GREEN)

async def limit_range_error_embed(ctx: commands.Context) -> None:
	await _send(ctx, "⚠️ 範囲エラー", "1から50の間で指定してください。", _YELLOW)

async def limit_updated_embed(ctx: commands.Context, target: str, limit: int) -> None:
	await _send(ctx, "✅ 設定更新", f"{target}を **{limit}** 曲に設定しました。", _GREEN)


# ==========================================
# キューリスト表示
# ==========================================
async def queue_list_pages(queue: collections.deque | list) -> list[discord.Embed]:
	if not queue:
		return [discord.Embed(title="📝 キューリスト", description="キューは空です。", color=_BLUE)]
	TRACKS_PER_PAGE = 10
	items = list(queue)
	total = len(items)
	total_pages = (total - 1) // TRACKS_PER_PAGE + 1
	embeds: list[discord.Embed] = []
	for page in range(total_pages):
		embed = discord.Embed(title=f"📝 キューリスト ({page + 1}/{total_pages}ページ)", color=_BLUE)
		start = page * TRACKS_PER_PAGE
		lines: list[str] = []
		for i, track in enumerate(items[start:start + TRACKS_PER_PAGE], start=start + 1):
			t = track.get("title", "Unknown Title")
			if len(t) > 45:
				t = t[:42] + "..."
			dur = await play_time(track.get("duration", 0))
			lines.append(f"**{i}.** {t} `[{dur}]`")
		embed.description = "\n".join(lines)
		embeds.append(embed)
	return embeds