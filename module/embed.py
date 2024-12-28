import discord
from module.color import Embed

red = Embed.RED
green = Embed.LIGHT_GREEN

def help_pages():
    help_pages = [
        discord.Embed(
            title="コマンドヘルプ #1",
            fields=[
                discord.EmbedField(
                    name="/p [URL・タイトル名]", value="YouTube・ニコニコ動画の音声を再生することができます。", inline=False,
                ),
                discord.EmbedField(
                    name="/skip", value="再生中の音楽をスキップできます。", inline=False,
                ),
                discord.EmbedField(
                    name="/vol [数値]", value="再生される音量を1~200の間で変更できます。", inline=False,
                ),
                discord.EmbedField(
                    name="/move", value="自分のボイスチャンネルにBOTを移動させます。\nボイスチャンネルに接続していないと使用できません。", inline=False,
                ),
                discord.EmbedField(
                    name="/rep", value="キューをリピートするよう変更ができます。\n再生中の曲はキューから取り出されているため、リピートの対象にはなりません。", inline=False,
                ),
            ],
            color=green,
        ),
        discord.Embed(
            title="コマンドヘルプ #2",
            fields=[
                discord.EmbedField(
                    name="/leave", value="BOTをボイスチャンネルから退出させます。", inline=False,
                ),
                discord.EmbedField(
                    name="/履歴削除", value="テキストチャンネルの履歴を削除できます。\n一気に全ては削除できません。", inline=False,
                ),
            ],
            color=green,
        ),
    ]
    return help_pages

async def user_not_here_embed(ctx):
    embed = discord.Embed(title="エラー", description="あなたはボイスチャンネルに接続していません。", color=red)
    await ctx.respond(embed=embed)

async def not_connect_bot_embed(ctx):
    embed = discord.Embed(
        title="エラー", description="BOTが接続していません。", color=red)
    await ctx.respond(embed=embed)

async def not_playing_embed(ctx):
    embed = discord.Embed(title="音声が再生されていません。", color=red)
    await ctx.respond(embed=embed)

async def playing_now_embed(ctx):
    embed = discord.Embed(title="再生中は操作できません。", color=red)
    await ctx.respond(embed=embed)

async def exception_embed(ctx, message, error):
    """例外エラーをEmbedとして送信する

    Args:
        ctx (discord.Integration): 送信先
        message (String): エラー箇所の名前
        error (String): エラー内容
    """
    embed = discord.Embed(
        title=f"例外エラーが発生 ({message})", description=f"管理者(<@185708834264842240>)にお問い合わせください。\n詳細: {error}", color=red)
    await ctx.respond(embed=embed)

async def leave_embed(ctx):
    embed = discord.Embed(title="退出しました。", color=green)
    await ctx.respond(embed=embed)