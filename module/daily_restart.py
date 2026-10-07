"""
毎日決まった時刻を過ぎてから、Bot が使われていないときに再起動するための待ち合わせ。
Bot は終了コード RESTART_EXIT_CODE で終了し、start.ps1 がそれを見て同じウィンドウで起動し直す。
"""

import asyncio
import datetime
import os
from collections.abc import Callable

from module.logger import get_bot_logger

logger = get_bot_logger()

# start.ps1 がこの環境変数を "1" にして起動したときだけ、終了後に起動し直してもらえる
SUPERVISOR_ENV = "SATOUSIOBOT_SUPERVISED"
# 再起動を求めて終了するときの終了コード (start.ps1 の $RestartExitCode と同じ値にする)
RESTART_EXIT_CODE = 75
# 再起動の時刻まで眠る 1 回あたりの最大秒数、および時刻を過ぎてから使われていないかを確かめる間隔
CHECK_INTERVAL = 60.0


def is_supervised() -> bool:
	"""start.ps1 から起動され、終了後に起動し直してもらえる状態か"""
	return os.environ.get(SUPERVISOR_ENV) == "1"


def next_restart_at(now: datetime.datetime, at: datetime.time) -> datetime.datetime:
	"""now より後で最初に時刻 at になる日時を返す (起動直後に再起動しないよう、now と同時刻なら翌日)"""
	candidate = datetime.datetime.combine(now.date(), at)
	if candidate <= now:
		candidate += datetime.timedelta(days=1)
	return candidate


async def wait_for_restart(at: datetime.time, in_use: Callable[[], bool]) -> None:
	"""
	次の時刻 at を過ぎるまで待ち、その後 in_use() が False になるまで CHECK_INTERVAL 秒ごとに確かめて待つ。
	- 長い sleep 1 回にせず小分けにするのは、PC のスリープや時刻合わせで実際の時刻とずれないようにするため
	"""
	target = next_restart_at(datetime.datetime.now(), at)
	logger.info(f"{target:%Y-%m-%d %H:%M} 以降、使われていないときに定期再起動します。")
	while (remaining := (target - datetime.datetime.now()).total_seconds()) > 0:
		await asyncio.sleep(min(remaining, CHECK_INTERVAL))
	if in_use():
		logger.info("定期再起動の時刻を過ぎましたが、使用中のため終わるまで待ちます。")
		while in_use():
			await asyncio.sleep(CHECK_INTERVAL)
