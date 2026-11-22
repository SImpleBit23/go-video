"""Anonymous VK Video web session. No browser, account, OAuth app or static user token."""
import json, os, re, time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

CLIENT = 52461373  # Public VK Video website identity, not a user application.
HOME = 'https://vkvideo.ru/'

class Guest:
    def __init__(self, yt, cache):
        from yt_dlp.extractor.vk import VKIE
        self.ie = VKIE(yt.YoutubeDL({'quiet': True, 'no_warnings': True, 'socket_timeout': 15, 'retries': 0}))
        self.file = Path(cache) / 'vk-guest.json'

    def session(self):
        try:
            value = json.loads(self.file.read_text(encoding='utf8'))
            if value['until'] > time.time() + 30 and isinstance(value['token'], str):
                return value
        except (OSError, ValueError, KeyError, TypeError):
            pass
        html = self.ie._download_webpage(HOME, 'guest')
        # Read the website's current public guest configuration instead of shipping its secret.
        scripts = re.findall(r'<script[^>]+src="(https://st\.vkvideo\.ru/[^"<>]+\.js)"', html)
        secret = version = None
        for url in reversed(scripts):
            script = self.ie._download_webpage(url, 'guest-config')
            match = re.search(r'\(\w+=0x%x,\w+="([A-Za-z0-9]+)"\)' % CLIENT, script)
            if match:
                secret = match[1]
                version_match = re.search(r'mG:"(5\.\d+)"', script)
                version = version_match[1] if version_match else '5.289'
                break
        if not secret:
            raise ValueError('VK изменил гостевой веб-клиент. Требуется обновление Go Video.')
        response = self.ie._download_json('https://login.vk.ru/?act=get_anonym_token', 'guest-login',
            data=urlencode(dict(client_id=CLIENT, client_secret=secret,
                scopes='audio_anonymous,video_anonymous,photos_anonymous,profile_anonymous',
                isApiOauthAnonymEnabled='false', version='1', app_id='6287487')).encode(),
            headers={'Content-Type': 'application/x-www-form-urlencoded', 'Origin': HOME.rstrip('/'), 'Referer': HOME})
        data = response.get('data', {})
        token = data.get('access_token')
        if not isinstance(token, str) or not token:
            raise ValueError('VK не выдал гостевую сессию. Повторите позже.')
        value = dict(token=token, until=time.time() + 600, version=version)
        self.file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.file.with_suffix('.tmp')
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf8') as f:
            json.dump(value, f)
        os.replace(temp, self.file)
        return value

    def call(self, operation, q=''):
        if operation not in ('catalog', 'search'):
            raise ValueError('Неизвестный запрос каталога')
        for attempt in range(2):
            s = self.session()
            method = 'catalog.getVideoSearchWeb2' if operation == 'search' else 'catalog.getVideo'
            fields = dict(q=str(q)[:200], screen_ref='search_video_service', input_method='preset_from_link') if operation == 'search' else dict(url=HOME, need_blocks='1')
            fields['access_token'] = s['token']
            data = self.ie._download_json('https://api.vkvideo.ru/method/' + method + '?' + urlencode(dict(v=s['version'], client_id=CLIENT)), 'guest-catalog',
                data=urlencode(fields).encode(), headers={'Content-Type': 'application/x-www-form-urlencoded', 'Origin': HOME.rstrip('/'), 'Referer': HOME})
            error = data.get('error', {})
            if error.get('error_code') in (5, 1117) and attempt == 0:
                self.file.unlink(missing_ok=True)
                continue
            if error or not isinstance(data.get('response'), dict):
                raise ValueError('VK отклонил запрос каталога (%s)' % error.get('error_code', 'ответ'))
            r = data['response']
            return dict(items=r.get('videos', []), groups=r.get('groups', []))
        raise ValueError('Гостевая сессия VK истекла')
