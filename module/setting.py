import discord
from discord.ext import commands
from module.sqlite import sql_execution
from module.embed import * # Embed関数群をインポート

async def check_admin_permission(ctx: commands.Context) -> bool:
	"""
	実行者がDiscordのサーバー管理者であるか、
	またはBotの管理者としてデータベースに登録されているかを判定する。
	"""
	if ctx.author.guild_permissions.administrator:
		return True
	res = await sql_execution("SELECT * FROM bot_admins WHERE guild_id=? AND user_id=?;", (ctx.guild.id, ctx.author.id))
	return bool(res and len(res) > 0)

def setup_setting_commands(bot: commands.Bot):
	"""
	設定用スラッシュコマンド群をBotに登録する。
	"""
	@bot.hybrid_group(name="setting", description="Botの設定を変更します。")
	@commands.guild_only()
	async def setting(ctx: commands.Context):
		if ctx.invoked_subcommand is None:
			await setting_help_embed(ctx)

	@setting.group(name="admin", description="Bot管理者に関する設定を行います。")
	async def setting_admin(ctx: commands.Context):
		pass

	@setting_admin.command(name="add", description="特定のユーザーにBotの操作権限を付与します。")
	async def admin_add(ctx: commands.Context, user: discord.Member):
		if not await check_admin_permission(ctx):
			return await permission_error_embed(ctx)
		await sql_execution("INSERT OR IGNORE INTO bot_admins (guild_id, user_id) VALUES (?, ?);", (ctx.guild.id, user.id))
		await admin_added_embed(ctx, user)

	@setting_admin.command(name="remove", description="特定のユーザーからBotの操作権限を剥奪します。")
	async def admin_remove(ctx: commands.Context, user: discord.Member):
		if not await check_admin_permission(ctx):
			return await permission_error_embed(ctx)
		await sql_execution("DELETE FROM bot_admins WHERE guild_id=? AND user_id=?;", (ctx.guild.id, user.id))
		await admin_removed_embed(ctx, user)

	@setting.group(name="limit", description="上限に関する設定を行います。")
	async def setting_limit(ctx: commands.Context):
		pass

	@setting_limit.command(name="queue", description="キューの最大曲数を設定します。(1〜50)")
	async def limit_queue(ctx: commands.Context, limit: int):
		if not await check_admin_permission(ctx):
			return await permission_error_embed(ctx)
		if not (1 <= limit <= 50):
			return await limit_range_error_embed(ctx)
		await sql_execution("INSERT OR IGNORE INTO serverData (guild_id) VALUES (?);", (ctx.guild.id,))
		await sql_execution("UPDATE serverData SET queue_limit=? WHERE guild_id=?;", (limit, ctx.guild.id))
		await limit_updated_embed(ctx, "キューの最大曲数", limit)

	@setting_limit.command(name="playlist", description="プレイリストから取得する最大曲数を設定します。(1〜50)")
	async def limit_playlist(ctx: commands.Context, limit: int):
		if not await check_admin_permission(ctx):
			return await permission_error_embed(ctx)
		if not (1 <= limit <= 50):
			return await limit_range_error_embed(ctx)
		await sql_execution("INSERT OR IGNORE INTO serverData (guild_id) VALUES (?);", (ctx.guild.id,))
		await sql_execution("UPDATE serverData SET playlist_limit=? WHERE guild_id=?;", (limit, ctx.guild.id))
		await limit_updated_embed(ctx, "プレイリストの取得上限", limit)