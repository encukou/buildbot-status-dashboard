import datetime
import os
import time

from flask import Flask
from flask import render_template, request
import humanize

from buildbot.data.resultspec import Filter

N_BUILDS = 200

FAILED_BUILD_STATUS = 2
SUCCESS_BUILD_STATUS = 0
MIN_CONSECUTIVE_FAILURES = 2

# Cache result for 6 minutes. Generating the page is slow and a Python build
# takes at least 5 minutes, a common build takes 10 to 30 minutes.  There is a
# cronjob that forces a refresh every 5 minutes, so all human requests should
# get a cache hit.
CACHE_DURATION = 6 * 60


def get_breaking_build(builds):
    failing_streak = 0
    first_failing_build = None
    for build in builds:
        if not build["complete"]:
            continue
        if build["results"] == FAILED_BUILD_STATUS:
            failing_streak += 1
            first_failing_build = build
            continue
        elif build["results"] == SUCCESS_BUILD_STATUS:
            if failing_streak >= MIN_CONSECUTIVE_FAILURES:
                return True, first_failing_build
            return False, None
        failing_streak = 0
    return bool(first_failing_build), None


def get_release_status_app(buildernames=None):
    release_status_app = Flask("test", root_path=os.path.dirname(__file__))
    if buildernames is not None:
        buildernames = set(buildernames)
    cache = None

    def get_release_status():
        connected_builderids = set()
        for worker in release_status_app.buildbot_api.dataGet("/workers"):
            if worker["connected_to"]:
                for cnf in worker["configured_on"]:
                    connected_builderids.add(cnf["builderid"])

        builders = release_status_app.buildbot_api.dataGet("/builders")

        failed_builds_by_branch_and_tier = {}
        disconnected_builders = set()

        for i, builder in enumerate(builders):
            print(f'{i}/{len(builders)}', builder)
            if buildernames is not None and builder["name"] not in buildernames:
                continue

            if "stable" not in builder["tags"]:
                continue

            if "PullRequest" in builder["tags"]:
                continue

            branch = 'no-branch'
            tier = 'no tier'
            for tag in builder["tags"]:
                if "3." in tag:
                    branch = tag
                if tag.startswith('tier-'):
                    tier = tag

            #if not branch:
            #    continue

            failed_builds_by_tier = failed_builds_by_branch_and_tier.setdefault(branch, {})

            if builder["builderid"] not in connected_builderids:
                disconnected_builders.add(builder["builderid"])
                failed_builds = failed_builds_by_tier.setdefault(tier, [])
                failed_builds.append((builder, None, []))
                continue

            endpoint = ("builders", builder["builderid"], "builds")
            builds = release_status_app.buildbot_api.dataGet(
                endpoint,
                limit=N_BUILDS,
                order=["-complete_at"],
                filters=[Filter("complete", "eq", ["True"])],
            )

            is_failing, breaking_build = get_breaking_build(builds)

            if breaking_build:
                build = breaking_build
                changes = release_status_app.buildbot_api.dataGet(
                    ("builds", build["buildid"], "changes"),
                )
                build["changes"] = changes

            builds_to_show = []
            countdown = 10
            for build in builds:
                builds_to_show.append(build)
                if build["results"] == SUCCESS_BUILD_STATUS:
                    countdown -= 1
                    if countdown <= 0:
                        break

            if not is_failing:
                continue

            failed_builds = failed_builds_by_tier.setdefault(tier, [])
            failed_builds.append((builder, breaking_build, builds_to_show))

        def tier_sort_key(item):
            tier, data = item
            if tier == 'no tier':
                return 'zzz'  # sort last
            return tier

        failed_builders = []
        for branch, failed_builds_by_tier in failed_builds_by_branch_and_tier.items():
            if 'tier-1' in failed_builds_by_tier or 'tier-2' in failed_builds_by_tier:
                status = 'bad'
            elif failed_builds_by_tier:
                status = 'concern'
            else:
                status = 'ok'
            failed_builders.append((
                branch,
                sorted(failed_builds_by_tier.items(), key=tier_sort_key),
                status,
            ))

        def branch_sort_key(item):
            branch, *_ = item
            minor = branch.split('.')[-1]
            try:
                return int(minor)
            except ValueError:
                return 99

        failed_builders.sort(reverse=True, key=branch_sort_key)

        generated_at = datetime.datetime.now(tz=datetime.timezone.utc)

        return render_template(
            "releasedashboard.html",
            failed_builders=failed_builders,
            generated_at=generated_at,
            disconnected_builders=disconnected_builders,
        )

    @release_status_app.route('/')
    @release_status_app.route("/index.html")
    def main():
        nonlocal cache

        force_refresh = request.args.get("refresh", "").lower() in {"1", "yes", "true"}

        if cache is not None and not force_refresh:
            result, deadline = cache
            if time.monotonic() <= deadline:
                return result

        result = get_release_status()
        deadline = time.monotonic() + CACHE_DURATION
        cache = (result, deadline)
        return result

    @release_status_app.template_filter('first_line')
    def first_line(text):
        return text.partition('\n')[0]

    @release_status_app.template_filter('committer_name')
    def committer_name(text):
        return text.partition(' <')[0]

    @release_status_app.template_filter('format_timestamp')
    def format_timestamp(number):
        dt = datetime.datetime.fromtimestamp(number)
        ago = humanize.naturaldelta(datetime.datetime.now() - dt)
        return f'{humanize.naturaldate(dt)}, {ago} ago'

    return release_status_app
