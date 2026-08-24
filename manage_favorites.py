# ==========================================
# TELEGRAM FAVORITES BOT
# ==========================================
# One-shot command processor, run every ~10 minutes by
# .github/workflows/favorites-bot.yml: polls getUpdates, applies commands
# from the admin chat to favorite_shows.txt, commits/pushes the file,
# acknowledges the updates, then sends confirmation replies.
#
# Ordering is deliberate: save -> commit -> push -> ack -> reply. If the
# push fails we exit non-zero WITHOUT acking, so the commands are
# re-fetched and re-applied on the next run (handlers are idempotent) and
# no ✅ is ever sent for a change that did not land.
#
# Commands (leading '/' optional, one command per message line):
#     add <slug or tiwall URL>
#     remove <slug>
#     exclude <slug> <date>[، <date> ...]
#     include <slug> <date>[، <date> ...]   |   include <slug> all
#     list
#     help

import argparse
import os
import re
import subprocess
import sys

import requests

from favorites import (
    FAVORITES_FILE,
    load_entries,
    normalize_date,
    save_entries,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")  # the only chat obeyed

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
BASE_URL = "https://www.tiwall.com"

SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Dates may be separated by '|', ',' or the Persian comma '،'
DATE_SPLIT_RE = re.compile(r"[|,،]")

USAGE = (
    "📖 Commands (one per line):\n"
    "/add <slug or tiwall link>\n"
    "/remove <slug>\n"
    "/exclude <slug> <date>[، <date>…]\n"
    "/include <slug> <date>[، <date>…]\n"
    "/include <slug> all\n"
    "/list"
)


# ==========================================
# TELEGRAM API
# ==========================================

def get_updates(offset=None):
    params = {"timeout": 0, "limit": 100, "allowed_updates": '["message"]'}
    if offset is not None:
        params["offset"] = offset
        params["limit"] = 1
    resp = requests.get(f"{API_URL}/getUpdates", params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"getUpdates failed: {payload}")
    return payload.get("result", [])


def send_message(chat_id, text):
    if len(text) > 4096:
        text = text[:4095] + "…"
    try:
        requests.post(
            f"{API_URL}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"Failed to send reply: {e}")


# ==========================================
# COMMAND HANDLERS
# ==========================================
# Each returns (changed: bool, reply: str) and mutates `entries` in place.
# Comment entries are never touched.

def find_show(entries, slug):
    for i, entry in enumerate(entries):
        if entry[0] == "show" and entry[1] == slug:
            return i
    return None


def clean_slug(arg):
    """Accepts a bare slug, '#slug', or a pasted tiwall URL; returns the slug."""
    slug = arg.strip().lstrip("#")
    if "/" in slug:
        # e.g. https://www.tiwall.com/s/doshman13?x=1 -> doshman13
        slug = slug.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        slug = slug.rsplit("/", 1)[-1]
    return slug


def check_slug_on_tiwall(slug):
    """Best-effort existence check; returns False only on a definite miss."""
    try:
        resp = requests.get(f"{BASE_URL}/s/{slug}", timeout=10)
        return resp.status_code == 200
    except requests.RequestException:
        return True  # network trouble must never block an add


def split_dates(text):
    return [d.strip() for d in DATE_SPLIT_RE.split(text) if d.strip()]


def cmd_add(entries, arg):
    slug = clean_slug(arg)
    if not SLUG_RE.match(slug):
        return False, f"❌ Invalid slug: '{slug}'. Use letters/digits/._- only."
    if find_show(entries, slug) is not None:
        return False, f"⚠️ '{slug}' is already in favorites."
    entries.append(("show", slug, []))
    reply = f"✅ Added '{slug}' to favorites."
    if not check_slug_on_tiwall(slug):
        reply += f"\n⚠️ Note: {BASE_URL}/s/{slug} did not load — check the slug."
    return True, reply


def cmd_remove(entries, arg):
    slug = clean_slug(arg)
    index = find_show(entries, slug)
    if index is None:
        return False, f"❌ '{slug}' is not in favorites."
    del entries[index]
    return True, f"🗑 Removed '{slug}' from favorites."


def cmd_exclude(entries, arg):
    parts = arg.split(None, 1)
    if len(parts) < 2:
        return False, "❌ Usage: /exclude <slug> <date>"
    slug, dates = clean_slug(parts[0]), split_dates(parts[1])
    index = find_show(entries, slug)
    if index is None:
        return False, f"❌ '{slug}' is not in favorites. /add it first."
    if not dates:
        return False, "❌ Usage: /exclude <slug> <date>"
    _, _, existing = entries[index]
    existing_norm = {normalize_date(d) for d in existing}
    added, dupes = [], []
    for date in dates:
        if normalize_date(date) in existing_norm:
            dupes.append(date)
        else:
            existing.append(date)
            existing_norm.add(normalize_date(date))
            added.append(date)
    lines = []
    if added:
        lines.append(f"✅ Excluded for '{slug}': {'، '.join(added)}")
    if dupes:
        lines.append(f"⚠️ Already excluded for '{slug}': {'، '.join(dupes)}")
    return bool(added), "\n".join(lines)


def cmd_include(entries, arg):
    parts = arg.split(None, 1)
    if len(parts) < 2:
        return False, "❌ Usage: /include <slug> <date> (or: /include <slug> all)"
    slug = clean_slug(parts[0])
    index = find_show(entries, slug)
    if index is None:
        return False, f"❌ '{slug}' is not in favorites."
    _, _, existing = entries[index]
    if parts[1].strip().lower() == "all":
        if not existing:
            return False, f"⚠️ '{slug}' has no exclusions."
        existing.clear()
        return True, f"✅ Cleared all exclusions for '{slug}'."
    removed, missing = [], []
    for date in split_dates(parts[1]):
        needle = normalize_date(date)
        match = next((d for d in existing if normalize_date(d) == needle), None)
        if match is None:
            missing.append(date)
        else:
            existing.remove(match)
            removed.append(date)
    lines = []
    if removed:
        lines.append(f"✅ Removed exclusion(s) for '{slug}': {'، '.join(removed)}")
    if missing:
        lines.append(f"❌ No such exclusion for '{slug}': {'، '.join(missing)}")
    return bool(removed), "\n".join(lines)


def cmd_list(entries):
    shows = [e for e in entries if e[0] == "show"]
    if not shows:
        return False, "📋 No favorites yet. /add <slug> to start."
    lines = ["📋 Favorites:"]
    for _, slug, dates in shows:
        if dates:
            lines.append(f"• {slug} — excluded: {'، '.join(dates)}")
        else:
            lines.append(f"• {slug}")
    return False, "\n".join(lines)


def handle_command(entries, line):
    """Parses and applies one command line; returns (changed, reply)."""
    line = line.strip()
    if not line:
        return False, ""
    parts = line.split(None, 1)
    # tolerate "/add", "add" and "/add@MyBot"
    command = parts[0].lstrip("/").split("@", 1)[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command == "list":
        return cmd_list(entries)
    if command in ("help", "start"):
        return False, USAGE
    if command in ("add", "remove") and not arg:
        return False, f"❌ Usage: /{command} <slug>"
    if command == "add":
        return cmd_add(entries, arg)
    if command == "remove":
        return cmd_remove(entries, arg)
    if command == "exclude":
        return cmd_exclude(entries, arg)
    if command == "include":
        return cmd_include(entries, arg)
    return False, f"❓ Unknown command: '{parts[0]}'\n\n{USAGE}"


# ==========================================
# GIT
# ==========================================

def commit_and_push():
    subprocess.run(["git", "add", FAVORITES_FILE], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        print("File unchanged on disk; nothing to commit.")
        return
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(
        ["git", "config", "user.email",
         "41898282+github-actions[bot]@users.noreply.github.com"],
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Update favorite_shows.txt via Telegram bot"],
        check=True,
    )
    for attempt in range(1, 4):
        subprocess.run(
            ["git", "pull", "--rebase", "--autostash", "origin", "main"],
            check=True,
        )
        if subprocess.run(["git", "push"]).returncode == 0:
            return
        print(f"Push failed (attempt {attempt}/3).")
    raise RuntimeError("git push failed after 3 attempts")


# ==========================================
# MAIN
# ==========================================

def process_messages(texts, dry_run=False, use_git=True):
    """Applies command messages to the favorites file.

    Returns one reply string per input message. Saving/committing is skipped
    when nothing changed or when dry_run is set.
    """
    entries = load_entries()
    replies = []
    any_changed = False
    for text in texts:
        message_replies = []
        for line in text.splitlines():
            changed, reply = handle_command(entries, line)
            any_changed = any_changed or changed
            if reply:
                message_replies.append(reply)
        replies.append("\n".join(message_replies))

    if any_changed and not dry_run:
        save_entries(entries)
        if use_git:
            commit_and_push()
    return replies


def main():
    parser = argparse.ArgumentParser(description="Telegram favorites bot (one-shot).")
    parser.add_argument("--dry-run", action="store_true",
                        help="parse and apply in memory only: no save, no git, no ack, no replies sent")
    parser.add_argument("--simulate", metavar="TEXT",
                        help="treat TEXT as an admin message instead of polling Telegram; no git, no ack")
    args = parser.parse_args()

    if args.simulate is not None:
        for reply in process_messages([args.simulate], dry_run=args.dry_run, use_git=False):
            print(reply or "(no reply)")
        return

    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is not set.")
    if not ADMIN_CHAT_ID:
        raise SystemExit("ADMIN_CHAT_ID environment variable is not set.")

    updates = get_updates()
    if not updates:
        print("No updates.")
        return

    # Ack junk too (other chats, stickers, ...) so it never piles up.
    max_update_id = max(u["update_id"] for u in updates)

    texts = []
    for update in updates:
        message = update.get("message") or {}
        text = message.get("text")
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if text and chat_id == ADMIN_CHAT_ID:
            texts.append(text)
        else:
            print(f"Ignoring update {update['update_id']} (chat {chat_id or '?'}).")

    replies = process_messages(texts, dry_run=args.dry_run)

    if args.dry_run:
        print(f"[dry-run] Would ack up to update {max_update_id}.")
        for reply in replies:
            print(f"[dry-run] Reply: {reply or '(no reply)'}")
        return

    # Only after a successful save+push: confirm receipt, then reply.
    get_updates(offset=max_update_id + 1)
    for reply in replies:
        if reply:
            send_message(ADMIN_CHAT_ID, reply)
    print(f"Processed {len(texts)} message(s), acked through update {max_update_id}.")


if __name__ == "__main__":
    main()
