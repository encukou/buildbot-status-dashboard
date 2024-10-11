from urllib.parse import urlunsplit, urlencode

from flask import redirect
import requests
import requests_cache

import release_dashboard

requests_cache.install_cache(backend='sqlite', stale_while_revalidate=True)
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

@app.route('/')
def index():
    return redirect("/index.html")
