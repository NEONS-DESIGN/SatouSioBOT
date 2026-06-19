# CLAUDE.md

このファイルは、本リポジトリで作業する Claude Code (claude.ai/code) 向けのガイダンスを提供します。

## 詳細設計 → SPEC.md

**非自明な作業の前に [`SPEC.md`](SPEC.md) を読むこと。** SPEC.md は AIエージェント向けの密度の高い設計書（各モジュールの責務、track dict のスキーマ、プリフェッチ/再生の制御フロー、DBスキーマ、config キー、落とし穴）で、全ソースを読まずにシステムを把握できるよう書かれており、トークン節約になります。記載された挙動を変更したら `SPEC.md` も更新してください。本ファイル（CLAUDE.md）は俯瞰、SPEC.md は詳細という役割分担です。

## 概要

SatouSioBOT（砂糖塩）は `discord.py` + `yt-dlp` で構築された Discord 音楽Botです。YouTube・ニコニコ・SoundCloud などから音声をストリーミング再生します（最終的なメディアのローカルダウンロードは行いません）。コメント・Embed・ユーザー向け文字列はすべて日本語なので、編集時もそれに合わせてください。

## コマンド

```bash
pip install -r requirements.txt   # 依存インストール (Python 3.13.x)
python main.py                    # Bot起動 (Windowsなら start.bat のダブルクリックでも可)
```

テストスイート・Lint・ビルド工程はありません。`tools/hatch_build.py` はパッケージング用の hatch ビルドフックで、通常の開発には不要です。

### 実行時の前提（PATHが通っている必要あり）
- **FFmpeg** — 音声のトランスコード/ストリーミング。
- **Deno** — yt-dlp の `js_runtimes`（署名/EJS抽出）に必要（`YTDLP_OPTIONS` 参照）。環境によっては Node.js も必要。

### 必須の設定ファイル（`main.py` と同階層、いずれも gitignore 対象）
- `.env` — `discord_api = "DISCORD_BOT_TOKEN"`（`os.getenv("discord_api")` で読込）。
- `config.ini` — `[MusicBot]` セクション。`module/options.py` でパース。全キーが `Config` にハードコードのフォールバックを持つため、キー欠落は安全だがファイルが無いと全項目デフォルトになる。
- `data.db` — SQLite DB。初回起動時に自動生成。

## アーキテクチャ

エントリポイント `main.py` が `commands.Bot` のサブクラスと全トップレベルのスラッシュ/ハイブリッドコマンドを定義し、それ以外はすべて `module/` 配下にあります。コマンドは **ハイブリッドコマンド**（`@bot.hybrid_command`）なので `/slash` と `/` プレフィックスのテキストコマンドの両方で動作し、`setup_hook` で一度だけ同期されます。

### 音楽エンジン（`module/music.py`）— 中核
状態はモジュールグローバルの `server_music_data: dict[int, GuildMusicPlayer]` にギルド単位で保持されます。`ensure_guild_data(guild_id, bot)` が唯一のアクセサで、プレイヤーを遅延生成しプリフェッチワーカーを起動します。他所で `GuildMusicPlayer` を直接生成しないこと。

主要部品と非自明なフロー:
- **track はプレーンな dict**（クラスではない）。`url`、`stream_url`、`ready_event`（asyncio.Event）、`is_fetching`、`error`、`http_headers` などを持ち、パイプライン全体でこの dict を in-place で書き換える。
- **プリフェッチワーカー**（`_prefetch_worker`、ギルド毎に1 asyncio タスク）: `queue_updated_event` で起床し、`stream_url` 未取得の先頭 track を yt-dlp で解決して、その track の `ready_event` をセットする。これが曲間の解決レイテンシを隠蔽する仕組み。
- **二段速度の解決**: 検索クエリとプレイリストはまず `FAST_META_OPTIONS`（`extract_flat`、メタデータのみ）で取得し、実ストリームURLは後でワーカーが解決する。**単曲URL** は `play_music` 内で前もって完全解決され、`stream_url` をセットし `ready_event` を即発火する（ワーカーをバイパス）。
- **プロセス分離 + キャッシュ**: `extract_info_process` は `ProcessPoolExecutor`（シングルトン、`MAX_WORKER_THREADS` ワーカー）内で yt-dlp を実行し、ブロッキング/CPU重い抽出をイベントループから隔離する。`fetch_track_info` がそれを `aiocache` のインメモリTTLキャッシュ（`CACHE_TTL`）でラップする。プールは子プロセス毎に `_ytdl_instances` 経由で `YoutubeDL` インスタンスを使い回す。
- **再生チェーン**: `play_next_song` はキューを pop し、track の `ready_event` を待ち、`YTDLSource`（`FFmpegPCMAudio` を包む `PCMVolumeTransformer`）を生成して再生する。FFmpeg の `after=` コールバックが `asyncio.run_coroutine_threadsafe` で `play_next_song` を再スケジュールする（コールバックは非asyncスレッドで実行されるため）。ループ/エラースキップは再帰ではなく `while True` の反復で処理する。
- **クリーンアップ**: `main.py` の `on_voice_state_update` が強制切断を検知して `player.cleanup()`（ワーカーをcancel、キューをクリア）を呼ぶ。`/leave` も同様の処理を明示的に行う。

### 認証 / 抽出（`module/options.py`）
`cookiesfrombrowser = ('firefox',)` は yt-dlp が実行時に **ホストの Firefox プロファイルの Cookie** を読むことを意味する — 手動の Cookie ファイル無しで年齢制限/ログイン必須動画を再生できるのはこのため。ホストに Firefox がインストールされている必要がある。`ImpersonateTarget.from_str('chrome')` + `impersonate` が TLS フィンガープリント偽装を担い、`force_ipv4` やカスタム `User-Agent`/ヘッダーも同じアクセス制限回避の一部。

### 永続化（`module/sqlite.py`）
単一の長命 `aiosqlite` 接続（モジュールグローバル `_connection`）を1つの `asyncio.Lock` で保護。WAL + `synchronous=NORMAL`。テーブルは2つ: `server_data`（ギルド単位の volume / queue_limit / playlist_limit）と `bot_admins`（ギルド単位のBot操作権限付与）。`init_db()` は `on_ready` から呼ばれ、`ALTER TABLE ADD COLUMN` を試みて "duplicate column name" を握り潰すことで自己マイグレーションする。全クエリは `async sql_execution(query, params)` を通り、行リストを返すか失敗時 `None` を返す。

### 権限（`module/setting.py`）
`/setting` コマンドは `check_admin_permission` でゲートされる: ユーザーが Discord のサーバー管理者である **か**、そのギルドの `bot_admins` に登録されていれば true。`setup_setting_commands(bot)` が起動時に `/setting` グループ一式を登録する。

### UI / Embed（`module/embed.py`）
Discord向けの全メッセージはここの `*_embed` ヘルパーコルーチンが生成する。コマンド側は Embed をインラインで組まずこれらを呼ぶ。`main.py` の `SimplePaginator`（`discord.ui.View`）が複数ページの Embed（ヘルプ、キューリスト）を駆動する。

### コンソールログ（`module/logger.py` + `module/utils.py`）
`log/` 配下にデイリーローテーションのファイルログ、加えてコンソールハンドラ。`SpinnerAwareHandler` は共有モジュールグローバル `logger.spinner_active` / `logger.spinner_line` を通じて `utils.py` の `loading_spinner` アニメーションと協調し、進行中スピナーがログ行で崩れないようにする。`module/color.py` はコンソール出力専用の ANSI カラー定数を持つ（Discord Embed の色ではない — そちらは `color.Embed`）。

## 規約
- **インデントはタブ**（スペースではない）、コードベース全体で統一。
- イベントループは `main.py` の import 時に **winloop**（Windows）または **uvloop**（Unix）へ差し替えられる。どちらも optional で、無ければ標準ループにフォールバックする。
- 終了処理: `SatouSioBot.close()`（`main.py`）が全プレイヤーの cleanup、`close_db()`、`close_http_session()`、`shutdown_process_pool()` を呼び、ProcessPool 子プロセス・DB接続・aiohttp セッションを解放する。discord.py が正常終了/Ctrl+C 時に `close()` を呼ぶため、README記載の「プロセスが残る」問題はこの正常系では改善される（強制kill時は走らない）。
