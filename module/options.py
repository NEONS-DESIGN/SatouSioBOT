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
    'no_warnings': False,
    'default_search': 'auto',
    #'force-ipv6': True,
    'source_address': '0.0.0.0',
    'extractor_args': {
        'youtube': {
            'player_client': ['web_music'],
        }
    },
    'downloader': 'aria2c',
    'external-downloader': {'http': 'aria2c','ftp': 'aria2c','m3u8': 'aria2c','dash': 'aria2c','rstp': 'aria2c','rtmp': 'aria2c','mms': 'aria2c',},
}

FFMPEG_OPTIONS = {"options": "-threads 16 -vn"}