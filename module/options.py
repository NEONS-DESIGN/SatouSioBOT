YTDLP_OPTIONS = {
    'cookiefile': 'cookies.txt',
    'format': 'bestaudio',
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
}

FFMPEG_OPTIONS = {"options": "-vn -threads 16"}