"""Installed-helper publisher admission, independent of writable API evidence."""
from __future__ import annotations

from datetime import datetime
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

OFFICIAL_REPOSITORY = "kmishnev87/km-vms"
API_ROOT = "https://api.github.com/repos/" + OFFICIAL_REPOSITORY
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_METADATA_BYTES = 256 * 1024
MAX_TAG_HOPS = 4
LOOKUP_DEADLINE_SECONDS = 25


class PublishedSourceError(RuntimeError):
    def __init__(self, *, unavailable=False):
        self.unavailable = unavailable
        super().__init__("Official published source verification is unavailable" if unavailable
                         else "Source does not match an official published release")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _github_json(path, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PublishedSourceError(unavailable=True)
    request = urllib.request.Request(API_ROOT + path, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "km-vms-helper-publisher-check",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.build_opener(_NoRedirects()).open(request, timeout=min(8, remaining)) as response:
            chunks, size = [], 0
            while size <= MAX_METADATA_BYTES:
                if time.monotonic() >= deadline:
                    raise PublishedSourceError(unavailable=True)
                chunk = response.read1(min(16 * 1024, MAX_METADATA_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b"".join(chunks)
    except urllib.error.HTTPError as exc:
        raise PublishedSourceError(unavailable=exc.code in {403, 408, 429} or exc.code >= 500) from None
    except (OSError, TimeoutError, urllib.error.URLError):
        raise PublishedSourceError(unavailable=True) from None
    if len(raw) > MAX_METADATA_BYTES:
        raise PublishedSourceError()
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise PublishedSourceError() from None
    if not isinstance(result, dict):
        raise PublishedSourceError()
    return result


def verify_published_source(source, *, get_json=None):
    """Return pinned verified identity; never use caller-provided URLs or cache."""
    if not isinstance(source, dict):
        raise PublishedSourceError()
    tag = source.get("ref")
    commit = str(source.get("commit") or "").lower()
    if (str(source.get("repo") or "").casefold() != OFFICIAL_REPOSITORY
            or not isinstance(tag, str) or not TAG_RE.fullmatch(tag)
            or tag != "v" + str(source.get("version") or "")
            or not SHA_RE.fullmatch(commit)
            or str(source.get("apply_ref") or "").lower() != commit):
        raise PublishedSourceError()
    deadline = time.monotonic() + LOOKUP_DEADLINE_SECONDS
    fetch = get_json or _github_json
    quoted_tag = urllib.parse.quote(tag, safe="")
    release = fetch("/releases/tags/" + quoted_tag, deadline)
    if (release.get("draft") is not False or release.get("tag_name") != tag
            or not isinstance(release.get("published_at"), str)):
        raise PublishedSourceError()
    try:
        published_at = datetime.fromisoformat(release["published_at"].replace("Z", "+00:00"))
        if published_at.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise PublishedSourceError() from None
    reference = fetch("/git/ref/tags/" + quoted_tag, deadline)
    if reference.get("ref") != "refs/tags/" + tag:
        raise PublishedSourceError()
    obj = reference.get("object")
    for hop in range(MAX_TAG_HOPS + 1):
        if not isinstance(obj, dict):
            raise PublishedSourceError()
        sha = str(obj.get("sha") or "").lower()
        if not SHA_RE.fullmatch(sha):
            raise PublishedSourceError()
        if obj.get("type") == "commit":
            if sha != commit:
                raise PublishedSourceError()
            return {**source, "repo": OFFICIAL_REPOSITORY, "commit": sha, "apply_ref": sha}
        if obj.get("type") != "tag" or hop == MAX_TAG_HOPS:
            raise PublishedSourceError()
        obj = fetch("/git/tags/" + sha, deadline).get("object")
    raise PublishedSourceError()
