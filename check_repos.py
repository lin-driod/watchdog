#!/usr/bin/env python3
"""
GitHub Watchdog — Cloud-side checker (runs in GitHub Actions)

Reads repos.json (list of repos + scope), fetches the latest release / tag /
commit for each repo via the GitHub API (using the built-in GITHUB_TOKEN),
compares against state.json, and pushes a notification to ntfy.sh for every
change detected.

First run: records baseline only, no notifications (avoids flooding).
Subsequent runs: notify on any new release/tag/commit.
"""
import json
import os
import sys
import urllib.error
import urllib.request

REPOS_FILE = "repos.json"
STATE_FILE = "state.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
API_BASE = "https://api.github.com"
TIMEOUT = 15


def api_get(path):
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-watchdog",
    }
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        body = e.read().decode("utf-8", errors="replace")[:200]
        print(f"  HTTP {e.code} — {url}: {body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  error — {url}: {e}", file=sys.stderr)
        return None


def ntfy_push(title, message, click_url=""):
    if not NTFY_TOPIC:
        print("  (NTFY_TOPIC not set — skipping push)")
        return
    headers = {"Title": title, "Tags": "package", "Priority": "default"}
    if click_url:
        headers["Click"] = click_url
    data = message.encode("utf-8")
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}", data=data, headers=headers, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"  → ntfy pushed: {title}")
    except Exception as e:
        print(f"  → ntfy push FAILED: {e}", file=sys.stderr)


def main():
    with open(REPOS_FILE) as f:
        config = json.load(f)
    repos = config.get("repos", [])
    scope = config.get("scope", "release")  # release | tag | commit

    first_run = not os.path.exists(STATE_FILE)
    if first_run:
        state = {}
        print("=== FIRST RUN — recording baseline, no notifications ===\n")
    else:
        with open(STATE_FILE) as f:
            state = json.load(f)

    changed = False
    notifications = []

    for entry in repos:
        repo_full = entry if isinstance(entry, str) else entry.get("name", "")
        parts = repo_full.split("/")
        if len(parts) != 2:
            print(f"  skip invalid entry: {entry}")
            continue
        owner, name = parts
        print(f"Checking {owner}/{name} …")
        rs = state.setdefault(repo_full, {})

        # ── Release ────────────────────────────────────────────────────
        rel = api_get(f"/repos/{owner}/{name}/releases/latest")
        if rel and rel.get("id"):
            rid = rel["id"]
            if rs.get("release_id") != rid:
                if not first_run:
                    tag = rel.get("tag_name", "?")
                    url = rel.get(
                        "html_url", f"https://github.com/{owner}/{name}/releases"
                    )
                    preview = (rel.get("body") or "").strip().split("\n")[0][:160]
                    msg = f"新 release: {tag}"
                    if preview:
                        msg += f"\n{preview}"
                    notifications.append((f"📦 {owner}/{name}", msg, url))
                rs["release_id"] = rid
                changed = True
                print(f"  release → {rel.get('tag_name')} {'(new!)' if not first_run else '(baseline)'}")
        else:
            print("  no releases")

            # ── Tag fallback (only if scope allows) ────────────────────
            if scope in ("tag", "commit"):
                tags = api_get(f"/repos/{owner}/{name}/tags?per_page=1")
                if tags:
                    latest = tags[0]
                    tsha = latest.get("commit", {}).get("sha", "")
                    if rs.get("tag_sha") != tsha:
                        if not first_run:
                            notifications.append(
                                (
                                    f"🏷️ {owner}/{name}",
                                    f"新 tag: {latest.get('name', '?')}",
                                    f"https://github.com/{owner}/{name}/tags",
                                )
                            )
                        rs["tag_sha"] = tsha
                        changed = True
                        print(f"  tag → {latest.get('name')} {'(new!)' if not first_run else '(baseline)'}")

        # ── Commit (only if scope == commit) ───────────────────────────
        if scope == "commit":
            commits = api_get(f"/repos/{owner}/{name}/commits?per_page=1")
            if commits:
                latest = commits[0]
                csha = latest.get("sha", "")
                if rs.get("commit_sha") != csha:
                    if not first_run:
                        subj = (
                            latest.get("commit", {}).get("message") or ""
                        ).split("\n")[0][:120]
                        notifications.append(
                            (
                                f"🔧 {owner}/{name}",
                                f"新提交: {subj}",
                                f"https://github.com/{owner}/{name}/commit/{csha}",
                            )
                        )
                    rs["commit_sha"] = csha
                    changed = True
                    print(f"  commit → {csha[:8]} {'(new!)' if not first_run else '(baseline)'}")

    # ── Push notifications ────────────────────────────────────────────
    for title, msg, url in notifications:
        ntfy_push(title, msg, url)

    # ── Persist state ─────────────────────────────────────────────────
    if changed or first_run:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        print(f"\nState {'initialized (first run)' if first_run else 'updated'}.")

    print(
        f"\n=== Done: {len(repos)} repos checked, "
        f"{len(notifications)} notifications pushed ==="
    )


if __name__ == "__main__":
    main()
