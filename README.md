<h1 align="center">SatouSioBOT</h1>
<p align="center"><img width="140" src="https://raw.githubusercontent.com/NEONS-DESIGN/SatouSioBOT/refs/heads/main/img/logo.png"></p>
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
discord.py と yt-dlp を利用して構築された、高機能かつレスポンスの速いDiscord用音楽Botです。
非同期処理によるバックグラウンドでのメタデータ解析と、ローカルブラウザ（Firefox）のCookieを活用した強力な再生機能を備えています。

## ✨ 主な機能

* **幅広いプラットフォーム対応**: YouTube、ニコニコ動画、SoundCloudなどに対応。
* **高速な楽曲ロード**: 専用のプレフェッチワーカーがバックグラウンドでストリームURLを事前取得し、曲間のラグを最小限に抑えます。
* **高度な認証回避**: `cookiesfrombrowser` を利用し、普段使用しているFirefoxのCookieを自動参照。年齢制限やログイン必須の動画も設定不要で再生可能です（手動でのCookie更新作業は不要）。
* **サーバーごとの動的設定 (SQLite)**: 
  * 再生音量、キューの最大数、プレイリストの取得上限をサーバー単位で保存。
  * Bot専用の管理者権限をDiscord上からコマンドで付与・剥奪可能。
* **インタラクティブなUI**: Embedを利用した美しい再生パネルと、ページネーション付きのヘルプ画面。
* **デイリーログローテーション**: エラーやシステムログを日付ごとに自動分割して `log` フォルダに保存します。

---

## 🛠 準備
### 1. 外部依存ソフトのインストール
このBotを動作させるには、システムに [**FFmpeg**](https://www.ffmpeg.org/)・[**Deno**](https://deno.com/)がインストールされており、パスが通っている必要があります。  
また、[**Node.js**](https://nodejs.org/)も必要になる場合があります。

### 2. ライブラリのインストール
以下のコマンドを実行して、必要なPythonライブラリを一括でインストールしてください。

```bash
pip install -r requirements.txt
```

## ⚙️ 設定ファイルの作成
Botの実行には、以下の3つのファイルを `main.py` と同じディレクトリに配置する必要があります。

### ① `.env` (APIキー・環境変数)
```env
discord_api = "あなたのDISCORD_BOT_TOKEN"
# 現在不要 (アーカイブ用に残してあります。)
#bitly_api = "あなたのBITLY_ACCESS_TOKEN"

# 現在不必要 (アーカイブ用に残してあります。)
# YouTubeのボット検知回避用
#po_token = "YOUR_PO_TOKEN"
#visitor_data = "YOUR_VISITOR_DATA"
```

### ② `config.ini` (Bot動作設定)
```ini
[MusicBot]
# 初期音量（0.0～2.0の範囲で指定）
default_volume = 0.25
# キューの最大数（曲の追加制限）
default_queue_limit = 50
# プレイリストの最大数（ユーザーごとの保存制限）
default_playlist_limit = 10
# ブラウザ偽装の際に渡すカスタムヘッダー（アクセス制限回避用）
user_agent = "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
# SQLiteデータベースのファイルパス
database_path = data.db
```

---

## 🚀 実行方法
```bash
python main.py
```
または、start.batを起動してください。

---

## ⚠️ 注意事項・セキュリティに関する重要なお知らせ
本ボットを利用する際は、以下の点に十分ご注意ください。
* **YouTubeの制限**: YouTube側の仕様変更により、再生に失敗する（403エラー等）場合は、yt-dlp関連を最新のものに更新してください。
* **履歴削除機能 (/purge)**: DiscordのAPI制限により、2週間（14日）以上経過したメッセージを一括削除することはできません。
* **認証情報の取り扱いについて**: Cookie や PO Token は、あなたの本人確認を行うための非常に重要な情報です。これらが第三者に渡ると、アカウントの不正利用や個人情報の流出につながる恐れがあります。スクリーンショットやログ、ソースコードの共有時にこれらが含まれないよう、厳重に管理してください。
* **免責事項**: 本ソフトウェアの使用によって生じた、いかなる損害（データの損失、アカウントの停止、金銭的被害など）についても、開発者は一切の責任を負いません。 全て利用者の自己責任において使用するものとします。
* **非公式ツールであることの理解**: 本ツールは公式のサービス提供者が提供するものではありません。仕様変更により突然利用できなくなったり、予期せぬ挙動が発生したりする可能性があることをあらかじめご了承ください。

---

## 📄 ライセンス
このプロジェクトは [**MITライセンス**](https://github.com/NEONS-DESIGN/SatouSioBOT/blob/main/LICENSE) に基づいて公開されています。  
Copyright © 2026 NEONS-DESIGN
