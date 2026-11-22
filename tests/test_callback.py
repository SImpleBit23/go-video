"""Static VK callback relay in an isolated browser, every request intercepted."""
from pathlib import Path
from urllib.parse import urlencode, parse_qs
import json
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
html = (root / 'docs/oauth_callback.html').read_text(encoding='utf8')
results = []
with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe', headless=True)
    for mismatch in (False, True):
        context = browser.new_context()
        page = context.new_page()
        posted = []
        callback = 'http://localhost/callback'
        state = 'fixture-state-123456'
        authorize = 'https://id.vk.com/authorize?' + urlencode(dict(client_id='12345', redirect_uri=callback, state=state, response_type='code'))
        def handle(route):
            u = route.request.url
            if u.startswith(callback):
                route.fulfill(status=200, content_type='text/html', body=html)
            elif u.startswith('https://id.vk.com/authorize?'):
                returned = callback + '?' + urlencode(dict(state='wrong' if mismatch else state, code='fixture-code', device_id='fixture-device'))
                route.fulfill(status=200, content_type='text/html', body='<script>location.replace('+json.dumps(returned)+')</script>')
            elif u == 'http://192.168.0.1:8472/api/oauth-return':
                posted.append(parse_qs(route.request.post_data))
                route.fulfill(status=200, content_type='text/html', body='<h1>Fixture MGU received code</h1>')
            else:
                route.abort()
        context.route('**/*', handle)
        start = dict(remote='http://192.168.0.1:8472/#'+'a'*32, authorize=authorize)
        from urllib.parse import quote
        page.goto(callback + '#start=' + quote(json.dumps(start), safe=''))
        if mismatch:
            page.wait_for_function("document.querySelector('#message') && document.querySelector('#message').className==='error'")
            assert not posted
        else:
            page.wait_for_url('http://192.168.0.1:8472/api/oauth-return')
            assert posted[0]['token'] == ['a'*32]
            assert 'code=fixture-code' in posted[0]['callback'][0]
            assert 'access_token' not in str(posted) and 'code_verifier' not in str(posted)
        results.append(dict(state_mismatch=mismatch, posts=len(posted), all_requests_intercepted=True, live_vk_login=False))
        context.close()
    browser.close()
print(json.dumps(results))
