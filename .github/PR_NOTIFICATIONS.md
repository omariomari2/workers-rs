# Personal PR notifications

This fork-only workflow monitors [Cloudflare workers-rs PR #1080](https://github.com/cloudflare/workers-rs/pull/1080)
and mentions `@omariomari2` in [tracking issue #1](https://github.com/omariomari2/workers-rs/issues/1).
It is not part of the upstream contribution.

- Runs at 00:07 and 12:07 UTC. GitHub can delay scheduled runs.
- The first run posts a baseline. Later runs post only when PR details/commits,
  discussion or inline comments, submitted reviews, or checks/statuses change.
- Notifications link to the affected PR sections. Public upstream text is
  hashed, not copied into notifications or executed.
- A bot comment stores the notification and its state in one write. Concurrent
  runs are serialized; a failed read does not replace the saved state.
- Closing the tracking issue pauses notifications; reopening resumes them.
  Disable **PR notifications** in Actions to stop the scheduled jobs entirely.
- Inbox/email/push delivery follows your GitHub notification settings.

The workflow uses only the repository's built-in `GITHUB_TOKEN`, with
`contents: read` and `issues: write`. It does not check out the upstream PR,
post upstream comments, or require a personal access token. The fixed repository
guard and `main` branch guard prevent accidental execution in other forks or on
contribution branches. Other inherited workflows are not needed for this monitor.

## Verify or run manually

Select **Actions -> PR notifications -> Run workflow** on `main`. A first run
should report `initialized`, then an unchanged run should report `unchanged`
without another issue comment. To test locally without credentials or writes:

```sh
python3 -B -m unittest discover -s .github/scripts -p 'test_pr_notify.py' -v
python3 -B .github/scripts/pr_notify.py --dry-run
```

This is periodic snapshot comparison, not an event archive: changes that are
fully reversed between polls may not be reported. A failed run is visible in
Actions and does not advance notification state. Malformed trusted state fails
closed instead of silently resetting. Public-repository schedules can be
disabled by GitHub after 60 days without repository activity; re-enable the
workflow if necessary. Keep this automation off branches submitted upstream.

References: [schedule behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[comment notifications](https://docs.github.com/en/rest/issues/comments#create-an-issue-comment),
[notification settings](https://docs.github.com/en/subscriptions-and-notifications/get-started/configuring-notifications).
