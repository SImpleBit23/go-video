#!/usr/bin/env python3
"""Go Video fetcher. Uses RideTube's MGYI writer and a local FFmpeg demuxer.

VK commonly exposes combined A/V streams, unlike YouTube's separate tracks.
Download one authorized stream, demux H.264 without decoding video, prepare AAC
for BMW MediaPlayer. All helpers run as the service's dedicated network uid.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ytfetch as base

VID = re.compile(r'-?[1-9][0-9]{0,19}_[1-9][0-9]{0,19}\Z')
CHILDREN = set()
FFMPEG = str(Path(__file__).with_name('ffmpeg'))
LIBC = None
if sys.platform.startswith('linux'):
    import ctypes
    LIBC = ctypes.CDLL(None, use_errno=True)


def stop(signum, frame):
    for child in list(CHILDREN):
        try:
            child.kill()
        except OSError:
            pass
    raise SystemExit(128 + signum)


def spawn(args, **kw):
    if LIBC is not None:
        parent = os.getpid()
        def parent_death_signal():
            # The daemon may SIGKILL this fetcher on Stop. FFmpeg must die too,
            # even when Python's own signal handler cannot run.
            if LIBC.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
                os._exit(127)
        kw['preexec_fn'] = parent_death_signal
    child = subprocess.Popen(args, **kw)
    CHILDREN.add(child)
    return child


def call(args):
    child = spawn(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, err = child.communicate()
    CHILDREN.discard(child)
    if child.returncode:
        # Avoid leaking expiring URLs or credentials through an ffmpeg error line.
        raise ValueError('Не удалось подготовить звук VK (FFmpeg exit %s)' % child.returncode)


def params(a, hook=None):
    return dict(base.ydl_params(a, None, hook),
                format='best[height<=%d][fps<=?30][vcodec!^=av01][vcodec!^=vp9]/best[height<=%d][fps<=?30]' % (a.height, a.height),
                socket_timeout=15, retries=2, fragment_retries=2,
                ffmpeg_location=FFMPEG, max_filesize=2 * 1024 ** 3)


def metadata(info, vid):
    if info.get('is_live') or info.get('has_drm'):
        raise ValueError('Прямой эфир или защищённое видео не поддерживается этим плеером')
    return dict(id=vid, title=info.get('title') or 'VK Видео',
                channel=info.get('uploader') or 'VK Видео',
                duration_s=info.get('duration') or 0,
                chapters=[[int(float(c.get('start_time') or 0) * 1000), str(c.get('title') or '')[:80]]
                          for c in (info.get('chapters') or [])[:100]])


def prepare_media(source, folder, meta, ffmpeg=FFMPEG):
    """Same files/events consumed by RideTube's unchanged playback state machine."""
    audio_part = str(folder / 'audio.m4a.part')
    audio_error = []
    def audio():
        try:
            call([ffmpeg, '-nostdin', '-v', 'error', '-i', str(source), '-map', '0:a:0',
                  '-vn', '-c:a', 'aac', '-b:a', '128k', '-threads', '1', '-movflags', '+faststart',
                  '-metadata', 'title=' + meta['title'], '-metadata', 'artist=' + meta['channel'],
                  '-f', 'ipod', '-y', audio_part])
            os.replace(audio_part, folder / 'audio.m4a')
            base.emit(ev='audio_ready', bytes=(folder / 'audio.m4a').stat().st_size)
        except Exception as e:
            audio_error.append(e)
    worker = threading.Thread(target=audio)
    worker.start()
    child = spawn([ffmpeg, '-nostdin', '-v', 'error', '-i', str(source), '-map', '0:v:0',
                   '-an', '-c:v', 'copy', '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
                   '-f', 'mp4', 'pipe:1'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        base.demux_video(child.stdout, str(folder), meta['duration_s'],
                         lambda ms, frames, size: base.emit(ev='video', ms=ms, frames=frames, bytes=size))
        child.stdout.close()
        if child.wait():
            raise ValueError('Не удалось подготовить H.264 для MGU')
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(); CHILDREN.discard(child)
        worker.join()
    if audio_error:
        raise audio_error[0]
    n = base.validate(str(folder / 'index.bin'), str(folder / 'video.h264'))
    if not n:
        raise ValueError('Пустой видеопоток VK')
    base.emit(ev='done')


def read_mp4_prefix(source):
    """Reject a tail-moov MP4 before spending time downloading it to a pipe."""
    prefix = bytearray()
    offset = 0
    while offset + 16 <= 1024 * 1024:
        while len(prefix) < offset + 16:
            chunk = source.read(min(65536, offset + 16 - len(prefix)))
            if not chunk:
                return None
            prefix.extend(chunk)
        size, kind = struct.unpack_from('>I4s', prefix, offset)
        if kind == b'moov':
            return bytes(prefix)
        if kind == b'mdat':
            return None
        if size == 1:
            size = struct.unpack_from('>Q', prefix, offset + 8)[0]
            if size < 16:
                return None
        if size < 8:
            return None
        offset += size
    return None


class StreamInterrupted(ValueError):
    pass


def prepare_stream(source, folder, meta, ffmpeg=FFMPEG, prefix=b''):
    """Demux one sequential input into a growing video index and an AAC head."""
    audio_frag = folder / 'audio.stream.part'
    audio_part = folder / 'audio.m4a.part'
    head_part = folder / 'audio.head.m4a.part'
    tags = base.tags_udta(meta['title'], meta['channel'], None)
    child = spawn([ffmpeg, '-nostdin', '-v', 'error', '-i', 'pipe:0',
                   '-map', '0:a:0', '-c:a', 'aac', '-b:a', '128k', '-threads', '1',
                   '-movflags', '+empty_moov+default_base_moof', '-frag_duration', '1000000',
                   '-flush_packets', '1', '-f', 'mp4', '-y', str(audio_frag),
                   '-map', '0:v:0', '-c:v', 'copy',
                   '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
                   '-flush_packets', '1', '-f', 'mp4', 'pipe:1'],
                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    input_error = []
    head_sent = False
    next_check = 12000
    content_length = getattr(source, 'headers', {}).get('Content-Length')

    def feed():
        try:
            received = len(prefix)
            with child.stdin:
                child.stdin.write(prefix)
                while chunk := source.read(128 * 1024):
                    received += len(chunk)
                    if received > 2 * 1024 ** 3:
                        raise ValueError('VK media size limit')
                    child.stdin.write(chunk)
                if content_length and received != int(content_length):
                    raise ValueError('VK media transfer incomplete')
        except Exception as error:
            input_error.append(error)

    def progress(ms, frames, size):
        nonlocal head_sent, next_check
        if shutil.disk_usage(folder).free < base.MIN_FREE:
            raise ValueError('VK media storage limit')
        base.emit(ev='video', ms=ms, frames=frames, bytes=size)
        if head_sent or ms < next_check:
            return
        next_check = ms + 2000
        try:
            result = base.flatten_m4a(audio_frag, head_part, limit=audio_frag.stat().st_size, udta=tags)
        except (OSError, ValueError, struct.error):
            return
        if result and result[1] >= 10000:
            os.replace(head_part, folder / 'audio.head.m4a')
            head_sent = True
            base.emit(ev='audio_head', ms=result[1], bytes=result[0])

    worker = threading.Thread(target=feed)
    worker.start()
    try:
        base.demux_video(child.stdout, str(folder), meta['duration_s'], progress)
        if child.wait():
            raise ValueError('VK streaming demux failed')
        worker.join()
        if input_error:
            raise ValueError('VK streaming input incomplete')
        size, audio_ms = base.flatten_m4a(audio_frag, audio_part, udta=tags)
        if meta['duration_s'] and audio_ms + 3000 < meta['duration_s'] * 1000:
            raise ValueError('VK streaming audio incomplete')
        if not base.validate(str(folder / 'index.bin'), str(folder / 'video.h264')):
            raise ValueError('VK streaming video incomplete')
        os.replace(audio_part, folder / 'audio.m4a')
        base.emit(ev='audio_ready', bytes=size)
        base.emit(ev='done')
        return True
    except Exception:
        if head_sent:
            # The daemon has opened these files. Let it stop playback before replacing them.
            base.emit(ev='stream_fallback')
            raise StreamInterrupted('VK streaming interrupted')
        return False
    finally:
        if child.poll() is None:
            child.kill()
        child.stdout.close()
        child.wait()
        CHILDREN.discard(child)
        worker.join()
        for file in (audio_frag, audio_part, head_part):
            file.unlink(missing_ok=True)


def try_stream(ydl, url, folder, vid, height):
    from yt_dlp.networking import Request

    try:
        info = ydl.extract_info(url, download=False)
        meta = metadata(info, vid)
        formats = info.get('formats') or [info]
        fmt = next((f for f in reversed(formats)
                    if f.get('protocol') == 'https' and f.get('url', '').startswith('https://')
                    and 0 < (f.get('height') or 0) <= height and (f.get('fps') or 0) <= 30
                    and f.get('ext') == 'mp4' and not f.get('has_drm')
                    and (not f.get('vcodec') or f['vcodec'].startswith(('avc1', 'h264')))
                    and f.get('acodec') != 'none'), None)
        if not fmt:
            return False
        headers = dict(fmt.get('http_headers') or {})
        headers['Accept-Encoding'] = 'identity'
        with ydl.urlopen(Request(fmt['url'], headers=headers)) as source:
            encoding = source.headers.get('Content-Encoding', 'identity')
            if encoding.lower() != 'identity':
                return False
            prefix = read_mp4_prefix(source)
            if prefix is None:
                return False
            (folder / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf8')
            base.emit(ev='meta', title=meta['title'], duration_s=meta['duration_s'], chapters=meta['chapters'])
            return prepare_stream(source, folder, meta, prefix=prefix)
    except StreamInterrupted:
        raise
    except Exception:
        return False


def extract(a, download=False):
    if not a.id or not VID.fullmatch(a.id):
        raise ValueError('Некорректный идентификатор VK')
    base.install_dns(a.lease)
    yt_dlp = base.load_ytdlp(a.pyz, a.cache_dir)
    folder = Path(a.dir) if download else None
    p = params(a)
    if download:
        folder.mkdir(parents=True, exist_ok=True)
        p['outtmpl'] = {'default': str(folder / 'source.%(ext)s')}
        tick = base.Throttle()
        def hook(st):
            if shutil.disk_usage(folder).free < base.MIN_FREE:
                raise ValueError('Недостаточно свободного места на MGU')
            if st['status'] == 'downloading' and tick():
                total = st.get('total_bytes') or st.get('total_bytes_estimate') or 0
                base.emit(ev='audio', pct=min(90, int(90 * st.get('downloaded_bytes', 0) / total)) if total else 0)
        p['progress_hooks'] = [hook]
    with yt_dlp.YoutubeDL(p) as ydl:
        url = 'https://vkvideo.ru/video' + a.id
        if download and a.early:
            if try_stream(ydl, url, folder, a.id, a.height):
                return
            for name in ('video.h264', 'video.h264.part', 'index.bin'):
                (folder / name).unlink(missing_ok=True)
            base.emit(ev='stream_reset')
            print('VK: using full download', file=sys.stderr, flush=True)
        info = ydl.extract_info(url, download=download)
        meta = metadata(info, a.id)
        if not download:
            Path(a.dir).parent.mkdir(parents=True, exist_ok=True)
            temp = a.dir + '.tmp'
            Path(temp).write_text(json.dumps(ydl.sanitize_info(info)), encoding='utf8')
            os.replace(temp, a.dir)
            base.emit(ev='info', id=a.id, title=meta['title'], duration_s=meta['duration_s'])
            return
        (folder / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf8')
        base.emit(ev='meta', title=meta['title'], duration_s=meta['duration_s'], chapters=meta['chapters'])
        source = (info.get('requested_downloads') or [{}])[0].get('filepath') or ydl.prepare_filename(info)
        source = Path(source)
        if not source.is_file():
            raise ValueError('VK не вернул готовый медиафайл')
        try:
            prepare_media(source, folder, meta)
        finally:
            try:
                source.unlink()
            except OSError:
                pass


def check(a):
    ok_all=[]
    def emit(**value):
        ok_all.append(value.get('ok') is True);base.emit(**value)
    try:
        yt = base.load_ytdlp(a.pyz, a.cache_dir)
        from yt_dlp.version import __version__
        emit(ev='check', name='ytdlp', ok=True, info='loaded', version=__version__, s=0)
    except Exception:
        emit(ev='check', name='ytdlp', ok=False, info='failed', s=0)
    try:
        base.install_dns(a.lease)
        import socket
        socket.getaddrinfo('api.vk.com',443)
        emit(ev='check', name='dns', ok=True, info='resolved', s=0)
    except Exception:
        emit(ev='check', name='dns', ok=False, info='name not resolved', s=0)
    try:
        with urllib.request.urlopen('https://api.vk.com/method/users.get?v=5.199', context=base.tls_ctx(), timeout=4) as response:
            emit(ev='check', name='tls', ok=True, info='HTTP %d' % response.status, s=0)
    except Exception:
        emit(ev='check', name='tls', ok=False, info='failed', s=0)
    base.emit(ev='done', ok=all(ok_all))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['fetch','extract','serve','check','update','request','oauth','info','web'])
    ap.add_argument('id', nargs='?'); ap.add_argument('dir', nargs='?')
    ap.add_argument('--cache-dir', required=True); ap.add_argument('--height', type=int, default=720)
    ap.add_argument('--lease', default=base.LEASE); ap.add_argument('--node', default=base.NODE)
    ap.add_argument('--pyz'); ap.add_argument('--info'); ap.add_argument('--avc1', action='store_true')
    ap.add_argument('--early', action='store_true')
    ap.add_argument('--json', action='store_true'); ap.add_argument('--no-cert-time', action='store_true')
    ap.add_argument('--releases')
    a = ap.parse_args(argv)
    # Keep certificate verification enabled for every VK request, including token exchange.
    base.cert_time = True
    if a.cmd == 'web':
        base.install_dns(a.lease)
        from webguest import Guest
        request = json.loads(sys.stdin.read(4097))
        yt = base.load_ytdlp(a.pyz, a.cache_dir)
        print(json.dumps(Guest(yt, a.cache_dir).call(request.get('operation'), request.get('q',''))), flush=True)
        return
    if a.cmd == 'oauth':
        from urllib.parse import urlencode
        payload=json.loads(sys.stdin.read(16385))
        fields = payload.get('fields', {})
        credential_field = {'authorization_code': 'code', 'refresh_token': 'refresh_token'}[fields['grant_type']]
        credential = fields.pop(credential_field)
        request = urllib.request.Request(
            'https://id.vk.com/oauth2/auth?' + urlencode(fields),
            data=urlencode({credential_field: credential}).encode(),
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        base.install_dns(a.lease)
        with urllib.request.urlopen(request,context=base.tls_ctx(),timeout=12) as response:
            result=json.loads(response.read(65537))
        print(json.dumps(result),flush=True)
        return
    if a.cmd == 'info':
        if not a.id or not VID.fullmatch(a.id): raise ValueError('bad id')
        base.install_dns(a.lease)
        yt=base.load_ytdlp(a.pyz,a.cache_dir)
        with yt.YoutubeDL(params(a)) as ydl:
            value=ydl.extract_info('https://vkvideo.ru/video'+a.id,download=False)
        print(json.dumps({'thumbnail':value.get('thumbnail')}),flush=True)
        return
    if a.cmd == 'update': return base.update(a)
    if a.cmd == 'request':
        base.install_dns(a.lease); return base.request_cmd()
    if a.cmd == 'check': return check(a)
    if a.cmd == 'serve':
        base.emit(ev='warm')
        job = json.loads(sys.stdin.readline())
        if not isinstance(job,list) or not job or job[0] not in ('fetch','extract'): raise ValueError('bad job')
        return main(job)
    if a.height not in (360,480,720) or not a.dir: raise ValueError('Некорректные параметры воспроизведения')
    return extract(a, a.cmd == 'fetch')


if __name__ == '__main__':
    for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, stop)
    try:
        main()
    except Exception as error:
        # yt-dlp errors can contain signed source URLs; only a bounded, URL-redacted summary leaves the helper.
        message = re.sub(r'https?://\S+', '[url]', str(error))[:500]
        base.emit(ev='error', msg=message or 'Ошибка получения видео VK')
        sys.exit(1)
