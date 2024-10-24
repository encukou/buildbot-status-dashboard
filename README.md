A dashboard to maybe replace https://buildbot.python.org/#/wsgi/release_status

Some files should be copiable directly to the buildbot config:
- `release_dashboard.py`
- `templates/*`
- `static/*`

The rest is scaffolding and hacks to make this a stand-alone app.
The stand-alone app is meant for local development, does heavy caching, and
will be out-of-date and inconsistent.

A static preview is on GitHub Pages. It does *not* update automatically.

To show failed test cases, the dashboard needs access to JUnit XML result
files, which are only readable by the `buildbot` user (or root) on the
Buildbot machine.
If you have access, exfiltrate them using:

    HOSTNAME=...

    rsync -vrzi --ignore-existing --rsync-path="sudo -u buildbot rsync" \
        $HOSTNAME:/data/www/buildbot/test-results/ test-results



This is a fork of a part of https://github.com/python/buildmaster-config/ and
follows the same licence.
