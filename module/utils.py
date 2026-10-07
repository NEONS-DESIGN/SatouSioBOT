def format_duration(duration: float | None) -> str:
	"""秒数を "MM:SS" または "HH:MM:SS" 形式に変換する。0 / None は "00:00" """
	if not duration:
		return "00:00"
	h, rem = divmod(int(duration), 3600)
	m, s = divmod(rem, 60)
	return f"{h:02}:{m:02}:{s:02}" if h > 0 else f"{m:02}:{s:02}"
