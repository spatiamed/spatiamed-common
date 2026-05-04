# Scripts

## release.sh

Bumps `pyproject.toml`, tags the commit, and rewrites the git-URL pin in every
consumer (platform-api, CareLoop, QueueCare/server, QueueCare/notification_service).

```bash
scripts/release.sh 0.1.2            # actually runs
scripts/release.sh 0.1.2 --dry-run  # prints what it would do
```

After the script finishes, push the tag and review/commit in each consumer repo
yourself — the script intentionally does not commit downstream.
