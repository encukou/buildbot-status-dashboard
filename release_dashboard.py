import datetime
import os
import time
from functools import cached_property, total_ordering
import enum
from dataclasses import dataclass
import itertools

from flask import Flask
from flask import render_template, request
import jinja2
import humanize

from buildbot.data.resultspec import Filter

N_BUILDS = 200

FAILED_BUILD_STATUS = 2
WARNING_BUILD_STATUS = 1
SUCCESS_BUILD_STATUS = 0
MIN_CONSECUTIVE_FAILURES = 2

# Cache result for 6 minutes. Generating the page is slow and a Python build
# takes at least 5 minutes, a common build takes 10 to 30 minutes.  There is a
# cronjob that forces a refresh every 5 minutes, so all human requests should
# get a cache hit.
CACHE_DURATION = 6 * 60


class BBObject:
    """Base wrapper fo a Buildbot object.

    Acts as a dict with the info we get from BuildBot API, but can also
    have extra attributes -- ones needed for analysis, or collections
    of related items.

    All retrieved information should be cached (using @cached_property).
    For a fresh view, discard all these objects and build them again.

    Objects are arranged in a tree: every one (except the root) has a parent.
    (Cross-tree references must go through the root.)

    Computing info on demand means the "for & if" logic in the template,
    doesn't need to be duplicated in Python code.

    N.B.: In Jinja, mapping keys and attributes are largely
    interchangeable. Shadow them wisely.
    """
    def __init__(self, parent, info):
        self._parent = parent
        self._root = parent._root
        self._info = info

    def __getitem__(self, key):
        return self._info[key]

    def dataGet(self, *args, **kwargs):
        # Buildbot sets `buildbot_api` as an attribute on the WSGI app,
        # a bit later than we'd like. Get to it dynamically.
        return self._root._app.buildbot_api.dataGet(*args, **kwargs)

    def __repr__(self):
        return f'<{type(self).__name__} at {id(self)}: {self._info}>'


class BBState(BBObject):
    """The root of our abstraction, a bit special.
    """
    def __init__(self, app):
        self._root = self
        self._app = app
        super().__init__(self, {})
        self._branches = {}
        self._tiers = {}

    @cached_property
    def builders(self):
        active_builderids = set()
        for worker in self.workers:
            for cnf in worker["configured_on"]:
                active_builderids.add(cnf["builderid"])
        return [
            Builder(self, info)
            for info in self.dataGet("/builders")
            if info["builderid"] in active_builderids
        ]

    @cached_property
    def workers(self):
        return [Worker(self, info) for info in self.dataGet("/workers")]

    def get_branch(self, tags):
        for tag in tags:
            if tag.startswith("3."):
                break
        else:
            tag = 'no-branch'
            sort_key = (0, 0)
        try:
            return self._branches[tag]
        except KeyError:
            branch = Branch(self, {'name': tag})
            self._branches[tag] = branch
            return branch

    def get_tier(self, tags):
        for tag in tags:
            if tag.startswith("tier-"):
                break
        else:
            tag = 'no-tier'
        try:
            return self._tiers[tag]
        except KeyError:
            tier = Tier(self, {'name': tag})
            self._tiers[tag] = tier
            return tier

    @cached_property
    def now(self):
        return datetime.datetime.now(tz=datetime.timezone.utc)

    @cached_property
    def branches(self):
        # Make sure all branches are filled in
        [b.branch for b in self.builders]
        return sorted(self._branches.values(), reverse=True)


def cached_sorted_property(func=None, /, **sort_kwargs):
    """Like cached_property, but calls sorted() on the value

    This is sometimes used just to turn a generator into a list, as the
    Jinja template generally likes to know if sequences are empty.
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            return sorted(func(*args, **kwargs), **sort_kwargs)
        return cached_property(wrapper)
    if func:
        return decorator(func)
    return decorator


@total_ordering
class Builder(BBObject):
    @cached_property
    def builds(self):
        endpoint = ("builders", self["builderid"], "builds")
        infos = self.dataGet(
            endpoint,
            limit=N_BUILDS,
            order=["-complete_at"],
            filters=[Filter("complete", "eq", ["True"])],
        )
        builds = []
        for info in infos:
            builds.append(Build(self, info))
        return [Build(self, info) for info in infos]

    @cached_property
    def branch(self):
        return self._root.get_branch(self["tags"])
        return 'no-branch'

    @cached_property
    def tier(self):
        return self._root.get_tier(self["tags"])

    @cached_property
    def is_stable(self):
        return 'stable' in self["tags"]

    @cached_property
    def is_release_blocking(self):
        return self.tier.value in (1, 2)

    def __lt__(self, other):
        return self["name"] < other["name"]

    def iter_interesting_builds(self):
        """Yield builds except unfinished/skipped/interrupted ones"""
        for build in self.builds:
            if build["results"] in (
                SUCCESS_BUILD_STATUS,
                WARNING_BUILD_STATUS,
                FAILED_BUILD_STATUS,
            ):
                yield build

    @cached_sorted_property()
    def problems(self):
        latest_build = None
        for build in self.iter_interesting_builds():
            latest_build = build
            break

        if not latest_build:
            yield NoBuilds(self)
            return
        elif latest_build["results"] == WARNING_BUILD_STATUS:
            yield BuildWarning(latest_build)
        elif latest_build["results"] == FAILED_BUILD_STATUS:
            failing_streak = 0
            first_failing_build = None
            for build in self.iter_interesting_builds():
                if build["results"] == FAILED_BUILD_STATUS:
                    first_failing_build = build
                    continue
                elif build["results"] == SUCCESS_BUILD_STATUS:
                    if latest_build != first_failing_build:
                        yield BuildFailure(latest_build, first_failing_build)
                    break
            else:
                yield BuildFailure(latest_build)

        if not self.connected_workers:
            yield BuilderDisconnected(self)

    @cached_sorted_property
    def connected_workers(self):
        for worker in self._root.workers:
            if worker["connected_to"]:
                for cnf in worker["configured_on"]:
                    if cnf["builderid"] == self["builderid"]:
                        yield worker

class Worker(BBObject):
    pass

@total_ordering
class _BranchTierBase(BBObject):
    @cached_property
    def name(self):
        return self["name"]

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, other):
        if isinstance(other, str):
            return self.name == other
        return self.sort_key == other.sort_key

    def __lt__(self, other):
        return self.sort_key < other.sort_key

    def __str__(self):
        return self.name

@total_ordering
class Branch(_BranchTierBase):
    @cached_property
    def sort_key(self):
        if self.name.startswith("3."):
            try:
                return (1, int(self.name[2:]))
            except ValueError:
                return (2, 99)
        return (0, 0)

    @cached_property
    def title(self):
        if self.name == '3.x':
            return 'main'
        return self.name

    @cached_sorted_property()
    def problems(self):
        problems = []
        for builder in self._root.builders:
            if builder.branch == self:
                problems.extend(builder.problems)
        return problems

    @cached_property
    def featured_problem(self):
        try:
            return self.problems[0]
        except IndexError:
            return NoProblem()

    def get_grouped_problems(self):
        for d, problems in itertools.groupby(self.problems, lambda p: p.description):
            yield d, list(problems)


class Tier(_BranchTierBase):
    @cached_property
    def value(self):
        if self.name.startswith("tier-"):
            try:
                return int(self.name[5:])
            except ValueError:
                return 99
        return 99

    @cached_property
    def sort_key(self):
        return self.value

    @cached_property
    def is_release_blocking(self):
        return self.value in {1, 2}


class Build(BBObject):
    @cached_property
    def builder(self):
        assert self._parent["builderid"] == self["builderid"]
        return self._parent

    @cached_property
    def changes(self):
        infos = self.dataGet(
            ("builds", self["buildid"], "changes"),
        )
        return [Change(self, info) for info in infos]

    @cached_property
    def started_at(self):
        if self["started_at"]:
            return datetime.datetime.fromtimestamp(self["started_at"],
                                                   tz=datetime.timezone.utc)

    @cached_property
    def age(self):
        if self["started_at"]:
            return self._root.now - self.started_at

    @property
    def css_color_class(self):
        if self["results"] == SUCCESS_BUILD_STATUS:
            return 'success'
        if self["results"] == WARNING_BUILD_STATUS:
            return 'warning'
        if self["results"] == FAILED_BUILD_STATUS:
            return 'danger'
        return 'unknown'


class Change(BBObject):
    pass


def get_or_make(mapping, key):
    def decorator(func):
        try:
            return mapping[key]
        except KeyError:
            value = func()
            mapping[key] = value
            return value
    return decorator


class Severity(enum.IntEnum):
    NO_PROBLEM = enum.auto()
    no_builds_yet = enum.auto()
    disconnected_unstable_builder = enum.auto()
    unstable_builder_failure = enum.auto()

    TRIVIAL = enum.auto()
    build_warnings = enum.auto()
    disconnected_stable_builder = enum.auto()
    disconnected_blocking_builder = enum.auto()

    CONCERNING = enum.auto()
    nonblocking_failure = enum.auto()

    BLOCKING = enum.auto()
    release_blocking_failure = enum.auto()


class Problem:
    def __str__(self):
        return self.description

    def __eq__(self, other):
        return self.description == other.description

    def __lt__(self, other):
        return (-self.severity, self.description) < (-other.severity,
                                                     other.description)

    @property
    def css_color_class(self):
        if self.severity >= Severity.BLOCKING:
            return 'danger'
        if self.severity >= Severity.CONCERNING:
            return 'warning'
        return 'success'

    @cached_property
    def severity(self):
        self.severity, self.description = self.get_severity_and_description()
        return self.severity

    @cached_property
    def description(self):
        self.severity, self.description = self.get_severity_and_description()
        return self.description

    @property
    def affected_builds(self):
        return {}


@dataclass
class BuildFailure(Problem):
    """The most recent build failed"""
    latest_build: Build
    first_failing_build: 'Build | None' = None

    def get_severity_and_description(self):
        if not self.builder.is_stable:
            return Severity.unstable_builder_failure, "Unstable builder failed"
        if self.builder.is_release_blocking:
            severity = Severity.release_blocking_failure
        else:
            severity = Severity.nonblocking_failure
        description = f"{str(self.builder.tier).title()} stable builder failed"
        return severity, description

    @property
    def builder(self):
        return self.latest_build.builder

    @cached_property
    def affected_builds(self):
        result = {"Latest build": self.latest_build}
        if self.first_failing_build:
            result["Breaking build"] = self.first_failing_build
        return result


@dataclass
class BuildWarning(Problem):
    """The most recent build warns"""
    build: Build

    description = "Warnings"
    severity = Severity.build_warnings

    @property
    def builder(self):
        return self.build.builder

    @cached_property
    def affected_builds(self):
        return {"Warning build": self.build}


@dataclass
class NoBuilds(Problem):
    """Builder has no finished builds yet"""
    builder: Builder

    description = "Builder has no builds"
    severity = Severity.no_builds_yet


@dataclass
class BuilderDisconnected(Problem):
    """Builder has no finished builds yet"""
    builder: Builder

    def get_severity_and_description(self):
       try:
        if not self.builder.is_stable:
            severity = Severity.disconnected_unstable_builder
            description = "Disconnected unstable builder"
        else:
            description = f"Disconnected {str(self.builder.tier).title()} stable builder"
            if self.builder.is_release_blocking:
                severity = Severity.disconnected_blocking_builder
            else:
                severity = Severity.disconnected_stable_builder
        for build in self.builder.iter_interesting_builds():
            if build.age and build.age < datetime.timedelta(hours=6):
                description += ' (with recent build)'
                if severity >= Severity.BLOCKING:
                    severity = Severity.CONCERNING
                if severity >= Severity.CONCERNING:
                    severity = Severity.TRIVIAL
            break
        return severity, description
       except:
           raise SystemError


class NoProblem(Problem):
    """Dummy problem"""
    name = "Releasable"

    description = "No problem detected"
    severity = Severity.NO_PROBLEM


class ReleaseStatusApp:
    def __init__(self):
        self.flask_app = Flask("test", root_path=os.path.dirname(__file__))
        self.cache = None

        self.flask_app.jinja_env.add_extension('jinja2.ext.loopcontrols')
        self.flask_app.jinja_env.undefined = jinja2.StrictUndefined

        @self.flask_app.route('/')
        @self.flask_app.route("/index.html")
        def main():
            force_refresh = request.args.get("refresh", "").lower() in {"1", "yes", "true"}

            if self.cache is not None and not force_refresh:
                result, deadline = self.cache
                if time.monotonic() <= deadline:
                    return result

            result = self.get_release_status()
            deadline = time.monotonic() + CACHE_DURATION
            self.cache = (result, deadline)
            return result

        @self.flask_app.template_filter('first_line')
        def first_line(text):
            return text.partition('\n')[0]

        @self.flask_app.template_filter('committer_name')
        def committer_name(text):
            return text.partition(' <')[0]

        @self.flask_app.template_filter('format_datetime')
        def format_timestamp(dt):
            now = datetime.datetime.now(tz=datetime.timezone.utc)
            ago = humanize.naturaldelta(now - dt)
            return f'{dt:%Y-%m-%d %H:%M:%S}, {ago} ago'

    def dataGet(self, *args, **kwargs):
        # Buildbot sets `buildbot_api` as an attribute on the WSGI app.
        return self.flask_app.buildbot_api.dataGet(*args, **kwargs)


    def get_release_status(self):
        state = BBState(self.flask_app)

        connected_builderids = set()
        for worker in self.dataGet("/workers"):
            if worker["connected_to"]:
                for cnf in worker["configured_on"]:
                    connected_builderids.add(cnf["builderid"])

        builders = state.builders

        failed_builds_by_branch_and_tier = {}
        disconnected_builders = set()

        for builder in builders:
            if builder["builderid"] not in connected_builderids:
                disconnected_builders.add(builder["builderid"])

        generated_at = datetime.datetime.now(tz=datetime.timezone.utc)

        return render_template(
            "releasedashboard.html",
            state=state,
            Severity=Severity,
            generated_at=generated_at,
        )

def get_release_status_app(buildernames=None):
    return ReleaseStatusApp().flask_app
