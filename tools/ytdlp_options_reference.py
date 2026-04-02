import yt_dlp.options

# 表示用テキストの定義
# 外部管理を想定したメッセージ定義
UI_MESSAGES = {
	'header_cli': 'CLI Option',
	'header_api': 'API Key (ydl_opts)',
	'header_desc': 'Description',
	'separator': '-' * 100,
}


def get_yt_dlp_options_list():
	"""
	yt_dlpのパーサーからオプション情報を抽出し、リスト形式で返す。

	Returns:
		list[dict]: オプション情報の辞書（cli, api_key, description）のリスト
	"""
	parser = yt_dlp.options.create_parser()
	options_info = []

	# オプショングループごとに解析
	for group in parser.option_groups:
		for option in group.option_list:
			# CLIオプション文字列の整形（-f, --formatなど）
			cli_strings = ', '.join(filter(None, option._short_opts + option._long_opts))

			# APIで利用されるキー名は通常dest属性に格納されている
			api_key = option.dest if option.dest else 'N/A'

			# ヘルプテキストの取得
			description = option.help if option.help else ''

			options_info.append({
				'cli': cli_strings,
				'api_key': api_key,
				'description': description
			})

	return options_info


def print_options_table(options):
	"""
	抽出されたオプション情報をコンソールにテーブル形式で出力する。
	"""
	# カラム幅の設定
	col_width_cli = 30
	col_width_api = 25

	# ヘッダー出力
	header = f"{UI_MESSAGES['header_cli']:<{col_width_cli}} | {UI_MESSAGES['header_api']:<{col_width_api}} | {UI_MESSAGES['header_desc']}"
	print(header)
	print(UI_MESSAGES['separator'])

	for opt in options:
		cli = opt['cli']
		api = opt['api_key']
		desc = opt['description']

		# 説明文が長い場合の簡易的なトリミング（必要に応じて調整）
		clean_desc = desc.replace('\n', ' ').strip()

		print(f"{cli:<{col_width_cli}} | {api:<{col_width_api}} | {clean_desc}")


if __name__ == '__main__':
	# オプションデータの取得と表示
	all_options = get_yt_dlp_options_list()
	print_options_table(all_options)