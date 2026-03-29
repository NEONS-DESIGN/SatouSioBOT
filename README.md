<h1 align="center">SatouSioBOT</h1>
<p align="center">砂糖塩という、Discordの音楽再生Botです。</p>

<p align="center">
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
このBotを動作させるには、システムに **FFmpeg** がインストールされており、パスが通っている必要があります。

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

# YouTubeのボット検知回避用
po_token = "YOUR_PO_TOKEN"
visitor_data = "YOUR_VISITOR_DATA"
```

### ② `config.ini` (Bot動作設定)
```ini
[MusicBot]
# プレイリストから一度に追加する最大曲数
playlist_limit = 10
```

### ③ `cookies.txt` (YouTubeクッキー)
YouTubeの403エラーを回避し、再生の安定性を高めるために必要です。ブラウザからエクスポートしたNetscape形式の `cookies.txt` を配置してください。

---

## 🚀 実行方法
```bash
python main.py
```
または、start.batを起動してください。

---

## ⚠️ 注意事項
* **YouTubeの制限**: YouTube側の仕様変更により、`po_token` や `cookies.txt` が期限切れになる場合があります。再生に失敗する（403エラー等）場合は、これらの情報を最新のものに更新してください。
* **履歴削除機能 (/purge)**: DiscordのAPI制限により、2週間（14日）以上経過したメッセージを一括削除することはできません。
* **データベース**: 起動時に `serverData.db` が自動生成され、音量設定などが保存されます。

---

## 📄 ライセンス
このプロジェクトは **MITライセンス** に基づいて公開されています。
