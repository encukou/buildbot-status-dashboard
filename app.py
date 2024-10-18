from urllib.parse import urlunsplit, urlencode

from flask import redirect, url_for
import requests
import requests_cache

import release_dashboard

requests_cache.install_cache(
    backend='sqlite',
    stale_while_revalidate=True,
    expire_after=15*60,
)
app = release_dashboard.get_release_status_app()

class BuildbotAPIShim:
    def dataGet(self, parts, limit=None, order=None, filters=None):
        if isinstance(parts, str):
            parts = [parts.lstrip('/')]
        query = {}
        if limit is not None:
            query['limit'] = limit
        if order is not None:
            for o in order:
                query['order'] = o
        if filters is not None:
            for f in filters:
                for value in f.values:
                    query[f'{f.field}__{f.op}'] = value
        url = urlunsplit((
            'https',
            'buildbot.python.org',
            '/api/v2/' + '/'.join(str(p) for p in parts),
            urlencode(query),
            '',
        ))
        print('GET', url, '...')
        response = requests.get(url)
        print('Got', response)
        response.raise_for_status()
        data = response.json()
        print('meta:', data.pop('meta'))
        [result] = data.values()
        return result

app.buildbot_api = BuildbotAPIShim()


def body_middleware(app):
    wsgi_app = app.wsgi_app  # the wrapped wsgi_app
    def _mw(environ, server_start_response):
        if environ['PATH_INFO'] not in ('/', '/index.html'):
            return (yield from wsgi_app(environ, server_start_response))
        saved_status = None
        saved_headers = ()
        def start_response(status, headers):
            nonlocal saved_status, saved_headers
            saved_status = status
            saved_headers = headers
            return server_start_response(status, headers)
        response = wsgi_app(environ, start_response)
        if saved_status.startswith('200'):
            with app.request_context(environ):
                yield b'<html>'
                yield b'<head>'
                yield b'<base href="https://buildbot.python.org/">'
                yield b'<link rel="stylesheet" href="https://buildbot.python.org/assets/index-RMiJqufA.css">'  # TODO: this'll break...
                url = url_for('static', filename='dashboard.css', _external=True)
                yield f'<link rel="stylesheet" href="{url}">'.encode()
                yield b'</head>'
                yield b'<body>'
        yield from response
        if saved_status.startswith('200'):
            yield b'</body>'
            yield b'</html>'
    return _mw

app.wsgi_app = body_middleware(app)
