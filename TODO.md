# TODO.md — 作業引き継ぎメモ（AIエージェント向け）

別PC・クリーンセッションでの再開用。最初に [`SPEC.md`](SPEC.md)（詳細設計）と [`CLAUDE.md`](CLAUDE.md)（俯瞰）を読むこと。本ファイルは「いま何をしている途中か／次に何をするか」だけを記す。

最終更新: 2026-06-19

---

## ⚠️ 最優先: 未コミットの変更がある

直近コミットは `617fae9 Update README.md`。**以下はすべて作業ツリー上の未コミット変更**。別PCへ移す前に commit & push（または stash/転送）すること。クリーン環境で `git pull` し忘れると下記の作業が消える。

```
M main.py  M module/embed.py  M module/logger.py  M module/music.py
M module/options.py  M module/setting.py  M module/sqlite.py  M module/utils.py
?? CLAUDE.md  ?? SPEC.md  ?? TODO.md
```

`.env` / `config.ini` / `data.db` は gitignore 対象。別PCでは別途用意が必要（`.env` の `discord_api`、`config.ini` の `[MusicBot]`）。実行前提: PATH に **FFmpeg / Deno**（+環境により Node.js）、ホストに **Firefox**（Cookie読込）。Python 3.13.x、依存は `pip install -r requirements.txt`。

---

## これまでの経緯（完了済み）

### 1. ドキュメント整備（完了）
- `CLAUDE.md`（俯瞰）/ `SPEC.md`（AIエージェント向け詳細設計）を新規作成。両方とも日本語。挙動変更時は SPEC.md も追従更新する規約。

### 2. 全体コードレビュー → 修正・再設計（完了・検証済み）
`py_compile` と `import main`（venv）で検証済み。主な変更:
- **バグ**: `options.py` の `player_client` を `['web_music, android']`→`['web_music', 'android']`（要素分割。旧値は無効クライアント名1個で効いていなかった）。`/clear` の引数型 `int=None`→`int|None=None`。
- **終了処理**: `SatouSioBot.close()` を追加し、全player cleanup→`close_db()`→`close_http_session()`→`shutdown_process_pool()`。README「プロセスが残る」問題の正常系対策。`sqlite.close_db()` / `utils.close_http_session()` / `music.shutdown_process_pool()` を新設。
- **キャッシュ再設計**: `fetch_track_info` の `@cached` を廃止。`is_fast=True`（メタ）のみ `_meta_cache`(aiocache MEMORY) に `CACHE_TTL` キャッシュ、`is_fast=False`（stream_url）は**非キャッシュ＝毎回新規解決**（/replay・ループで確実に最新URL、403回避）。
- **最適化**: `/vol`・`/setting limit` を UPSERT（`ON CONFLICT DO UPDATE`）1発に統合。volume を `GuildMusicPlayer.volume` にキャッシュ（曲ごとの DB SELECT 廃止、/vol で更新）。
- **デッドコード除去**: `utils.download_file` / `utils.get_id`、未使用 import、`GuildMusicPlayer.voice_client`（未読属性）。`any`→`typing.Any`。`YTDLSource` のヘッダ値から `"`・改行除去。`SimplePaginator` の prev/next をラップ→クランプ化。

### 3. レスポンス計測の足場（完了）← いまここ
`/p` から発声までのレイテンシを計測する `[PERF]` ログを実装した。**まだ計測データは未取得**。

---

## 🔜 次にやること（再開時のスタート地点）

### STEP 1: 計測データを採取する（ユーザーが実機実行）
ユーザーに以下を依頼し、`[PERF]` ログ（特に `★総計` と `メタ抽出`/`本抽出`）を集める:
1. `python main.py` で起動（winloop 適用済み）。
2. VCに入り `/p <曲名>` を **コールド初回** と **ウォーム2回目以降** で複数回。
3. **URL直指定**でも実行（`本抽出`1回のみ＝比較対象）。

出力される `[PERF]` 行（時系列）:
`VC接続`/`VC移動` → `メタ抽出(yt-dlp)` → `本抽出(yt-dlp/stream_url)` → `stream_url待ち` → `FFmpeg起動` → `★総計 コマンド→再生開始`。
- 抽出の純時間は `fetch_track_info` 内部で計測（スピナーの0.2sポーリング遅延を含まない）。
- `★総計 −（各段合計）` ≒ スピナー＋スケジューリングのオーバーヘッド。
- ON/OFF は `module/logger.py` の `PERF_LOG = True` 一箇所。

### STEP 2: データを見て最適化を確定 → 実装
計測で「接続支配 or 抽出支配 or Deno署名解決が重い」を判別してから、効果順に実装する。候補（ユーザー合意済みの方針あり）:

| # | 施策 | 実装メモ | リスク |
|---|---|---|---|
| 1 | **アイドル時の検索を full 抽出1発化**（メタ→ワーカーの二重抽出を解消） | `play_music`: `resolve_full_now = (is_url and not is_playlist) or (is_idle and not is_url)` を作り、検索でも `fetch_track_info(f"ytsearch1:{url}", False)` で stream_url まで一発取得→`entries[0]` から track 構築し `ready_event.set()`。**非アイドル（再生中の追加）はメタ先行のまま**（ユーザー曰く二重抽出は「追加した」を早く見せるため。1発化で速いなら不要と本人談）。 | 低 |
| 2 | **VC接続と抽出を並行化** | `bot_play` で `connect()` をタスク化し裏で進める。`play_music(..., connect_task=...)` を受け取り、再生直前（`bot.loop.create_task(play_next_song)` の前）で `await connect_task`。新規接続時は `is_idle=True` 確定なので、is_idle 判定を `connect_task` 有無で分岐させると vc=None 問題を回避できる。 | 低 |
| 3 | **スピナーのポーリング遅延除去**（毎回~100ms×回数） | `utils.loading_spinner` を「別タスクでアニメ＋本体は直接 `await task`」に変更。**見た目・機能は維持**（ユーザー承認済み）。`while not task.done(): sleep(0.2)` が完了後に最大200ms待つのが問題。 | 低 |
| 4 | **起動時にプール＆YoutubeDLを事前ウォーム** | `on_ready` で `fetch_track_info("ytsearch1:test", True)` を1回投げ、ProcessPool spawn＋子プロセスの yt_dlp import＋YoutubeDL生成のコールドスタートを初回 `/p` から外す。 | 低 |
| 5 | **子プロセス戻り値のトリム** | `extract_info_process` が全formats等の巨大dictを返す→pickle/IPC が重い。子側で必要キー（url, http_headers, title, duration, thumbnail(s), id, webpage_url, original_url, entries[*]の同等）だけに削って返す。**呼び出し側が参照する全フィールドを壊さないこと**（`play_music` の track 構築は多数のキーを見る。SPEC §5 のtrack構築順を参照）。 | 低〜中 |
| 6 | **yt-dlp クライアント調整**（最も効く可能性／要A/B） | `options.py` の `player_client` で `android`/`ios`/`tv` を優先し、Web系の**署名解決（Deno起動＋`remote_components:['ejs:github']` のリモート取得）を回避**。Deno起動は1〜3s級になり得る。**ただし年齢制限/ログイン必須動画で再生失敗が増える可能性**→必ず実機で再生可否をA/B。`js_runtimes`/`remote_components`/`allow_remote_strings` の要否も再評価。 | 中 |

winloop 等の高速化ライブラリ追加もユーザー許可済み（現状 winloop は適用済み）。

### STEP 3: 後始末
- 最適化確定後、`[PERF]` 計測コードは削除可（`PERF_LOG=False` で残す手も）。`logger.perf` / `PERF_LOG`、各所の `time.perf_counter()` 計測、`play_music` の `t_request` 引数、track の `t_request` キーが対象。
- 変更に合わせ **SPEC.md / CLAUDE.md を更新**。
- 二重抽出を1発化したら SPEC §5「二段速度の解決」と §3 コマンド表の記述も直す。

---

## 作業上の必須ルール（クリーン環境で忘れがちな点）
- **インデントはタブ**。スペース禁止（全ファイル）。Edit が「String not found」になったら、深いネストのタブ数を `sed -n 'N,Mp' file | cat -A` で実バイト確認してから合わせる（今回 3〜4タブの取り違えで何度か失敗した）。
- **日本語**: コメント・Embed・ユーザー向け文字列はすべて日本語で。
- テスト/Lint/ビルドは無い。検証は `python -m py_compile main.py module/*.py` と venv での `import main`（`.venv/Scripts/python.exe -c "import main"`）。実 import は discord/yt_dlp 等の依存が要る。
- ProcessPool は Windows spawn。`Date.now`/乱数ではなく `time.perf_counter()` を計測に使用済み。
- 検証で Discord 接続を伴う実動作（実再生・終了時のプロセス回収）は未確認。最終的にユーザーの実機起動が要る。

## 関連ファイル早見
- 中核ロジック: `module/music.py`（GuildMusicPlayer / `_prefetch_worker` / `play_next_song` / `play_music` / `fetch_track_info` / `YTDLSource`）
- コマンド & ライフサイクル: `main.py`（`bot_play`=`/p`、`SatouSioBot.close`）
- yt-dlp/FFmpeg設定: `module/options.py`
- 計測ヘルパ: `module/logger.py`（`perf` / `PERF_LOG`）
