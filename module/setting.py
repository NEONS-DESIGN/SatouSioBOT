import discord
from discord import app_commands
from discord.ext import commands

from module.embed import admin_added_embed, admin_removed_embed, autoleave_updated_embed, limit_updated_embed, setting_help_embed
from module.options import ALONE_TIMEOUT_MAX, MAX_LIMIT
from module.sqlite import save_guild_setting, sql_execution

class NotBotAdmin(commands.CheckFailure):
	"""Bot操作権限が無いユーザーが設定コマンドを実行した"""

async def check_admin_permission(ctx: commands.Context) -> bool:
	"""
	実行者がDiscordのサーバー管理者であるか、
	またはBotの管理者としてデータベースに登録されているかを判定する。
	DB の読み込みに失敗した場合は権限なしとして扱う (エラーは sql_execution がログに記録する)。
	"""
	if ctx.author.guild_permissions.administrator:
		return True
	try:
		rows = await sql_execution(
			"SELECT 1 FROM bot_admins WHERE guild_id=? AND user_id=? LIMIT 1;",
			(ctx.guild.id, ctx.author.id),
		)
	except Exception:
		return False
	return bool(rows)

def bot_admin_only():
	"""サーバー管理者または Bot 管理者のみ実行を許可するチェック (失敗時 NotBotAdmin)"""
	async def predicate(ctx: commands.Context) -> bool:
		if ctx.guild is None:
			raise commands.NoPrivateMessage()
		if await check_admin_permission(ctx):
			return True
		raise NotBotAdmin()
	return commands.check(predicate)

def setup_setting_commands(bot: commands.Bot) -> None:
	"""設定用コマンド群 (/setting) をBotに登録する"""
	@bot.hybrid_group(name="setting", description="Botの設定を変更します。")
	@commands.guild_only()
	async def setting(ctx: commands.Context) -> None:
		if ctx.invoked_subcommand is None:
			await setting_help_embed(ctx)

	@setting.group(name="admin", description="Bot管理者に関する設定を行います。")
	async def setting_admin(ctx: commands.Context) -> None:
		if ctx.invoked_subcommand is None:
			await setting_help_embed(ctx)

	@setting_admin.command(name="add", description="特定のユーザーにBotの操作権限を付与します。")
	@bot_admin_only()
	async def admin_add(ctx: commands.Context, user: discord.Member) -> None:
		await sql_execution("INSERT OR IGNORE INTO bot_admins (guild_id, user_id) VALUES (?, ?);", (ctx.guild.id, user.id))
		await admin_added_embed(ctx, user)

	@setting_admin.command(name="remove", description="特定のユーザーからBotの操作権限を剥奪します。")
	@bot_admin_only()
	async def admin_remove(ctx: commands.Context, user: discord.Member) -> None:
		await sql_execution("DELETE FROM bot_admins WHERE guild_id=? AND user_id=?;", (ctx.guild.id, user.id))
		await admin_removed_embed(ctx, user)

	@setting.group(name="limit", description="上限に関する設定を行います。")
	async def setting_limit(ctx: commands.Context) -> None:
		if ctx.invoked_subcommand is None:
			await setting_help_embed(ctx)

	@setting_limit.command(name="queue", description=f"キューの最大曲数を設定します。(1〜{MAX_LIMIT})")
	@bot_admin_only()
	async def limit_queue(ctx: commands.Context, limit: commands.Range[int, 1, MAX_LIMIT]) -> None:
		await save_guild_setting(ctx.guild.id, "queue_limit", limit)
		await limit_updated_embed(ctx, "キューの最大曲数", limit)

	@setting_limit.command(name="playlist", description=f"プレイリストから取得する最大曲数を設定します。(1〜{MAX_LIMIT})")
	@bot_admin_only()
	async def limit_playlist(ctx: commands.Context, limit: commands.Range[int, 1, MAX_LIMIT]) -> None:
		await save_guild_setting(ctx.guild.id, "playlist_limit", limit)
		await limit_updated_embed(ctx, "プレイリストの取得上限", limit)

	@setting.command(name="autoleave", description=f"聴者がいなくなってから自動で退出するまでの秒数を設定します。(0〜{ALONE_TIMEOUT_MAX}、0で無効)")
	@app_commands.describe(seconds=f"退出までの秒数 (0〜{ALONE_TIMEOUT_MAX})。0 にすると自動退出しません。")
	@app_commands.rename(seconds="秒数")
	@bot_admin_only()
	async def autoleave(ctx: commands.Context, seconds: commands.Range[int, 0, ALONE_TIMEOUT_MAX]) -> None:
		await save_guild_setting(ctx.guild.id, "alone_timeout", seconds)
		await autoleave_updated_embed(ctx, seconds)
