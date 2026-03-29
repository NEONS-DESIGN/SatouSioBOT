import discord
from discord.ext import commands
from module.color import Embed

# カラーコード定数
RED = Embed.RED
GREEN = Embed.LIGHT_GREEN
BLUE = 0x3498DB

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

# --- 通知系Embed ---

async def move_channel_embed(ctx: commands.Context):
    """チャンネル移動完了の通知"""
    await ctx.send(embed=discord.Embed(title="🚚 チャンネルを移動しました。", color=GREEN))

async def leave_embed(ctx: commands.Context):
    """ボイスチャンネル退出の通知"""
    await ctx.send(embed=discord.Embed(title="👋 退出しました。またね！", color=GREEN))

async def skip_music_embed(ctx: commands.Context):
    """曲のスキップ完了の通知"""
    await ctx.send(embed=discord.Embed(title="⏭️ 曲をスキップしました。", color=GREEN))

async def play_completed_embed(ctx: commands.Context):
    """キュー内の全曲再生完了の通知"""
    await ctx.send(embed=discord.Embed(title="✅ 全てのトラックの再生が終了しました。", color=GREEN))

async def loop_switch_embed(ctx: commands.Context, state: str):
    """ループ設定変更の通知"""
    await ctx.send(embed=discord.Embed(title=f"🔁 ループ再生を {state} にしました。", color=GREEN))

async def shuffle_complete_embed(ctx: commands.Context):
    """キューのシャッフル完了の通知"""
    await ctx.send(embed=discord.Embed(title="🔀 キューをシャッフルしました。", color=GREEN))

async def volume_set_embed(ctx: commands.Context, volume: int):
    """音量設定完了の通知"""
    await ctx.send(embed=discord.Embed(title=f"🔊 再生音量を {volume}% に設定しました。", color=GREEN))

async def purge_complete_embed(ctx: commands.Context, count: int):
    """履歴一括削除完了の通知"""
    embed = discord.Embed(title=f"✅ {count} 件のメッセージを削除しました。", color=GREEN)
    await ctx.send(embed=embed, ephemeral=True)

async def playlist_added_embed(ctx: commands.Context, title: str, count: int):
    """プレイリストからの複数曲追加時の通知"""
    embed = discord.Embed(
        title="📝 プレイリストを追加",
        description=f"**{title}** から {count} 曲をキューに追加しました",
        color=BLUE
    )
    await ctx.send(embed=embed)

async def queue_added_embed(ctx: commands.Context, title: str, count: int):
    """待機キューへの曲追加時の通知"""
    desc = f"**{title}**" if count == 1 else f"プレイリスト: **{title}** ({count} 曲)"
    embed = discord.Embed(title="✅ キューに追加", description=desc, color=BLUE)
    await ctx.send(embed=embed)

async def help_mention_embed(message: discord.Message):
    """メンション受信時のヘルプ案内通知"""
    embed = discord.Embed(description="助けが必要ですか？\n必要な場合は、</help:1140613857224687616> コマンドを実行してください。", color=GREEN)
    await message.reply(embed=embed)

# --- エラー・警告系Embed ---

async def user_not_here_embed(ctx: commands.Context):
    """ユーザーがボイスチャンネル未接続時の警告"""
    await ctx.send(embed=discord.Embed(title="⚠️ ボイスチャンネルに接続してから実行してください。", color=RED))

async def not_connect_bot_embed(ctx: commands.Context):
    """Botがボイスチャンネル未接続時の警告"""
    await ctx.send(embed=discord.Embed(title="ℹ️ BOTがボイスチャンネルに接続していません。", color=RED))

async def not_playing_embed(ctx: commands.Context):
    """楽曲未再生時の警告"""
    await ctx.send(embed=discord.Embed(title="🎵 現在、何も再生されていません。", color=RED))

async def playing_now_embed(ctx: commands.Context):
    """再生中の操作ブロック時の警告"""
    await ctx.send(embed=discord.Embed(title="🚫 再生中はこの操作を行えません。", color=RED))

async def empty_queue_embed(ctx: commands.Context):
    """キューが空である場合の警告"""
    await ctx.send(embed=discord.Embed(title="📝 キューが空です。曲を追加してください。", color=RED))

async def none_result_embed(ctx: commands.Context):
    """検索結果ゼロ件時の警告"""
    await ctx.send(embed=discord.Embed(title="🔎 検索結果が見つかりませんでした。", color=RED))

async def playback_error_embed(ctx: commands.Context, title: str):
    """再生エラー発生時の通知"""
    embed = discord.Embed(title="⚠️ 再生エラー", description=f"再生中にエラーが発生しました: {title}\n次の曲へスキップします。", color=RED)
    await ctx.send(embed=embed)

async def load_error_embed(ctx: commands.Context, error: Exception):
    """曲のメタデータ読み込み失敗時の通知"""
    embed = discord.Embed(title="⚠️ 読み込みエラー", description=f"読み込みに失敗しました:\n```py\n{error}\n```", color=RED)
    await ctx.send(embed=embed)

async def music_info_fallback_embed(ctx: commands.Context, title: str):
    """再生詳細情報のEmbed生成失敗時の最低限の通知"""
    embed = discord.Embed(title="🎵 再生中", description=f"{title[:50]}...\n(詳細情報の表示に失敗しました)", color=RED)
    await ctx.send(embed=embed)

async def exception_embed(ctx: commands.Context, command_name: str, error: Exception):
    """予期せぬ例外エラー発生時の通知"""
    embed = discord.Embed(
        title=f"❌ エラーが発生しました ({command_name})",
        description=f"管理者にお問い合わせください。\n```py\n{error}\n```",
        color=RED
    )
    await ctx.send(embed=embed)