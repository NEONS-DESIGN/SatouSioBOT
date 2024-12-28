YTDLP_OPTIONS = {
    'cookiesfrombrowser': ('firefox', ),
    'format': 'bestaudio',
    'writethumbnail': True,
    'extractaudio': True,
    'outtmpl': 'temp/%(extractor)s-%(id)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': False,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    "extractor_args": {
        "youtube": {
            "player_client": ["web_music"],
        }
    },
}

FFMPEG_OPTIONS = {"options": "-vn"}