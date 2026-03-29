<h1 align="center">SatouSioBOT</h1>
<p align="center">砂糖塩という、Discordの音楽再生Botです。できるだけ起動するだけで使用ができるように制作されています。</p>

<p align="center">
  <a href="https://www.python.org/downloads/release/python-31312/"><img src="https://img.shields.io/badge/Python-v3.13.12-ffde57" alt="/Discord.py"></a>
  <a href="https://github.com/Rapptz/discord.py"><img src="https://img.shields.io/badge/Discord.py-v2.7.1-3498db" alt="/Discord.py"></a>
  <a href="https://github.com/ellisonleao/pyshorteners/"><img src="https://img.shields.io/badge/pyshorteners-v1.0.1-34495e" alt="/pyshorteners"></a>
  <a href="https://github.com/yt-dlp/yt-dlp"><img src="https://img.shields.io/badge/yt--dlp-v2026.03.17-FF0000" alt="/yt-dlp"></a>
  <a href="https://ffmpeg.org/"><img src="https://img.shields.io/badge/ffmpeg-v8.1-242424" alt="/ffmpeg"></a>
</p>

---

## 🎵 概要
YouTube、ニコニコ動画、SoundCloudなど複数のプラットフォームに対応した高機能な音楽再生Botです。  
`yt-dlp`のストリーミング最適化と`aiosqlite`による非同期データベース管理により、低遅延で安定した動作を実現しています。

---

## 🛠 準備
### 1. 外部依存ソフトのインストール
このBotを動作させるには、システムに [**FFmpeg**](https://www.ffmpeg.org/) がインストールされており、パスが通っている必要があります。  
また、[**Deno**](https://deno.com/)と[**Node.js**](https://nodejs.org/)も必要になる場合があります。

### 2. ライブラリのインストール
以下のコマンドを実行して、必要なPythonライブラリを一括でインストールしてください。

```bash
pip install -r requirements.txt
```

---

## ⚙️ 設定ファイルの作成
Botの実行には、以下の3つのファイルを `main.py` と同じディレクトリに配置する必要があります。

### ① `.env` (APIキー・環境変数)
```env
discord_api = "あなたのDISCORD_BOT_TOKEN"
bitly_api = "あなたのBITLY_ACCESS_TOKEN"

# 現在は自動取得するようにしたため、不必要になりました。アーカイブ用に残してあります。
# YouTubeのボット検知回避用
#po_token = "YOUR_PO_TOKEN"
#visitor_data = "YOUR_VISITOR_DATA"
```

### ② `config.ini` (Bot動作設定)
```ini
[MusicBot]
# プレイリストから取得する最大曲数
playlist_limit = 10
# FFmpegに渡すカスタムヘッダー（アクセス制限回避用）
ffmpeg_headers = "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
# SQLiteデータベースのファイルパス
database_path = data.db
# yt-dlpのクッキー読み込みに使用するファイルパス
cookie_file_path = cookies.txt
```

### ③ `cookies.txt` (YouTubeクッキー)
YouTubeの403エラーを回避し、再生の安定性を高めるために必要です。  
ブラウザからエクスポートしたNetscape形式の `cookies.txt` を配置してください。

---

## 🚀 実行方法
```bash
python main.py
```
または、start.batを起動してください。

---

## ⚠️ 注意事項・セキュリティに関する重要なお知らせ
本ボットを利用する際は、以下の点に十分ご注意ください。
* **YouTubeの制限**: YouTube側の仕様変更により、`cookies.txt` が期限切れになる場合があります。再生に失敗する（403エラー等）場合は、`cookies.txt`の情報や、yt-dlp関連を最新のものに更新してください。
* **履歴削除機能 (/purge)**: DiscordのAPI制限により、2週間（14日）以上経過したメッセージを一括削除することはできません。
* **データベース**: 起動時に `data.db` が自動生成され、音量設定などが保存されます。
* **認証情報の取り扱いについて**
Cookie や PO Token は、あなたの本人確認を行うための非常に重要な情報です。これらが第三者に渡ると、アカウントの不正利用や個人情報の流出につながる恐れがあります。スクリーンショットやログ、ソースコードの共有時にこれらが含まれないよう、厳重に管理してください。
* **免責事項**
本ソフトウェアの使用によって生じた、いかなる損害（データの損失、アカウントの停止、金銭的被害など）についても、開発者は一切の責任を負いません。 全て利用者の自己責任において使用するものとします。
* **非公式ツールであることの理解**
本ツールは公式のサービス提供者が提供するものではありません。仕様変更により突然利用できなくなったり、予期せぬ挙動が発生したりする可能性があることをあらかじめご了承ください。

---

## 📄 ライセンス
このプロジェクトは [**MITライセンス**](https://github.com/NEONS-DESIGN/SatouSioBOT/blob/main/LICENSE) に基づいて公開されています。
Copyright (c) 2026 NEONS-DESIGN
