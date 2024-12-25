import datetime
import os
import re
import time
import discord
import pyshorteners
import urllib

from module.color import Color


class Link(discord.ui.View):
    def __init__(self, url: str):
        super().__init__()
        shortURL = ""
        # URL短縮
        for _ in range(3):  # 最大3回実行
            try:
                s = pyshorteners.Shortener(api_key=os.getenv("bitly_api"))
                shortURL = s.bitly.short(url)
                print(f"{Color.BG_GREEN}[Shortener]{Color.RESET}:\nSuccessful shortener of {Color.BOLD}{shortURL}")
            except Exception as e:
                print(f"\n{Color.RED}[ERROR]{Color.RESET}\n失敗しました。もう一度繰り返します。 残り: {_}回")
                print(f"\n{Color.RED}[ERROR]{Color.RESET}\n",e) # 例外の内容を表示
                time.sleep(1) # 適当に待つ
            else: # 成功してループ脱出
                self.add_item(discord.ui.Button(label="ダウンロード", url=shortURL))
                break
        else:
            raise Exception(f"URLショートカットの最大試行回数に達しました。")

async def download_file(url, dst_path):
    try:
        with urllib.request.urlopen(url) as web_file:
            with open(dst_path, 'wb') as local_file:
                local_file.write(web_file.read())
                print(f"{Color.BG_GREEN}[Downloader]{Color.RESET}:\nSuccessful download of {Color.BOLD}{url}")
    except Exception as e:
        print(f"\n{Color.RED}[ERROR]{Color.RESET}\n",e)
        return

# 与えられたYoutubeサービスのURLからIDだけを取り出し返す
async def get_id(url):
    vid = ""
    # 対応するURLパターン
    pattern_youtube = ['https://www.youtube.com/watch?', 'https://youtu.be/', 'https://music.youtube.com/watch?']
    pattern_niconico = 'https://www.nicovideo.jp/watch/'
    pattern_not_url = ['https://', 'http://']
    # 動画サイト区別
    type = ""

    # 通常URLのとき
    if re.match(pattern_youtube[0],url):
        yturl_qs = urllib.parse.urlparse(url).query
        vid = urllib.parse.parse_qs(yturl_qs)['v'][0]
        type = "youtube"
    # 短縮URLのとき
    elif re.match(pattern_youtube[1],url):
        # "https://youtu.be/"に続く11文字が動画ID
        vid = url[17:28]
        type = "youtube"
    # MusicURLのとき
    elif re.match(pattern_youtube[2],url):
        yturl_qs = urllib.parse.urlparse(url).query
        vid = urllib.parse.parse_qs(yturl_qs)['v'][0]
        type = "youtube"
    elif re.match(pattern_niconico,url):
        vid = url.split(pattern_niconico, maxsplit=1)[1]
        type = "niconico"
    elif not re.match(pattern_not_url[0],url) or not re.match(pattern_not_url[1],url):
        print("title")
        type = "title"
    else:
        raise Exception("No matching pattern was found.")
    return vid, type

async def play_time(duration):
    td = datetime.timedelta(seconds=duration)
    m, s = divmod(td.seconds, 60)
    h, m = divmod(m, 60)
    # 再生時間
    time = ""
    # 再生時間が1時間以上か
    if h > 0:
        time = str(h).zfill(2)+":"+str(m).zfill(2)+":"+str(s).zfill(2)
    else:
        time = str(m).zfill(2)+":"+str(s).zfill(2)
    return time