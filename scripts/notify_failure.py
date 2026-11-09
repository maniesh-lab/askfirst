"""Tell Discord a scheduled triage run failed, so a broken schedule is never silent.

Standard library only: it runs in the workflow's failure step, which may be reached because
installing the app itself failed.

Discord sits behind Cloudflare, which refuses requests with Python's default `Python-urllib`
User-Agent, so the request names a client of its own.

Run: `DISCORD_WEBHOOK_URL=... RUN_URL=... python scripts/notify_failure.py`
"""

import json
import os
import sys
import urllib.error
import urllib.request

USER_AGENT = "askfirst-triage (https://github.com/maniesh-lab/askfirst, 0.1)"


def build_request(webhook_url: str, run_url: str) -> urllib.request.Request:
    body = {
        "content": f"Triage run failed: {run_url}",
        # Never ping anyone.
        "allowed_mentions": {"parse": []},
    }
    return urllib.request.Request(
        webhook_url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )


def main() -> int:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL is not set; nothing to send")
        return 0
    run_url = os.environ.get("RUN_URL", "(run URL unknown)")
    try:
        with urllib.request.urlopen(build_request(webhook_url, run_url), timeout=10) as response:
            print(f"failure message sent (HTTP {response.status})")
    except urllib.error.URLError as exc:
        # The webhook URL holds a secret token, so it is never printed.
        reason = getattr(exc, "code", None) or type(exc).__name__
        print(f"could not post the failure message: {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
