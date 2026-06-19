# SPEC.md — SatouSioBOT 詳細設計

AIエージェント向けの詳細設計書。ソースを全読みせずこのファイルで全体把握できることを目的とする。実装変更時はこのファイルも更新すること。インデントは全ファイル**タブ**。ユーザー向け文字列・コメントは日本語。

## 1. 構成と依存関係

```
main.py            … エントリ。Botサブクラス + 全トップレベルコマンド + ページネーションUI + イベント
module/
  music.py         … 音楽エンジン中核（プレイヤー状態・プリフェッチ・再生チェーン）
  options.py       … config.ini読込 + yt-dlp/FFmpegオプション定義
  sqlite.py        … 永続SQLite接続・スキーマ・クエリAPI
  setting.py       … /setting コマンド群 + 権限判定
  embed.py         … 全Discord向けEmbed生成関数
  logger.py        … デイリーローテーションログ + スピナー対応ハンドラ
  utils.py         … URL短縮・スピナー・時間整形等のヘルパ
  color.py         … ANSI(コンソール)/Embed/Bootstrap のカラー定数
tools/             … 開発補助（通常実行には不要）
  get-po-token.js          … PO Token生成（Node, youtube-po-token-generator）
  hatch_build.py           … hatchビルドフック（パッケージング用）
  ytdlp_options_reference.py … yt-dlpオプションの参照メモ
```

依存方向（循環なし、ただし下記の遅延importに注意）：
`main` → `embed, logger, music, setting, sqlite`
`music` → `color, embed, logger, options, sqlite, utils`
`embed` → `color, utils`（`music_info_embed`内で`logger`を遅延import）
`setting` → `sqlite, embed`
`sqlite` → `logger, options`
`utils` → `color, logger`（`import module.logger as _logger_module` で可変グローバル共有）
`options` → `yt_dlp.networking.impersonate`（import時に実行）

## 2. ライフサイクルとイベント (main.py)

- **起動**: `_apply_fast_event_loop()` をimport時実行 → win32は`winloop`、他は`uvloop`、無ければ標準ループ（いずれも optional, ImportErrorは握り潰し）。
- `load_dotenv()` → `os.getenv("discord_api")` でトークン取得（キー名は **`discord_api`**）。
- `setup_daily_logger()` → `logger = get_bot_logger()`（名前 `"MusicBot"`）。
- `SatouSioBot(commands.Bot)`: `command_prefix="/"`, `intents.default() + message_content=True`, `help_command=None`。
  - `setup_hook()`: `setup_setting_commands(self)` で /setting 群を登録 → `tree.sync()`。
  - `close()`: 終了時オーバーライド。全 `GuildMusicPlayer.cleanup()` → `server_music_data.clear()` → `close_db()`（DB接続）→ `close_http_session()`（aiohttp）→ `shutdown_process_pool()`（子プロセス回収）→ `super().close()`。Ctrl+C/正常終了で discord.py が呼ぶ。
- `bot.run(_DISCORD_TOKEN, log_handler=None)`。

イベントハンドラ:
- `on_ready`: `await init_db()` → presence設定（Playing「音楽再生BOTです。 /help」）→ ログイン記録。**DB初期化はon_readyで行う**点に注意。
- `on_message`: bot発言は無視。botがメンションされたら`help_mention_embed`。最後に`process_commands`。
- `on_voice_state_update`: 対象が自Bot かつ `before.channel あり→after.channel なし`（強制切断）のとき `server_music_data.pop(guild_id)` → `player.cleanup()`。

`SimplePaginator(discord.ui.View)`: 複数Embedをページ送り。timeout=120s。embeds<3 で「最初へ/最後へ」ボタンを除去。タイムアウトで全ボタン無効化し`message.edit`。`self.message`は送信後に手動代入が必要。

## 3. コマンド一覧 (すべて hybrid command = スラッシュ/プレフィックス両対応)

| コマンド | 引数 | 権限/制約 | 動作概要 |
|---|---|---|---|
| `/help` | なし | ephemeral | `help_pages()` 3ページをPaginatorで表示 |
| `/p <urlか曲名>` | query:str (rename=`urlか曲名`) | guild_only, VC接続必須 | VC未接続なら接続、別チャンネルなら移動 → `play_music` |
| `/vol <1-200>` | volume:Range[1,200] | guild_only | `source.volume = volume/100`、`player.volume` 更新、DBへ UPSERT 保存 |
| `/loop` | なし | guild_only | `player.loop` トグル |
| `/sh` | なし | guild_only | dequeをlist化→`random.shuffle`→戻す |
| `/skip` | なし | guild_only | `vc.stop()`（after発火で次曲へ） |
| `/move` | なし | guild_only, VC接続必須 | Botを実行者のVCへ`move_to` |
| `/leave` | なし | guild_only | stop→disconnect→`pop`+`cleanup` |
| `/purge [件数=50]` | limit:Range[1,50] | guild_only, `manage_messages` | `channel.purge`。14日超は削除不可(Discord仕様) |
| `/qlist` | なし | guild_only | `queue_list_pages`（10曲/ページ） |
| `/pause` | なし | guild_only | 再生中なら`vc.pause()` |
| `/resume` | なし | guild_only | 一時停止中なら`vc.resume()` |
| `/clear [start] [end]` | start:int\|None=None, end:int\|None=None | guild_only | 引数なし=全削除 / 1つ=先頭からstart件 / 2つ=start〜end(1-indexed) |
| `/replay` | なし | guild_only | currentをstream_url=Noneで`appendleft`→`vc.stop()`で再取得再生 |
| `/setting` | （グループ） | guild_only | サブ未指定で`setting_help_embed` |
| `/setting admin add <user>` | user:Member | bot管理者権限 | `bot_admins`へINSERT OR IGNORE |
| `/setting admin remove <user>` | user:Member | bot管理者権限 | `bot_admins`からDELETE |
| `/setting limit queue <1-50>` | limit:int | bot管理者権限 | 1〜50検証 → `server_data.queue_limit` を UPSERT |
| `/setting limit playlist <1-50>` | limit:int | bot管理者権限 | 1〜50検証 → `server_data.playlist_limit` を UPSERT |

全トップレベルコマンドは `try/except` で囲み、例外時 `exception_embed(ctx, name, e)` + `logger.error`。多くが冒頭で `await ctx.defer()`（/help, /purge は `defer(ephemeral=True)`）。

## 4. データモデル (music.py)

### server_music_data: dict[int, GuildMusicPlayer]
ギルドID→プレイヤーのモジュールグローバル。**唯一の正規アクセサは `ensure_guild_data(guild_id, bot)`**（無ければ生成し、botが渡されればワーカー起動）。直接 `GuildMusicPlayer(...)` を他所で作らない。

### GuildMusicPlayer (`__slots__`)
| 属性 | 型 | 意味 |
|---|---|---|
| `guild_id` | int | |
| `bot` | commands.Bot | |
| `queue` | `deque[dict]` | トラックキュー |
| `queue_updated_event` | asyncio.Event | プリフェッチワーカー起床用 |
| `loop` | bool | ループ再生フラグ |
| `current` | dict\|None | 再生中トラック |
| `worker_task` | asyncio.Task\|None | プリフェッチワーカー |
| `volume` | float\|None | 音量キャッシュ。Noneなら次回再生時にDBから読込。`/vol` で更新 |

- `start_worker()`: 未起動/終了済みなら `_prefetch_worker` をタスク起動（名前 `prefetch_worker_{guild_id}`）。
- `cleanup()`: worker_taskをcancel、queue/event/current をクリア。

### track (プレーンdict、クラスではない。パイプライン全体でin-place変更)
```
url:         str    # 解決済み再生対象URL（webpage_url等から決定）
display_url: str    # 表示用URL（単曲URLは元入力、他はtrack_url）
title:       str
author_id:   int    # ctx.author.id
thumbnail:   str|None
duration:    int    # 秒
stream_url:  str|None   # FFmpegに渡す実ストリームURL。未取得はNone
http_headers: dict      # FFmpeg before_optionsに展開
error:       Exception|None  # 取得失敗時セット → 再生時スキップ
ready_event: asyncio.Event   # stream_url準備完了の通知
wait_msg:    discord.Message|None  # 「準備中」メッセージ（編集して幽霊メッセージ防止）
is_fetching: bool   # ワーカーの二重取得防止
```

## 5. 音楽エンジンの制御フロー (music.py) — 最重要

### 情報取得の二段速度
- **FAST_META_OPTIONS**（`extract_flat=True`）: 検索・プレイリストのメタデータのみ高速取得。stream_urlは後でワーカーが解決。
- **YTDLP_OPTIONS**: 単曲URLの完全取得（stream_url込み）。

### プロセス分離 + キャッシュ（再設計済み）
- `extract_info_process(query, is_fast)`: **子プロセス内で** yt-dlp実行。`_ytdl_instances` dictで `YoutubeDL` を fast/normal 別に使い回す（子プロセスごと）。
- `_get_process_pool()`: `ProcessPoolExecutor(max_workers=MAX_WORKER_THREADS)` のシングルトン。ブロッキング/CPU重い抽出をイベントループから隔離。`shutdown_process_pool()` で停止（`close()` から呼ぶ）。
- `fetch_track_info(query, is_fast)`: `run_in_executor(pool, extract_info_process, ...)`。**キャッシュ方針が is_fast で異なる**:
  - `is_fast=True`（メタデータ）: モジュールグローバル `_meta_cache = Cache(Cache.MEMORY)` に `CACHE_TTL` 秒キャッシュ。メタデータは失効しないため安全。
  - `is_fast=False`（ストリームURL）: **キャッシュしない**。毎回新しいURLを解決する。これにより `/replay`・ループでの再取得が確実に最新URLになり、stream_url 失効による403も回避する。
  - 旧実装は `@cached` で stream_url ごとキャッシュしていたため再取得が効かなかった（解消済み）。`CACHE_TTL` は実質メタデータ専用になった。

### play_music(ctx, url, bot) — キュー追加エントリポイント
1. `ensure_guild_data` でプレイヤー取得。`is_url`(http/https始まり), `is_playlist`(`list=` or `playlist` 含む), `is_idle`(current無し かつ 非再生中) を判定。
2. `is_idle` なら `preparing_audio_embed` で wait_msg 送信。
3. 取得分岐:
   - 単曲URL: `fetch_track_info(url, False)`（完全取得）→ `entries=[info]`。
   - プレイリスト/検索: 非URLは `ytsearch1:` を前置 → `fetch_track_info(.., True)`（高速）。`entries = info["entries"] or [info]`。`is_playlist_result = "entries"あり and 非ytsearch`。
4. DBから `queue_limit, playlist_limit` 取得（無ければ config デフォルト）。`available = queue_limit - len(queue)`。0以下なら上限エラー。プレイリストは `min(playlist_limit, available)` 件、他は `available` 件に切詰。
5. 各 entry で track dict を構築（url解決順: `webpage_url → original_url → watch?v={id} → url`、thumbnail: `thumbnail → thumbnails[-1].url`）。**単曲URLのみ** `stream_url`即セット+`is_fetching=True`+`ready_event.set()`（ワーカー不要）。`queue.append` 後 `queue_updated_event.set()`。
6. 通知: プレイリストは `playlist_added_embed`、アイドルでない単曲は `queue_added_embed`。
7. `is_idle` なら `bot.loop.create_task(play_next_song(ctx, bot))` で即再生開始。
8. 例外時 `load_error_embed(ctx, e, edit_msg=wait_msg)` で return。

### _prefetch_worker(player) — ギルド毎1タスク
無限ループ。`queue_updated_event.wait()` で起床 → queue先頭から「stream_url無し & error無し & is_fetching無し」のtrackを探す。無ければ event.clear()して0.5s sleep。見つかれば `is_fetching=True` にし、`MAX_RETRIES` 回まで `fetch_track_info(url, False)` を `loading_spinner` 付きで試行。結果が `entries` 形式なら先頭採用。`stream_url/http_headers/duration` をセット。最終失敗で `track["error"]=e`。最後に **必ず `track["ready_event"].set()`**。`CancelledError` でループ終了。

### play_next_song(ctx, bot) — 再生チェーン（再帰でなく while ループ）
1. player取得（無ければreturn）。
2. ループ先頭: `player.loop and player.current` なら current を stream_url=None でコピーし `queue.append`+`event.set()`（次周回で再取得再生）。
3. queue空 → `current=None`, VC接続中なら`disconnect`, `play_completed_embed`, `cleanup`, return。
4. `next_track = queue.popleft()`, `current = next_track`。
5. `ready_event` 未setなら wait（必要なら `preparing_audio_embed`）。
6. `track.error` あり → wait_msg削除, `skip_error_embed`, `continue`（次トラックへ）。
7. volume は `player.volume`（None時のみDB参照しキャッシュ、`/vol` で更新）→ `YTDLSource.from_track(track, player.volume)`。曲ごとのDB SELECT は廃止。
8. `after=_after_playing`: エラーログ後 `asyncio.run_coroutine_threadsafe(play_next_song(ctx,bot), bot.loop)`（**afterは非asyncスレッドで実行されるため必須**）。`vc.play(source, after=...)`。
9. `music_info_embed(ctx, source, len(queue), wait_msg)` → return。
10. ソース生成例外 → `playback_error_embed`, `continue`。

### YTDLSource(discord.PCMVolumeTransformer)
`from_track(track, volume)` クラスメソッド: `stream_url`無しなら`ValueError`。`http_headers`があれば各値から `"`・改行を除去（FFmpeg引数破壊防止）した上で `before_options` に `-headers "k: v\r\n..."` を追記。`FFmpegPCMAudio(stream_url, before_options, options, stderr=sys.stderr)` を包む。属性 `data, title, display_url`。リアルタイム音量は `vc.source.volume` 代入で変更可（/vol が利用）。

## 6. yt-dlp / 認証設定 (options.py)

- `Config`: `config.ini [MusicBot]` を読む。全キーにハードコードのフォールバックあり → ファイル/キー欠落でもデフォルト動作。`app_config` がシングルトン。
- 共通設定の要点:
  - **`cookiesfrombrowser=('firefox',)`**: 実行ホストのFirefoxプロファイルのCookieを実行時に読む。年齢制限/ログイン必須動画対応の核。**Firefoxインストール必須**。
  - `impersonate=ImpersonateTarget.from_str('chrome')`: TLSフィンガープリント偽装（curl-cffi）。
  - `js_runtimes={'deno': {}}`: 署名/EJS抽出に **Deno必須**（node はコメントアウト）。`remote_components=['ejs:github']`, `allow_remote_strings=True`。
  - `force_ipv4=True`, `source_address='0.0.0.0'`, カスタム`User-Agent`/`Accept-Language`。
  - `extractor_args`: youtube `player_client=['web_music', 'android']`（要素を分けること。`['web_music, android']` は1個の無効クライアント名になり効かない）、nicovideo `action_wait_time=['1.0']`。
  - `playlistend=50`, `extract_flat='in_playlist'`, `default_search='ytsearch'`。
- `FFMPEG_OPTIONS`: `before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -analyzeduration 0 -probesize 32"`, `options="-threads 2 -vn -sn"`。

## 7. 永続化 (sqlite.py)

- 単一の長命 `aiosqlite` 接続（グローバル `_connection`）+ 単一 `asyncio.Lock`。初回接続で `PRAGMA journal_mode=WAL` / `synchronous=NORMAL`。
- `init_db()`（on_readyから呼ぶ）: テーブル作成 + `ALTER TABLE ADD COLUMN` による自己マイグレーション（"duplicate column name" は握り潰し）。
- **全クエリは `sql_execution(query, params=()) -> list|None`** 経由。Lockで排他、行リストを返し、失敗時 `None` + error log。
- `close_db()`: 接続を閉じて `_connection=None`（`close()` から呼ぶ）。
- 値更新は **UPSERT**（`INSERT ... ON CONFLICT(guild_id) DO UPDATE SET col=excluded.col`）を使用。`/vol` と `/setting limit queue|playlist` がこの形（旧 `INSERT OR IGNORE`＋`UPDATE` の二発を1発に統合）。

### スキーマ
```sql
server_data(
  guild_id       INTEGER PRIMARY KEY UNIQUE NOT NULL,
  volume         REAL    DEFAULT {DEFAULT_VOLUME},
  queue_limit    INTEGER DEFAULT {DEFAULT_QUEUE_LIMIT},
  playlist_limit INTEGER DEFAULT {DEFAULT_PLAYLIST_LIMIT}
)
bot_admins(
  guild_id INTEGER NOT NULL,
  user_id  INTEGER NOT NULL,
  PRIMARY KEY(guild_id, user_id)
)
```
volumeは0.0〜2.0の実数で保存（/vol は `volume/100` を格納、UIは1〜200%）。

## 8. 権限・設定 (setting.py)

- `check_admin_permission(ctx) -> bool`: 実行者が **Discordサーバー管理者** または **`bot_admins`に登録済み** なら true。
- `setup_setting_commands(bot)`: `/setting`（hybrid_group）と admin/limit サブグループ・コマンドを登録。各 limit コマンドは 1〜50 をバリデーション、`INSERT OR IGNORE`→`UPDATE` でDB更新。

## 9. Embed (embed.py)

全ユーザー向けメッセージはここの `*_embed` コルーチンが生成。コマンド側でinline構築しない。
- `_send(ctx, title, description=None, color=_GREEN, ephemeral=False, edit_msg=None)`: 共通送信/編集。`edit_msg`失敗時は新規送信フォールバック。
- `_music_embed_base`: 楽曲/プレイリスト追加通知の土台（タイトル/URL/サムネ画像/Requested by フッタ）。
- `help_pages()`: 3ページ（再生基本 / キュー音量 / 管理設定）。`help_mention_embed` は `/help` への案内（ハードコードのコマンドID `</help:1140613857224687616>` を含む）。
- `queue_list_pages(queue)`: 10曲/ページ、`**i.** title `[mm:ss]``。
- 主なエラー系: `exception_embed`(共通), `load_error_embed`, `skip_error_embed`, `playback_error_embed`, `permission_error_embed`, `user_not_here_embed`, `bot_not_in_vc_embed` 等。
- `_FALLBACK_THUMBNAIL`: 再生中Embed画像のフォールバック（Unsplash URL）。

## 10. ログ・スピナー (logger.py + utils.py)

- `setup_daily_logger()`: `log/` に `TimedRotatingFileHandler`（midnight, backupCount=30, suffix `%Y_%m_%d.log`）+ コンソール `SpinnerAwareHandler`。root level INFO。`ConsoleFilter` が `discord` 由来の WARNING未満をコンソールから除外（ファイルには残る）。
- **スピナー連携**: `logger.py` のモジュールグローバル `spinner_active` / `spinner_line` を `utils.loading_spinner` が更新（`import module.logger as _logger_module` で参照）。`SpinnerAwareHandler.emit` はスピナー表示中ならスピナー行を消去→ログ出力→スピナー再描画し、表示崩れを防ぐ。
- `get_bot_logger(name="MusicBot")`。
- **パフォーマンス計測（一時）**: `logger.perf(label, ms)` が `[PERF] label: NN.Nms` を出力。`logger.PERF_LOG = False` で全停止。`/p` の1曲目で `VC接続/メタ抽出/本抽出(stream_url)/stream_url待ち/FFmpeg起動/★総計 コマンド→再生開始` が時系列で出る。抽出の純時間は `fetch_track_info` 内部で計測（スピナーのポーリング遅延を含まない）。レスポンス最適化の判断材料用で、確定後は削除可。

### utils.py
- `_get_session()`: aiohttp `ClientSession` シングルトン（再利用）。
- `shorten_url(url)`: 100文字超のみ TinyURL API で短縮、失敗時は原URL。
- `close_http_session()`: 共有セッションを閉じる（`close()` から呼ぶ）。
- `loading_spinner(task, message)`: コルーチン/Taskの完了待ちでコンソールにアニメ表示。完了/キャンセル/例外で出力切替、スピナーフラグをリセットし `task.result()` を返す。
- `play_time(duration)`: 秒→`MM:SS` または `HH:MM:SS`（0/None→`00:00`）。
- （旧 `download_file` / `get_id` は未使用のため削除済み。）

## 11. カラー定数 (color.py)
- `Color`: ANSI（コンソール専用）。
- `Embed`: Discord Embed用 16進（RED/GREEN/BLUE/YELLOW/ORANGE/PURPLE…）。**embed.py は `Embed` を使用、コンソールは `Color`**。混同しない。
- `Bootstrap`: 未使用に近いカラーセット。

## 12. 設定・前提・既知の注意点

### config.ini `[MusicBot]` キー（→ options.Config 属性 / デフォルト）
`default_volume`→DEFAULT_VOLUME/0.25 ・ `default_queue_limit`→DEFAULT_QUEUE_LIMIT/50 ・ `default_playlist_limit`→DEFAULT_PLAYLIST_LIMIT/10 ・ `max_retries`→MAX_RETRIES/3 ・ `max_worker_threads`→MAX_WORKER_THREADS/4（ProcessPool幅）・ `cache_ttl`→CACHE_TTL/10800（コード既定）｜14400（README例）。**再設計後は CACHE_TTL はメタデータキャッシュ専用**（stream_url は非キャッシュ）・ `user_agent`→USER_AGENT ・ `database_path`→DATABASE_PATH/data.db。

### 必須ファイル（同階層、gitignore対象）
- `.env`: `discord_api = "TOKEN"`。
- `config.ini`: 上記キー。
- `data.db`: 初回自動生成。

### 外部前提（PATH必須）
FFmpeg、Deno（yt-dlp js_runtimes）。環境によりNode.js（PO Token生成ツール）。Firefox（Cookie読込）。Python 3.13.x。

### 起動・停止
`python main.py` または `start.bat`。`SatouSioBot.close()` で ProcessPool 子プロセス・DB接続・aiohttpセッションを解放するようにしたため、Ctrl+C/正常終了時のプロセス残存は改善されているはず（discord.py が `close()` を呼ぶ正常系のみ。プロセスを強制killした場合は当然走らない）。

### Discord仕様の制約
`/purge` は14日超のメッセージを一括削除不可。

### テスト・Lint
テストスイート/Lint/ビルド工程は無い。`tools/hatch_build.py` は通常開発に不要。
