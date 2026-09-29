#!/usr/bin/env python3
"""Notify one fork issue when the public workers-rs PR 1080 snapshot changes."""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request


UPSTREAM = "/repos/cloudflare/workers-rs"
FORK = "/repos/omariomari2/workers-rs"
TARGET = "cloudflare/workers-rs#1080"
PR_URL = "https://github.com/cloudflare/workers-rs/pull/1080"
CATEGORIES = {
    "PR details": PR_URL,
    "comments": PR_URL + "#discussion_bucket",
    "reviews": PR_URL + "/files",
    "checks": PR_URL + "/checks",
}
MARKER = "<!-- pr-notify-state:"
MAX_PAGES = 100
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class MonitorError(Exception):
    """A sanitized error that is safe to print in Actions logs."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token=None):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirects())

    def request(self, path, data=None):
        # Never follow response-provided URLs, including pagination or redirects.
        if not re.fullmatch(
            r"/repos/(cloudflare/workers-rs|omariomari2/workers-rs)/[A-Za-z0-9_/?=&.-]+",
            path,
        ) or ".." in path:
            raise MonitorError("Refusing an unexpected GitHub API path")
        if data is not None and (
            not self.token or not re.fullmatch(FORK + r"/issues/[1-9][0-9]*/comments", path)
        ):
            raise MonitorError("Refusing an unauthenticated or unexpected write")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "workers-rs-pr-1080-monitor",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        payload = None if data is None else json.dumps(data).encode("utf-8")
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request("https://api.github.com" + path,
                                         data=payload, headers=headers)
        try:
            # A failed/uncertain POST is never retried. The next run reads its marker.
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise MonitorError("GitHub API response exceeds the size limit")
            return json.loads(raw)
        except urllib.error.HTTPError as error:
            raise MonitorError(f"GitHub API returned HTTP {error.code}; no retry") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise MonitorError("GitHub API request failed; no retry") from None

    def paginate(self, path, key=None):
        items = []
        separator = "&" if "?" in path else "?"
        for page in range(1, MAX_PAGES + 1):
            value = self.request(f"{path}{separator}per_page=100&page={page}")
            batch = value[key] if key is not None else value
            if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
                raise MonitorError("Unexpected GitHub API list response")
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise MonitorError("GitHub pagination limit exceeded; snapshot not saved")


def digest(value):
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ordered(items, fields):
    return sorted(({field: item.get(field) for field in fields} for item in items),
                  key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))


def snapshot(api):
    pr = api.request(UPSTREAM + "/pulls/1080")
    head = pr["head"]["sha"]
    merge = pr.get("merge_commit_sha")
    for sha in (head, merge):
        if sha is not None and not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise MonitorError("Unexpected commit SHA from GitHub")
    if head is None or pr["number"] != 1080 or pr["state"] not in ("open", "closed"):
        raise MonitorError("Unexpected pull request response")
    if merge is None and pr["state"] == "open" and pr.get("mergeable") is None:
        raise MonitorError("GitHub is recalculating the merge commit; snapshot not saved")
    details = {field: pr.get(field) for field in
               ("title", "body", "state", "draft", "merged", "locked")}
    details.update({
        "head": head, "base": pr["base"]["ref"],
        "merge_commit": merge if pr["merged"] else None,
        "labels": sorted(label["name"] for label in pr["labels"]),
        "reviewers": sorted(user["login"] for user in pr["requested_reviewers"]),
        "teams": sorted(team["slug"] for team in pr["requested_teams"]),
    })
    comments = {
        "discussion": ordered(api.paginate(UPSTREAM + "/issues/1080/comments"),
                              ("id", "body", "updated_at")),
        "inline": ordered(api.paginate(UPSTREAM + "/pulls/1080/comments"),
                          ("id", "body", "updated_at")),
    }
    reviews = ordered(
        [review for review in api.paginate(UPSTREAM + "/pulls/1080/reviews")
         if review.get("state") != "PENDING"],
        ("id", "body", "state", "commit_id", "submitted_at"),
    )
    checks = {}
    for role, sha in (("head", head), ("merge", merge)):
        if sha is None or (role == "merge" and sha == head):
            continue
        # A transient 404 for the synthetic merge commit aborts this run, preserving
        # the complete previous snapshot. The next scheduled run can try again.
        runs = api.paginate(UPSTREAM + f"/commits/{sha}/check-runs?filter=latest", "check_runs")
        statuses = api.paginate(UPSTREAM + f"/commits/{sha}/statuses")
        latest = {}
        for status in statuses:
            context = status["context"]
            if context not in latest or status["id"] > latest[context]["id"]:
                latest[context] = status
        # Recomputing an empty synthetic merge commit is not a CI change.
        if role == "head" or runs or latest:
            checks[role] = {
                "runs": ordered(runs, ("id", "name", "status", "conclusion", "started_at", "completed_at")),
                "statuses": ordered(latest.values(), ("id", "context", "state")),
            }
    return {"PR details": details, "comments": comments, "reviews": reviews, "checks": checks}


def read_state(comments):
    states = []
    for comment in comments:
        user = comment.get("user") or {}
        if user.get("login") != "github-actions[bot]" or user.get("type") != "Bot":
            continue
        body = comment.get("body") or ""
        if MARKER not in body:
            continue
        match = re.search(re.escape(MARKER) + r"(.*?) -->", body, re.DOTALL)
        try:
            if body.count(MARKER) != 1 or not match:
                raise ValueError
            state = json.loads(match.group(1))
            hashes = state["categories"]
            if (state["schema"] != 1 or state["target"] != TARGET
                    or set(hashes) != set(CATEGORIES)
                    or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                           for value in hashes.values())
                    or type(comment["id"]) is not int):
                raise ValueError
            states.append((comment["id"], hashes))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise MonitorError("Malformed trusted monitor state; refusing to notify") from None
    return max(states, key=lambda state: state[0])[1] if states else None


def summaries(values):
    details = values["PR details"]
    state = "merged" if details["merged"] else details["state"]
    review_states = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes requested",
                     "COMMENTED": "commented", "DISMISSED": "dismissed"}
    reviews = values["reviews"]
    review_counts = [f"{sum(review['state'] == key for review in reviews)} {label}"
                     for key, label in review_states.items()
                     if any(review["state"] == key for review in reviews)]
    other_reviews = sum(review["state"] not in review_states for review in reviews)
    if other_reviews:
        review_counts.append(f"{other_reviews} other")
    counts = dict.fromkeys(("success", "failure", "error", "pending", "neutral", "skipped",
                           "cancelled", "timed_out", "action_required", "stale", "other"), 0)
    for bucket in values["checks"].values():
        for check in bucket["runs"]:
            result = check["conclusion"] if check["status"] == "completed" else "pending"
            counts[result if result in counts else "other"] += 1
        for status in bucket["statuses"]:
            result = status["state"]
            counts[result if result in counts else "other"] += 1
    check_counts = ", ".join(f"{count} {key.replace('_', ' ')}" for key, count in counts.items() if count)
    comments = values["comments"]
    return {
        "PR details": f"PR {state}; head {details['head'][:8]}.",
        "comments": f"Comments: {len(comments['discussion'])} discussion, {len(comments['inline'])} inline.",
        "reviews": "Review submissions: " + (", ".join(review_counts) or "0") + ".",
        "checks": "Checks: " + (check_counts or "0 reported") + ".",
    }


def notification(hashes, changed, baseline, summary):
    if baseline:
        visible = f"@omariomari2 Monitoring [{TARGET}]({PR_URL}). Initial baseline saved."
    else:
        links = ", ".join(f"[{category}]({CATEGORIES[category]})" for category in changed)
        visible = f"@omariomari2 [{TARGET}]({PR_URL}) changed: {links}."
    visible += "\n\n" + " ".join(summary[category] for category in CATEGORIES
                                  if category == "PR details" or category in changed)
    state = {"schema": 1, "target": TARGET, "categories": hashes}
    return visible + "\n\n" + MARKER + json.dumps(state, sort_keys=True, separators=(",", ":")) + " -->"


def run(api, tracking_issue):
    if not re.fullmatch(r"[1-9][0-9]*", str(tracking_issue)):
        raise MonitorError("TRACKING_ISSUE must be a positive integer")
    path = FORK + f"/issues/{tracking_issue}"
    issue = api.request(path)
    if "pull_request" in issue or issue.get("state") not in ("open", "closed"):
        raise MonitorError("Tracking target is not an issue")
    if issue["state"] == "closed":
        return "paused"
    previous = read_state(api.paginate(path + "/comments"))
    values = snapshot(api)
    current = {category: digest(value) for category, value in values.items()}
    changed = [category for category in CATEGORIES
               if previous is None or current[category] != previous[category]]
    if not changed:
        return "unchanged"
    # One comment atomically delivers the notification and records its snapshot.
    api.request(path + "/comments", {"body": notification(current, changed, previous is None, summaries(values))})
    return "initialized" if previous is None else "notified"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Read public upstream without a token or writes")
    args = parser.parse_args()
    try:
        if args.dry_run:
            values = snapshot(GitHub())
            summary = summaries(values)
            print(json.dumps({"target": TARGET, "dry_run": True,
                              "categories": {key: {"sha256": digest(value), "summary": summary[key]}
                                             for key, value in values.items()}}, indent=2))
        else:
            token = os.environ.get("GH_TOKEN")
            if not token:
                raise MonitorError("GH_TOKEN is required")
            print(run(GitHub(token), os.environ.get("TRACKING_ISSUE", "")))
        return 0
    except MonitorError as error:
        print(f"Monitor stopped: {error}", file=sys.stderr)
    except (KeyError, TypeError, ValueError, AttributeError):
        print("Monitor stopped: unexpected API response or stored state", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
