import json
import time
import unicodedata
from typing import NamedTuple

from module.options import app_config
from module.sqlite import sql_execute_batch, sql_execution

# 1 日の秒数 (保存期間の設定は日数で持つ)
_DAY_SECONDS = 86400

class CachedPlaylist(NamedTuple):
	"""保存した再生リストの結果"""
	# 抽出結果と同じ形の辞書 (title, webpage_url, thumbnail, entries)
	info: dict
	# 保存してからの経過秒数
	age: float

def normalize_query(query: str) -> str:
	"""検索語の表記ゆれ (全角・半角、大文字・小文字、空白の数) を揃えたキーを返す"""
	return " ".join(unicodedata.normalize("NFKC", query).casefold().split())

def entry_url(entry: dict) -> str | None:
	"""抽出結果のエントリから曲の URL を返す。(track dict の url と同じ決め方) 無ければ None"""
	return entry.get("webpage_url") or entry.get("original_url") or entry.get("url")

def _track_ttl() -> float:
	"""曲のメタ情報の保存期間(秒)"""
	return app_config.TRACK_CACHE_DAYS * _DAY_SECONDS

def _search_ttl() -> float:
	"""曲名検索の結果の保存期間(秒)。曲のメタ情報より長くは使えない"""
	return min(app_config.SEARCH_CACHE_DAYS, app_config.TRACK_CACHE_DAYS) * _DAY_SECONDS

def _track_upsert(entry: dict, now: int) -> tuple[str, tuple] | None:
	"""曲のメタ情報を保存 (取得し直した場合は上書き) する SQL を返す。URL が無いエントリは None"""
	url = entry_url(entry)
	if not url:
		return None
	return (
		"INSERT OR REPLACE INTO track_meta (url, title, duration, thumbnail, fetched_at) VALUES (?, ?, ?, ?, ?);",
		(url, entry.get("title"), entry.get("duration"), entry.get("thumbnail"), now),
	)

def _row_to_entry(row: tuple) -> dict:
	"""track_meta の (url, title, duration, thumbnail) を抽出結果のエントリと同じ形にする"""
	url, title, duration, thumbnail = row
	return {"url": url, "title": title, "duration": duration, "thumbnail": thumbnail}

async def get_search(query: str) -> dict | None:
	"""
	保存した曲名検索の結果 (エントリ) を返す。query は normalize_query 済みのキー。
	保存が無い・期限切れ・曲のメタ情報が期限切れ・DB の読み込みに失敗した場合は None
	"""
	if _search_ttl() <= 0:
		return None
	now = time.time()
	try:
		rows = await sql_execution(
			"SELECT t.url, t.title, t.duration, t.thumbnail FROM search_cache s JOIN track_meta t ON t.url = s.track_url "
			"WHERE s.query = ? AND s.fetched_at > ? AND t.fetched_at > ?;",
			(query, now - _search_ttl(), now - _track_ttl()),
		)
	except Exception:
		# 失敗は sql_execution が記録済み。検索し直せば再生できるため、保存なしとして扱う
		return None
	return _row_to_entry(rows[0]) if rows else None

async def save_search(query: str, entry: dict) -> None:
	"""曲名検索の結果を保存する。query は normalize_query 済みのキー。失敗しても再生には影響しないため例外を出さない"""
	if _search_ttl() <= 0:
		return
	now = int(time.time())
	if (track := _track_upsert(entry, now)) is None:
		return
	try:
		await sql_execute_batch((
			track,
			("INSERT OR REPLACE INTO search_cache (query, track_url, fetched_at) VALUES (?, ?, ?);", (query, track[1][0], now)),
		))
	except Exception:
		pass

async def forget_search(query: str) -> None:
	"""保存した曲名検索の結果を消す。(その曲を取得できなかったとき) 失敗しても例外を出さない"""
	try:
		await sql_execution("DELETE FROM search_cache WHERE query = ?;", (query,))
	except Exception:
		pass

async def get_playlist(url: str) -> CachedPlaylist | None:
	"""
	保存した再生リストの結果を返す。保存が無い・期限切れ・曲のメタ情報が 1 曲でも期限切れ・DB の読み込みに失敗した場合は None
	"""
	if _track_ttl() <= 0:
		return None
	now = time.time()
	try:
		rows = await sql_execution(
			"SELECT title, webpage_url, thumbnail, track_urls, fetched_at FROM playlist_cache WHERE url = ? AND fetched_at > ?;",
			(url, now - _track_ttl()),
		)
		if not rows:
			return None
		title, webpage_url, thumbnail, track_urls_json, fetched_at = rows[0]
		track_urls: list[str] = json.loads(track_urls_json)
		tracks = await sql_execution(
			f"SELECT url, title, duration, thumbnail FROM track_meta WHERE fetched_at > ? AND url IN ({', '.join('?' * len(track_urls))});",
			(now - _track_ttl(), *track_urls),
		) if track_urls else []
	except Exception:
		# 読み込み失敗・壊れた JSON は保存なしとして扱う (取得し直せば上書きされる)
		return None
	entries_by_url = {row[0]: _row_to_entry(row) for row in tracks}
	if len(entries_by_url) != len(set(track_urls)):
		return None
	info = {
		"title": title,
		"webpage_url": webpage_url,
		"thumbnail": thumbnail,
		"entries": [entries_by_url[track_url] for track_url in track_urls],
	}
	return CachedPlaylist(info, now - fetched_at)

async def save_playlist(url: str, info: dict) -> None:
	"""再生リストの結果と、含まれる曲のメタ情報を保存する。曲が 1 曲も無い結果は保存しない。失敗しても例外を出さない"""
	if _track_ttl() <= 0 or not info.get("entries"):
		return
	now = int(time.time())
	statements: list[tuple[str, tuple]] = []
	track_urls: list[str] = []
	for entry in info["entries"]:
		if (track := _track_upsert(entry, now)) is not None:
			statements.append(track)
			track_urls.append(track[1][0])
	statements.append((
		"INSERT OR REPLACE INTO playlist_cache (url, title, webpage_url, thumbnail, track_urls, fetched_at) VALUES (?, ?, ?, ?, ?, ?);",
		(url, info.get("title"), info.get("webpage_url") or info.get("original_url"), info.get("thumbnail"), json.dumps(track_urls), now),
	))
	try:
		await sql_execute_batch(statements)
	except Exception:
		pass

async def purge_expired() -> int:
	"""期限切れの保存を消し、消した曲の件数を返す。(起動時に呼ぶ) 失敗したら例外を送出する"""
	now = time.time()
	track_limit = now - _track_ttl()
	await sql_execute_batch((
		("DELETE FROM search_cache WHERE fetched_at <= ?;", (now - _search_ttl(),)),
		("DELETE FROM playlist_cache WHERE fetched_at <= ?;", (track_limit,)),
	))
	deleted = await sql_execution("DELETE FROM track_meta WHERE fetched_at <= ? RETURNING url;", (track_limit,))
	return len(deleted)
