from collections.abc import Iterable

import discord
from discord.ext import commands

from module.color import Embed as EmbedColor
from module.errors import ErrorCause, describe_error
from module.logger import get_bot_logger
from module.utils import format_duration

logger = get_bot_logger()

# カラー定数
_RED    = EmbedColor.RED
_GREEN  = EmbedColor.GREEN
_BLUE   = EmbedColor.BLUE
_YELLOW = EmbedColor.YELLOW

# 再生中サムネイルのフォールバック画像URL
_FALLBACK_THUMBNAIL = "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?q=80&w=1024&auto=format&fit=crop"
# Embed の説明文・フィールド値の最大文字数 (Discord の制限)
_DESCRIPTION_LIMIT = 4096
_FIELD_VALUE_LIMIT = 1024
# 再生中表示のタイトル最大文字数 (通常表示 / 表示失敗時の簡易表示)
_NOW_PLAYING_TITLE_LIMIT = 100
_FALLBACK_TITLE_LIMIT = 50
# キューリストの1ページあたりの曲数とタイトル最大文字数
_TRACKS_PER_PAGE = 10
_QUEUE_TITLE_LIMIT = 45
# 想定外のエラーで表示する元のエラー文の最大文字数
_RAW_ERROR_LIMIT = 1500
# Bot 側の問題によるエラーで、利用者向けの説明に添える一文
_SERVER_ERROR_NOTE = "Bot 側の問題のため、時間をおいても直らない場合は Bot の管理者にお知らせください。"


# ==========================================
# 内部ヘルパー
# ==========================================
async def _send_or_edit(ctx: commands.Context, embed: discord.Embed, edit_msg: discord.Message | None = None, ephemeral: bool = False) -> discord.Message:
	"""edit_msg があれば編集し、失敗 (削除済みなど) または未指定なら新規送信する"""
	if edit_msg:
		try:
			return await edit_msg.edit(embed=embed)
		except discord.HTTPException:
			pass
	return await ctx.send(embed=embed, ephemeral=ephemeral)

async def _send(ctx: commands.Context, title: str, description: str | None = None, color: int = _GREEN, ephemeral: bool = False, edit_msg: discord.Message | None = None) -> discord.Message:
	"""タイトルと説明文だけのシンプルなEmbedを送信 (edit_msg 指定時は編集) する"""
	if description is not None:
		description = _truncate(description, _DESCRIPTION_LIMIT)
	return await _send_or_edit(ctx, discord.Embed(title=title, description=description, color=color), edit_msg, ephemeral)

def _truncate(text: str, limit: int) -> str:
	"""limit 文字を超える場合は末尾を "..." にして切り詰める"""
	return text if len(text) <= limit else text[:limit - 3] + "..."

def _title_link(title: str, url: str | None, limit: int = _FIELD_VALUE_LIMIT) -> str:
	"""[タイトル](URL) 形式のリンクを返す。limit 文字を超える場合はタイトルのみにする"""
	if url:
		# タイトル中の角括弧と URL 中の丸括弧はリンク記法を壊すためエスケープする
		text = title.replace("[", "\\[").replace("]", "\\]")
		link = f"[{text}]({url.replace('(', '%28').replace(')', '%29')})"
		if len(link) <= limit:
			return link
	return _truncate(title, limit)

def _set_requester_footer(embed: discord.Embed, ctx: commands.Context) -> None:
	"""Embed のフッターにリクエスト者を表示する"""
	embed.set_footer(text=f"Requested by: {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)

def _music_embed_base(ctx: commands.Context, info: dict, title: str, label: str) -> discord.Embed:
	"""楽曲・プレイリスト追加通知用Embedのベース (label 欄にタイトルとURL、サムネイル、フッター) を生成する"""
	url = info.get("webpage_url") or info.get("url")
	embed = discord.Embed(title=title, color=_BLUE)
	embed.add_field(name=label, value=_title_link(info.get("title") or "Unknown Title", url), inline=False)
	_set_requester_footer(embed, ctx)
	if thumbnail := info.get("thumbnail"):
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
	p1.add_field(name="/speed [0.5-3.0] [ピッチ維持]", value="再生速度を変更します（再生中の曲にも即反映）。ピッチ維持を False にすると音の高さも変わります。退出・再生終了で1倍に戻ります。", inline=False)

	p2 = discord.Embed(title="📖 コマンドヘルプ #2 (キュー・音量)", color=_GREEN)
	p2.add_field(name="/qlist",              value="現在のキューに入っている曲のリストを表示します。", inline=False)
	p2.add_field(name="/pnow [番号]",         value="キューの指定した曲を今すぐ再生します。再生中の曲は次の曲として最初から再生し直します。", inline=False)
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
	p3.add_field(name="/setting autoleave [秒数]",       value="聴者がいなくなってから自動で退出するまでの秒数を設定します（0で自動退出しない）。", inline=False)

	return [p1, p2, p3]

async def help_mention_embed(message: discord.Message, help_mention: str) -> None:
	"""メンション受信時のヘルプ案内。help_mention は </help:ID> 形式のコマンドメンション"""
	embed = discord.Embed(
		description=f"助けが必要ですか？\n必要な場合は、{help_mention} コマンドを実行してください。",
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

async def alone_leave_embed(channel: discord.abc.Messageable) -> None:
	"""聴者不在による自動退出の通知。コマンドの応答ではないため ctx ではなくチャンネルへ直接送る"""
	embed = discord.Embed(title="👋 聴者がいなくなったため再生を停止します", description="ボイスチャンネルから退出しました。", color=_YELLOW)
	await channel.send(embed=embed)

async def _send_to_channel(channel: discord.abc.Messageable, embed: discord.Embed, edit_msg: discord.Message | None = None) -> discord.Message:
	"""コマンドの応答ではない通知をチャンネルへ送る。edit_msg があれば編集し、失敗 (削除済みなど) なら新規送信する"""
	if edit_msg:
		try:
			return await edit_msg.edit(embed=embed)
		except discord.HTTPException:
			pass
	return await channel.send(embed=embed)

async def voice_reconnecting_embed(channel: discord.abc.Messageable, attempts: int) -> discord.Message:
	"""Discord との音声接続が切れ、再接続を始めたときの通知 (結果は同じメッセージを編集して知らせる)"""
	embed = discord.Embed(
		title="📡 ボイスチャンネルとの接続が切れました",
		description=f"Discord との通信が一時的に途切れたため、再接続しています… (最大 {attempts} 回)\n再接続できれば、切れた位置から再生を再開します。",
		color=_YELLOW,
	)
	return await _send_to_channel(channel, embed)

async def voice_reconnected_embed(channel: discord.abc.Messageable, track: dict | None, edit_msg: discord.Message | None = None) -> None:
	"""再接続に成功したときの通知。再開する曲があれば曲名と再開位置を示す"""
	if track is None:
		description = "ボイスチャンネルに再接続しました。キューの続きから再生します。"
	else:
		description = f"ボイスチャンネルに再接続しました。\n{_title_link(track['title'], track['url'])} を **{format_duration(track['start'])}** から再開します。"
	embed = discord.Embed(title="✅ 再接続しました", description=_truncate(description, _DESCRIPTION_LIMIT), color=_GREEN)
	await _send_to_channel(channel, embed, edit_msg)

async def voice_reconnect_failed_embed(channel: discord.abc.Messageable, reason: str, edit_msg: discord.Message | None = None) -> None:
	"""再接続を諦めて再生を止めたときの通知。reason は「〜ため」で終わる停止の理由"""
	embed = discord.Embed(
		title="⚠️ 再生を停止しました",
		description=f"{reason}、再生を停止しました。キューは空になっています。\n少し時間をおいてから `/p` で再生し直してください。",
		color=_RED,
	)
	await _send_to_channel(channel, embed, edit_msg)

async def loop_switch_embed(ctx: commands.Context, state: str) -> None:
	await _send(ctx, f"🔁 ループ再生を {state} にしました。")

async def shuffle_complete_embed(ctx: commands.Context) -> None:
	await _send(ctx, "🔀 キューをシャッフルしました。")

async def volume_set_embed(ctx: commands.Context, volume: int) -> None:
	await _send(ctx, f"🔊 再生音量を {volume}% に設定しました。")

def _speed_label(speed: float, keep_pitch: bool) -> str:
	"""再生速度の表示文字列 (例: "1.5 倍 (ピッチ維持)")"""
	return f"{speed:g} 倍 ({'ピッチ維持' if keep_pitch else 'ピッチ変更'})"

async def speed_set_embed(ctx: commands.Context, speed: float, keep_pitch: bool) -> None:
	await _send(ctx, f"⏩ 再生速度を {_speed_label(speed, keep_pitch)} に設定しました。", "ボイスチャンネルから退出するか、再生が終了すると1倍に戻ります。")

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

async def play_now_embed(ctx: commands.Context, track: dict, bumped: dict) -> None:
	"""キューの曲を今すぐ再生した通知。bumped は押しのけられて次の曲に回った曲"""
	description = (
		f"{_title_link(_truncate(track.get('title', 'Unknown Title'), _NOW_PLAYING_TITLE_LIMIT), track.get('url'))} を再生します。\n"
		f"再生中だった {_title_link(_truncate(bumped.get('title', 'Unknown Title'), _NOW_PLAYING_TITLE_LIMIT), bumped.get('url'))} は次に最初から再生します。"
	)
	await _send(ctx, "⏯️ 今すぐ再生", description)


# ==========================================
# 楽曲追加・再生情報
# ==========================================
async def playlist_added_embed(ctx: commands.Context, info: dict, count: int, edit_msg: discord.Message | None = None) -> None:
	"""プレイリストをキューに追加した際の通知Embed"""
	embed = _music_embed_base(ctx, info, "📝 プレイリストをキューに追加", "プレイリスト名")
	embed.add_field(name="追加曲数", value=f"{count} 曲", inline=True)
	await _send_or_edit(ctx, embed, edit_msg)

async def queue_added_embed(ctx: commands.Context, info: dict, queue_pos: int, edit_msg: discord.Message | None = None) -> None:
	"""単曲をキューに追加した際の通知Embed"""
	embed = _music_embed_base(ctx, info, "✅ キューに追加", "タイトル")
	embed.add_field(name="再生時間", value=format_duration(info.get("duration")), inline=True)
	embed.add_field(name="待機数",   value=f"{queue_pos} 曲", inline=True)
	await _send_or_edit(ctx, embed, edit_msg)

async def music_info_embed(ctx: commands.Context, source: discord.AudioSource, queue_count: int, wait_msg: discord.Message | None = None) -> None:
	"""
	再生中の楽曲情報をEmbedで送信する。
	- source は data(track dict) / title / display_url / speed / keep_pitch を持つ再生ソース (速度は等速以外のとき表示)
	- wait_msg が渡された場合はそのメッセージを編集する
	- 失敗時はフォールバック表示に切り替える
	"""
	title: str = source.title
	try:
		data: dict = source.data
		embed = discord.Embed(title="🎵 再生中", color=_GREEN)
		embed.add_field(name="タイトル", value=_title_link(_truncate(title, _NOW_PLAYING_TITLE_LIMIT), source.display_url), inline=False)
		embed.add_field(name="再生時間", value=format_duration(data.get("duration")), inline=True)
		embed.add_field(name="待機数",   value=f"{queue_count} 曲", inline=True)
		if source.speed != 1.0:
			embed.add_field(name="再生速度", value=_speed_label(source.speed, source.keep_pitch), inline=True)
		_set_requester_footer(embed, ctx)
		embed.set_image(url=data.get("thumbnail") or _FALLBACK_THUMBNAIL)
		await _send_or_edit(ctx, embed, wait_msg)
	except Exception as e:
		logger.error(f"music_info_embed エラー: {e}")
		try:
			await music_info_fallback_embed(ctx, title)
		except discord.HTTPException:
			pass

async def preparing_audio_embed(ctx: commands.Context) -> discord.Message:
	"""音源準備中のウェイトメッセージを送信して、そのMessageオブジェクトを返す"""
	return await _send(ctx, "⏳ 準備中", "音源を準備しています...", _YELLOW)


# ==========================================
# エラー・警告系
# ==========================================
async def not_connect_bot_embed(ctx: commands.Context) -> None:
	await _send(ctx, "ℹ️ BOTがボイスチャンネルに接続していません。", color=_RED)

async def user_not_here_embed(ctx: commands.Context) -> None:
	await _send(ctx, "⚠️ ボイスチャンネルに接続してから実行してください。", color=_RED)

async def already_in_channel_embed(ctx: commands.Context) -> None:
	await _send(ctx, "⚠️ 通知", "既にそのチャンネルに接続しています。", _YELLOW)

async def not_playing_embed(ctx: commands.Context) -> None:
	await _send(ctx, "🎵 現在、何も再生されていません。", color=_RED)

async def empty_queue_embed(ctx: commands.Context) -> None:
	await _send(ctx, "📝 キューが空です。曲を追加してください。", color=_RED)

async def _send_error(
	ctx: commands.Context,
	title: str,
	error: BaseException,
	*,
	lead: str | None = None,
	unknown_lead: str | None = None,
	edit_msg: discord.Message | None = None,
) -> discord.Message:
	"""
	エラーの原因に応じた通知を送る (edit_msg 指定時は編集)。lead は先頭に常に出す一文。
	- 利用者側 (URL・動画の問題): 黄色で、原因と確認してほしいことを表示する
	- Bot 側 (環境・認証・ライブラリ・通信の問題): 赤で、説明と管理者向けの対処を表示する
	- 想定外: 赤で、unknown_lead に続けて元のエラー文をそのまま表示する
	"""
	info = describe_error(error)
	lines = [lead] if lead else []
	if info.cause is ErrorCause.UNKNOWN:
		if unknown_lead:
			lines.append(unknown_lead)
		lines.append(f"```py\n{_truncate(info.message, _RAW_ERROR_LIMIT)}\n```")
		color = _RED
	elif info.cause is ErrorCause.USER:
		lines.append(info.message)
		color = _YELLOW
	else:
		lines += [info.message, _SERVER_ERROR_NOTE]
		color = _RED
	embed = discord.Embed(title=title, description=_truncate("\n".join(lines), _DESCRIPTION_LIMIT), color=color)
	if info.admin_hint:
		embed.add_field(name="🔧 管理者向け", value=_truncate(info.admin_hint, _FIELD_VALUE_LIMIT), inline=False)
	return await _send_or_edit(ctx, embed, edit_msg)

async def playback_error_embed(ctx: commands.Context, title: str, error: BaseException, edit_msg: discord.Message | None = None) -> None:
	await _send_error(ctx, "⚠️ 再生エラー", error, lead=f"再生中にエラーが発生しました: {title}\n次の曲へスキップします。", edit_msg=edit_msg)

async def load_error_embed(ctx: commands.Context, error: BaseException, edit_msg: discord.Message | None = None) -> None:
	await _send_error(ctx, "⚠️ 読み込みエラー", error, unknown_lead="読み込みに失敗しました:", edit_msg=edit_msg)

async def skip_error_embed(ctx: commands.Context, title: str, error: BaseException, edit_msg: discord.Message | None = None) -> None:
	await _send_error(ctx, "⚠️ スキップ", error, lead=f"`{title}` の読み込みに失敗したためスキップします。", edit_msg=edit_msg)

async def exception_embed(ctx: commands.Context, command_name: str, error: BaseException) -> None:
	await _send_error(ctx, f"❌ エラーが発生しました ({command_name})", error, unknown_lead="管理者にお問い合わせください。")

async def music_info_fallback_embed(ctx: commands.Context, title: str) -> None:
	await _send(ctx, "🎵 再生中", f"{_truncate(title, _FALLBACK_TITLE_LIMIT)}\n(詳細情報の表示に失敗しました)", _RED)

async def already_paused_embed(ctx: commands.Context) -> None:
	await _send(ctx, "⚠️ 通知", "既に一時停止中です。", _YELLOW)

async def already_playing_embed(ctx: commands.Context) -> None:
	await _send(ctx, "⚠️ 通知", "既に再生中です。", _YELLOW)

async def invalid_clear_range_embed(ctx: commands.Context) -> None:
	await _send(ctx, "⚠️ 範囲エラー", "正しい数値を指定してください。\n例: `/clear 5` または `/clear 4 8`", _YELLOW)

async def invalid_queue_index_embed(ctx: commands.Context, queue_count: int) -> None:
	await _send(ctx, "⚠️ 範囲エラー", f"1〜{queue_count} の番号を指定してください。\n番号は `/qlist` で確認できます。", _YELLOW)


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

async def invalid_argument_embed(ctx: commands.Context, detail: str) -> None:
	await _send(ctx, "⚠️ 入力エラー", f"引数が正しくありません。\n{detail}", _YELLOW)

async def guild_only_embed(ctx: commands.Context) -> None:
	await _send(ctx, "⚠️ 通知", "このコマンドはサーバー内でのみ使用できます。", _YELLOW)

async def limit_updated_embed(ctx: commands.Context, target: str, limit: int) -> None:
	await _send(ctx, "✅ 設定更新", f"{target}を **{limit}** 曲に設定しました。", _GREEN)

async def autoleave_updated_embed(ctx: commands.Context, seconds: int) -> None:
	if seconds == 0:
		description = "聴者がいなくなっても自動で退出しないように設定しました。"
	else:
		description = f"聴者がいなくなってから **{seconds}** 秒後に自動で退出するように設定しました。"
	await _send(ctx, "✅ 設定更新", description, _GREEN)


# ==========================================
# キューリスト表示
# ==========================================
def _queue_line(index: int, track: dict, with_link: bool) -> str:
	"""キューリストの1行 (番号・タイトル・再生時間) を返す。with_link なら タイトルに URL を埋め込む"""
	title = _truncate(track.get("title", "Unknown Title"), _QUEUE_TITLE_LIMIT)
	if with_link:
		title = _title_link(title, track.get("url"))
	return f"**{index}.** {title} `[{format_duration(track.get('duration'))}]`"

def queue_list_pages(queue: Iterable[dict]) -> list[discord.Embed]:
	"""
	キューの内容を _TRACKS_PER_PAGE 曲ずつのページ Embed リストにする。
	- タイトルには URL を埋め込む。説明文の上限を超える行はタイトルのみにする
	"""
	items = list(queue)
	if not items:
		return [discord.Embed(title="📝 キューリスト", description="キューは空です。", color=_BLUE)]
	total_pages = (len(items) - 1) // _TRACKS_PER_PAGE + 1
	embeds: list[discord.Embed] = []
	for page in range(total_pages):
		start = page * _TRACKS_PER_PAGE
		lines: list[str] = []
		used = 0
		for i, track in enumerate(items[start:start + _TRACKS_PER_PAGE], start=start + 1):
			line = _queue_line(i, track, with_link=True)
			# 改行1文字分を含めて上限を超えるならリンク無しの行にする
			if used + len(line) + 1 > _DESCRIPTION_LIMIT:
				line = _queue_line(i, track, with_link=False)
			lines.append(line)
			used += len(line) + 1
		embeds.append(discord.Embed(
			title=f"📝 キューリスト ({page + 1}/{total_pages}ページ)",
			description="\n".join(lines),
			color=_BLUE,
		))
	return embeds
