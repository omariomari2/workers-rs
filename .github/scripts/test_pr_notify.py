import copy
import io
import json
import unittest
from unittest.mock import patch

import pr_notify as monitor


HEAD = "a" * 40
MERGE = "b" * 40
UPSTREAM = "/repos/cloudflare/workers-rs"
TRACKER = "/repos/omariomari2/workers-rs/issues/42"


class FakeGitHub(monitor.GitHub):
    def __init__(self):
        self.posts = []
        self.requests = []
        self.routes = {
            TRACKER: {"state": "open"},
            TRACKER + "/comments": [],
            UPSTREAM + "/pulls/1080": {
                "number": 1080, "title": "Example", "body": "Text",
                "state": "open", "merged": False, "draft": False,
                "head": {"sha": HEAD}, "base": {"ref": "main"},
                "merge_commit_sha": MERGE, "labels": [{"name": "bug"}],
                "requested_reviewers": [{"login": "reviewer"}],
                "requested_teams": [],
            },
            UPSTREAM + "/issues/1080/comments": [],
            UPSTREAM + "/pulls/1080/comments": [],
            UPSTREAM + "/pulls/1080/reviews": [],
        }
        for sha in (HEAD, MERGE):
            self.routes[UPSTREAM + f"/commits/{sha}/check-runs"] = {
                "check_runs": [{"id": 1 if sha == HEAD else 2,
                                "name": "test", "status": "completed",
                                "conclusion": "success", "started_at": "start"}]
            }
            self.routes[UPSTREAM + f"/commits/{sha}/statuses"] = []

    def request(self, path, data=None):
        self.requests.append(path)
        if data is not None:
            self.posts.append(data)
            self.routes[TRACKER + "/comments"].append({
                "id": len(self.posts), "body": data["body"],
                "user": {"login": "github-actions[bot]", "type": "Bot"},
            })
            return {"id": len(self.posts)}
        value = self.routes[path.split("?")[0]]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()

    def test_initializes_once_then_unchanged_run_does_not_write(self):
        self.assertEqual(monitor.run(self.api, 42), "initialized")
        self.assertEqual(monitor.run(self.api, 42), "unchanged")
        self.assertEqual(len(self.api.posts), 1)
        self.assertIn("@omariomari2", self.api.posts[0]["body"])
        self.assertEqual(set(monitor.read_state(self.api.routes[TRACKER + "/comments"])),
                         set(monitor.CATEGORIES))

    def test_changed_categories_are_notified_once_without_untrusted_text(self):
        monitor.run(self.api, 42)
        self.api.routes[UPSTREAM + "/pulls/1080"]["title"] = "@everyone unsafe"
        self.api.routes[UPSTREAM + "/issues/1080/comments"].append(
            {"id": 3, "body": "@someone <script>", "updated_at": "today"})
        self.assertEqual(monitor.run(self.api, 42), "notified")
        visible = self.api.posts[-1]["body"].split("<!--")[0]
        self.assertIn("PR details", visible)
        self.assertIn("comments", visible)
        self.assertNotIn("unsafe", visible)
        self.assertNotIn("@someone", visible)
        self.assertNotIn("<script>", visible)
        self.assertEqual(monitor.run(self.api, 42), "unchanged")
        self.assertEqual(len(self.api.posts), 2)

    def test_api_failure_makes_no_write(self):
        self.api.routes[UPSTREAM + "/pulls/1080/reviews"] = monitor.MonitorError("API failed")
        with self.assertRaises(monitor.MonitorError):
            monitor.run(self.api, 42)
        self.assertEqual(self.api.posts, [])

    def test_closed_tracking_issue_pauses_without_upstream_requests(self):
        self.api.routes[TRACKER]["state"] = "closed"
        self.assertEqual(monitor.run(self.api, 42), "paused")
        self.assertEqual(self.api.requests, [TRACKER])
        self.assertEqual(self.api.posts, [])

    def test_forged_state_is_ignored_and_malformed_bot_state_fails_closed(self):
        monitor.run(self.api, 42)
        comments = self.api.routes[TRACKER + "/comments"]
        comments.append({"id": 2, "body": "<!-- pr-notify-state:invalid -->",
                         "user": {"login": "attacker", "type": "User"}})
        self.assertEqual(monitor.run(self.api, 42), "unchanged")
        comments[-1]["user"] = {"login": "github-actions[bot]", "type": "Bot"}
        with self.assertRaises(monitor.MonitorError):
            monitor.run(self.api, 42)
        self.assertEqual(len(self.api.posts), 1)

    def test_latest_state_uses_comment_id_not_input_order(self):
        monitor.run(self.api, 42)
        self.api.routes[UPSTREAM + "/pulls/1080"]["title"] = "new"
        monitor.run(self.api, 42)
        self.api.routes[TRACKER + "/comments"].reverse()
        self.assertEqual(monitor.run(self.api, 42), "unchanged")

    def test_normalization_ignores_order_and_volatile_fields(self):
        pr = self.api.routes[UPSTREAM + "/pulls/1080"]
        pr["labels"] += [{"name": "help"}]
        comments = self.api.routes[UPSTREAM + "/issues/1080/comments"]
        comments.extend([{"id": 3, "body": "a"}, {"id": 4, "body": "b"}])
        monitor.run(self.api, 42)
        pr["labels"].reverse()
        pr.update({"updated_at": "later", "mergeable": None, "mergeable_state": "unknown"})
        comments.reverse()
        self.assertEqual(monitor.run(self.api, 42), "unchanged")
        comments[0]["body"] = "edited"
        self.assertEqual(monitor.run(self.api, 42), "notified")

    def test_submitted_reviews_and_merge_commit_reruns_are_detected(self):
        monitor.run(self.api, 42)
        reviews = self.api.routes[UPSTREAM + "/pulls/1080/reviews"]
        reviews.append({"id": 5, "state": "PENDING", "body": "draft"})
        self.assertEqual(monitor.run(self.api, 42), "unchanged")
        reviews[-1].update({"state": "APPROVED", "submitted_at": "now"})
        self.assertEqual(monitor.run(self.api, 42), "notified")
        self.assertIn("1 approved", self.api.posts[-1]["body"])
        self.assertIn("PR open", self.api.posts[-1]["body"])
        checks = self.api.routes[UPSTREAM + f"/commits/{MERGE}/check-runs"]["check_runs"]
        checks[0]["id"] = 20
        self.assertEqual(monitor.run(self.api, 42), "notified")
        checks[0]["conclusion"] = "failure"
        self.assertEqual(monitor.run(self.api, 42), "notified")
        self.assertIn("1 failure", self.api.posts[-1]["body"])

    def test_merged_notification_reports_state_and_validated_head(self):
        monitor.run(self.api, 42)
        self.api.routes[UPSTREAM + "/pulls/1080"].update({"state": "closed", "merged": True})
        monitor.run(self.api, 42)
        visible = self.api.posts[-1]["body"].split("<!--")[0]
        self.assertIn("PR merged", visible)
        self.assertIn("head aaaaaaaa", visible)

    def test_dry_run_prints_safe_useful_summaries_without_token_or_writes(self):
        self.api.routes[UPSTREAM + "/pulls/1080"]["title"] = "@unsafe"
        with patch("sys.argv", ["pr_notify.py", "--dry-run"]), \
                patch.dict("os.environ", {"GH_TOKEN": "secret-value"}), \
                patch.object(monitor, "GitHub", return_value=self.api) as constructor, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(monitor.main(), 0)
        constructor.assert_called_once_with()
        result = json.loads(output.getvalue())
        self.assertIn("PR open", result["categories"]["PR details"]["summary"])
        self.assertIn("2 success", result["categories"]["checks"]["summary"])
        self.assertNotIn("@unsafe", output.getvalue())
        self.assertNotIn("secret-value", output.getvalue())
        self.assertEqual(self.api.posts, [])

    def test_transient_merge_recalculation_preserves_previous_state(self):
        monitor.run(self.api, 42)
        pr = self.api.routes[UPSTREAM + "/pulls/1080"]
        pr.update({"merge_commit_sha": None, "mergeable": None})
        with self.assertRaises(monitor.MonitorError):
            monitor.run(self.api, 42)
        pr.update({"merge_commit_sha": MERGE, "mergeable": True})
        self.assertEqual(monitor.run(self.api, 42), "unchanged")
        self.assertEqual(len(self.api.posts), 1)

    def test_empty_synthetic_merge_sha_changes_do_not_notify(self):
        self.api.routes[UPSTREAM + f"/commits/{MERGE}/check-runs"] = {"check_runs": []}
        monitor.run(self.api, 42)
        replacement = "c" * 40
        self.api.routes[UPSTREAM + "/pulls/1080"]["merge_commit_sha"] = replacement
        self.api.routes[UPSTREAM + f"/commits/{replacement}/check-runs"] = {"check_runs": []}
        self.api.routes[UPSTREAM + f"/commits/{replacement}/statuses"] = []
        self.assertEqual(monitor.run(self.api, 42), "unchanged")
        self.api.routes[UPSTREAM + "/pulls/1080"].update({"merge_commit_sha": None, "mergeable": False})
        self.assertEqual(monitor.run(self.api, 42), "unchanged")

    def test_only_latest_commit_status_for_each_context_matters(self):
        statuses = self.api.routes[UPSTREAM + f"/commits/{HEAD}/statuses"]
        statuses.extend([{"id": 8, "context": "build", "state": "success"},
                         {"id": 7, "context": "build", "state": "pending"}])
        monitor.run(self.api, 42)
        statuses.reverse()
        statuses[0]["state"] = "failure"
        self.assertEqual(monitor.run(self.api, 42), "unchanged")
        statuses.append({"id": 9, "context": "build", "state": "pending"})
        self.assertEqual(monitor.run(self.api, 42), "notified")

    def test_merge_commit_404_fails_closed(self):
        self.api.routes[UPSTREAM + f"/commits/{MERGE}/check-runs"] = monitor.MonitorError("HTTP 404")
        with self.assertRaises(monitor.MonitorError):
            monitor.run(self.api, 42)
        self.assertEqual(self.api.posts, [])

    def test_pagination_includes_all_pages_without_following_link_urls(self):
        api = monitor.GitHub()
        pages = [[{"id": i} for i in range(100)], [{"id": 100}]]
        with patch.object(api, "request", side_effect=pages) as request:
            self.assertEqual(len(api.paginate(UPSTREAM + "/issues/1080/comments")), 101)
        self.assertTrue(request.call_args_list[-1].args[0].endswith("per_page=100&page=2"))

    def test_invalid_paths_and_tracking_ids_are_rejected(self):
        with self.assertRaises(monitor.MonitorError):
            monitor.GitHub().request("https://attacker.test/")
        with self.assertRaises(monitor.MonitorError):
            monitor.run(self.api, "42/../../other")
        self.assertEqual(self.api.posts, [])

    def test_post_failure_is_not_retried(self):
        original = self.api.request
        attempts = []
        def fail_post(path, data=None):
            if data is not None:
                attempts.append(data)
                raise monitor.MonitorError("uncertain POST")
            return original(path)
        with patch.object(self.api, "request", side_effect=fail_post):
            with self.assertRaises(monitor.MonitorError):
                monitor.run(self.api, 42)
        self.assertEqual(len(attempts), 1)


if __name__ == "__main__":
    unittest.main()
