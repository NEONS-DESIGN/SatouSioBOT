<h1 align="center">SatouSioBOT</h1>
<p align="center"><img width="140" src="https://raw.githubusercontent.com/NEONS-DESIGN/SatouSioBOT/refs/heads/main/img/logo.png"></p>
<p align="center">砂糖塩という、Discordの音楽再生Botです。できるだけ起動するだけで使用ができるように制作されています。</p>

<p align="center">
  <a href="https://www.python.org/downloads/release/python-31312/"><img src="https://img.shields.io/badge/Python-v3.13.x-ffde57" alt="Python"></a>
  <a href="https://github.com/Rapptz/discord.py"><img src="https://img.shields.io/badge/Discord.py-v2.7.1-3498db" alt="Discord.py"></a>
  <a href="https://github.com/yt-dlp/yt-dlp"><img src="https://img.shields.io/badge/yt--dlp-v2026.08.19-FF0000" alt="yt-dlp"></a>
  <a href="https://ffmpeg.org/"><img src="https://img.shields.io/badge/ffmpeg-v8.1-242424" alt="FFmpeg"></a>
  <a href="https://deno.com/"><img src="https://img.shields.io/badge/Deno-v2.7-70ffaf" alt="Deno"></a>
</p>

---

## 🎵 概要
discord.py と yt-dlp を利用して構築された、レスポンスの速さを重視したDiscord用音楽Botです。
yt-dlp による解析を別プロセスで並列実行し、ローカルブラウザ（Firefox）のCookieを活用して安定した再生を行います。

## ✨ 主な機能

* **幅広いプラットフォーム対応**: YouTube、ニコニコ動画、SoundCloudなど、yt-dlp が対応するサイトのURL・プレイリストURL・曲名検索に対応。
* **高速な再生開始**: 応答・ボイスチャンネル接続・曲の解析を並行して進めます。解析は専用の子プロセスで行うため、解析中も Bot の応答が止まりません。
* **曲間の待ち時間を短縮**: キューの先頭2曲分のストリームURLを先読みし、スキップや次曲への切り替えをすぐに行います。
* **Cookieの自動参照**: `cookiesfrombrowser` で普段使用しているFirefoxのCookieを自動で読み込みます。年齢制限やログイン必須の動画も、手動でのCookie書き出し作業なしで再生できます。
* **サーバーごとの設定保存 (SQLite)**:
  * 再生音量、キューの最大数、プレイリストから追加する曲数をサーバー単位で保存。
  * Bot専用の操作権限 (Bot管理者) をDiscord上からコマンドで付与・剥奪可能。
* **見やすいUI**: Embedによる再生パネル、URLリンク付きのキュー一覧、ページ送りボタン付きのヘルプ画面。Botをメンションすると `/help` の案内を表示します。
* **日付ごとのログ保存**: エラーやシステムログを日付ごとに自動分割して `log` フォルダに保存します。(30日分を保持)

---

## 💬 コマンド一覧
スラッシュコマンドとして使用できます。`/p 曲名` のように、通常のメッセージとして送信しても動作します。

### 再生・基本操作
| コマンド | 説明 |
|---|---|
| `/help` | コマンドの一覧と使い方を表示します。(本人のみに表示) |
| `/p [URL・曲名]` | 曲を再生します。再生中の場合はキューに追加します。プレイリストURLにも対応しています。 |
| `/pause` | 再生中の曲を一時停止します。 |
| `/resume` | 一時停止中の曲を再開します。 |
| `/replay` | 現在再生中の曲を最初から再生し直します。 |
| `/skip` | 再生中の曲をスキップします。 |

### キュー・音量
| コマンド | 説明 |
|---|---|
| `/qlist` | キューに入っている曲を一覧表示します。(10件ごとにページ送り) |
| `/clear [開始] [終了]` | キューの曲を削除します。引数なしで全件、`/clear 3` で先頭から3件、`/clear 2 5` で2〜5番目を削除します。 |
| `/loop` | キューのループ再生を切り替えます。 |
| `/sh` | キューの中身をシャッフルします。 |
| `/vol [1-200]` | 再生音量 (%) を変更します。設定はサーバーごとに保存されます。 |

### Bot管理・設定
| コマンド | 説明 | 必要な権限 |
|---|---|---|
| `/move` | Botを自分のいるボイスチャンネルへ移動させます。 | - |
| `/leave` | Botをボイスチャンネルから退出させ、キューを削除します。 | - |
| `/purge [件数]` | チャンネルのメッセージを一括削除します。(1〜50件、既定50件) | メッセージの管理 |
| `/setting admin add/remove [メンバー]` | Bot管理者の付与・剥奪を行います。 | サーバー管理者 または Bot管理者 |
| `/setting limit queue [1-50]` | キューの最大曲数を設定します。 | サーバー管理者 または Bot管理者 |
| `/setting limit playlist [1-50]` | プレイリストURLから追加する最大曲数を設定します。 | サーバー管理者 または Bot管理者 |

> [!TIP]
> キューに曲が無くなると、Botは自動でボイスチャンネルから退出します。

---

## 🛠 準備

### 1. 外部ソフトウェアのインストール
以下をインストールし、FFmpeg と Deno は環境変数 `PATH` を通してください。

| ソフトウェア | 用途 |
|---|---|
| [**Python 3.13.x**](https://www.python.org/downloads/) | Bot本体の実行 |
| [**FFmpeg**](https://www.ffmpeg.org/) | 音声のデコード・ストリーミング |
| [**Deno**](https://deno.com/) | YouTube の再生用署名の解読 (yt-dlp が使用) |
| [**Firefox**](https://www.mozilla.org/firefox/) | Cookie の参照元。**YouTube にログインした状態**にしておいてください。 |

> [!IMPORTANT]
> 高速化のため、YouTube の取得設定はログイン済みのCookieがあることを前提にしています。Firefox で YouTube にログインしていない場合、取得に失敗したり、予備設定に切り替わって遅くなったりすることがあります。

### 2. Discord Bot の作成
1. [Discord Developer Portal](https://discord.com/developers/applications) でアプリケーションを作成し、「Bot」ページでトークンを発行します。
2. 同じページの **Privileged Gateway Intents** で **MESSAGE CONTENT INTENT** を有効にします。(無効のままだと起動時にエラーになります)
3. 「OAuth2」→「URL Generator」で、スコープに `bot` と `applications.commands` を選択します。
4. Bot Permissions で以下を選択し、生成されたURLからサーバーに招待します。
   * メッセージを送信 / 埋め込みリンク / メッセージ履歴を読む
   * 接続 / 発言
   * メッセージの管理 (`/purge` を使う場合)

### 3. ライブラリのインストール
プロジェクトのフォルダで仮想環境 (venv) を作成し、必要なライブラリをインストールしてください。

```bash
py -3.13 -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
```

`start.bat` / `start.ps1` は `venv` があれば自動でそちらを使用します。venv を作らない場合は、`py -3.13 -m pip install -r requirements.txt` でインストールしてください。

---

## ⚙️ 設定ファイルの作成
以下のファイルを `main.py` と同じフォルダに配置します。`data.db` (SQLiteデータベース) は初回起動時に自動で作成されます。

### ① `.env` (Botトークン)
```env
discord_api = "あなたのDISCORD_BOT_TOKEN"
```

### ② `config.ini` (Bot動作設定)
リポジトリに同梱されています。必要に応じて値を変更してください。範囲外の値は自動で範囲内に丸められ、不正な値は既定値になります。

```ini
[MusicBot]
# 初期音量（0.0～2.0 の範囲で指定。1.0 = 100%）
default_volume = 0.25
# キューに入れられる最大曲数の既定値（1～50）
default_queue_limit = 50
# プレイリストURLから1回で追加する最大曲数の既定値（1～50）
default_playlist_limit = 10
# ストリームURLの取得に失敗したときの最大試行回数
max_retries = 3
# yt-dlp で曲を解析する子プロセスの数（同時に解析できる曲数）
max_worker_threads = 4
# 曲名検索・プレイリストの結果をキャッシュする秒数（例: 14400 は4時間）
cache_ttl = 14400
# yt-dlp に渡す User-Agent（空欄なら yt-dlp 既定の最新ブラウザ UA を使用。通常は空欄推奨）
user_agent =
# SQLiteデータベースのファイルパス（相対パスは main.py のフォルダ基準）
database_path = data.db
```

| キー | 既定値 | 範囲 | 説明 |
|---|---|---|---|
| `default_volume` | `0.25` | 0.0〜2.0 | サーバーで `/vol` を未設定のときの音量 (1.0 = 100%) |
| `default_queue_limit` | `50` | 1〜50 | キューに入れられる最大曲数の既定値 (`/setting limit queue` で上書き) |
| `default_playlist_limit` | `10` | 1〜50 | プレイリストURLから1回で追加する最大曲数の既定値 (`/setting limit playlist` で上書き) |
| `max_retries` | `3` | 1以上 | ストリームURLの取得に失敗したときの最大試行回数 |
| `max_worker_threads` | `4` | 1以上 | yt-dlp で曲を解析する子プロセスの数 (同時に解析できる曲数) |
| `cache_ttl` | `14400` | 0以上 | 曲名検索・プレイリストの結果をキャッシュする秒数 |
| `user_agent` | (空欄) | - | yt-dlp に渡す User-Agent。空欄なら yt-dlp の既定値を使用 |
| `database_path` | `data.db` | - | SQLiteデータベースの保存先 (相対パスは `main.py` のフォルダ基準) |

---

## 🚀 実行方法
`start.bat` をダブルクリックしてください。PowerShellのウィンドウで起動します。(PowerShell 7 があれば優先して使用し、無ければ Windows PowerShell を使用します)
PowerShellから直接起動する場合は `.\start.ps1` を実行してください。

コマンドから起動する場合は、以下を実行します。

```bash
venv\Scripts\python.exe main.py
```

### 終了方法
起動したウィンドウで `Ctrl + C` を押すと、ボイスチャンネルからの切断やデータベース接続の後片付けを行ってから終了します。
ウィンドウを閉じるなど、それ以外の方法で終了した場合も、曲の解析用の子プロセスは自動で終了します。

---

## 🩺 トラブルシューティング
| 症状 | 対処 |
|---|---|
| 再生に失敗する・403エラーが出る | YouTube側の仕様変更の可能性があります。`venv\Scripts\python.exe -m pip install -U yt-dlp yt-dlp-ejs` で yt-dlp を更新してください。 |
| YouTube の曲だけ取得が遅い・失敗する | Firefox で YouTube にログインしているか、Deno に `PATH` が通っているか (`deno --version`) を確認してください。 |
| 音が出ない | FFmpeg に `PATH` が通っているか (`ffmpeg -version`) を確認してください。 |
| 起動時に `PrivilegedIntentsRequired` エラーが出る | Developer Portal で MESSAGE CONTENT INTENT を有効にしてください。 |
| スラッシュコマンドが表示されない | 招待URLのスコープに `applications.commands` が含まれているか確認してください。反映まで時間がかかる場合は、Discord を再起動してください。 |
| エラーの詳細を確認したい | `log` フォルダのログファイル (`bot.log`、過去分は日付付き) を確認してください。 |

---

## ⚠️ 注意事項・セキュリティに関する重要なお知らせ
本ボットを利用する際は、以下の点に十分ご注意ください。
* **YouTubeの制限**: YouTube側の仕様変更により、再生に失敗する（403エラー等）場合は、yt-dlp関連を最新のものに更新してください。
* **履歴削除機能 (/purge)**: DiscordのAPI制限により、2週間（14日）以上経過したメッセージを一括削除することはできません。
* **認証情報の取り扱いについて**: Botトークン (`.env`) や Cookie は、あなたの本人確認を行うための非常に重要な情報です。これらが第三者に渡ると、アカウントの不正利用や個人情報の流出につながる恐れがあります。スクリーンショットやログ、ソースコードの共有時にこれらが含まれないよう、厳重に管理してください。
* **Botを動かすPCのアカウント**: Bot は Firefox に保存された YouTube のログイン情報を使用して動画を取得します。Bot経由での利用も、ログイン中のGoogleアカウントでの視聴として扱われます。
* **免責事項**: 本ソフトウェアの使用によって生じた、いかなる損害（データの損失、アカウントの停止、金銭的被害など）についても、開発者は一切の責任を負いません。 全て利用者の自己責任において使用するものとします。
* **非公式ツールであることの理解**: 本ツールは公式のサービス提供者が提供するものではありません。仕様変更により突然利用できなくなったり、予期せぬ挙動が発生したりする可能性があることをあらかじめご了承ください。

---

## 📄 ライセンス
このプロジェクトは [**MITライセンス**](https://github.com/NEONS-DESIGN/SatouSioBOT/blob/main/LICENSE) に基づいて公開されています。  
Copyright © 2026 NEONS-DESIGN
