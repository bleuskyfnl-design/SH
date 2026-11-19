"""
SapphicHeaven Bluesky Reposter
- Allow Mention list: author must be a member AND post must mention @sapphicheaven.bsky.social
- Repost Always list: independent of mention
- Optional custom feeds
- Saved Posts module
- Own Posts module runs last (latest 3 originals)
All reposted content must be an original image/video post (no replies or quotes).
"""

import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from atproto import Client, models

USERNAME = os.getenv("BSKY_USERNAME", "sapphicheaven.bsky.social").strip()
PASSWORD = os.getenv("SAPPHICHEAVEN_PASSWORD", "").strip()

# ------------------------- CONFIGURATION -------------------------

ALLOW_MENTION_LIST_URL = (
    "https://bsky.app/profile/did:plc:6muwszrhdaweasjajswkuy2u/lists/3n2ocuzk5rl2a"
)
REPOST_ALWAYS_LIST_URL = (
    "https://bsky.app/profile/did:plc:6muwszrhdaweasjajswkuy2u/lists/3mznzohqyqd22"
)

# Add feed URLs below. Leave an entry as "" to disable it.
FEED_URLS = [
    "",
    "",
    "",
]

LOOKBACK_HOURS = 12
MAX_TOTAL_POSTS = 100
MAX_NORMAL_POSTS = 50
MAX_PER_AUTHOR = 3
OWN_POSTS_COUNT = 3
MAX_SAVED_POSTS = 50
SLEEP_SECONDS = 2
PROCESS_SAVED_POSTS = True
PROCESS_OWN_POSTS = True
TARGET_MENTION = "@sapphicheaven.bsky.social"

# ----------------------------------------------------------------

client = Client()
now = datetime.now(timezone.utc)
cutoff = now - timedelta(hours=LOOKBACK_HOURS)


def attr(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def parse_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def post_datetime(post):
    record = attr(post, "record")
    return parse_datetime(attr(record, "created_at")) or parse_datetime(
        attr(post, "indexed_at")
    )


def post_uri(post):
    return attr(post, "uri", "")


def post_cid(post):
    return attr(post, "cid", "")


def author_did(post):
    return attr(attr(post, "author"), "did", "")


def author_handle(post):
    return attr(attr(post, "author"), "handle", "unknown")


def embed_kind(post):
    record = attr(post, "record")
    embed = attr(record, "embed") or attr(post, "embed")
    if embed is None:
        return "none"
    name = type(embed).__name__.lower()
    py_type = str(attr(embed, "py_type", "")).lower()
    kind = name + " " + py_type
    if "recordwithmedia" in kind or "record_with_media" in kind:
        return "quote_media"
    if "record" in kind:
        return "quote"
    if "images" in kind:
        return "images"
    if "video" in kind:
        return "video"
    return "other"


def is_original_media(post):
    record = attr(post, "record")
    if record is None or attr(record, "reply") is not None:
        return False
    kind = embed_kind(post)
    return kind in {"images", "video"}


def is_repost_item(item):
    return attr(item, "reason") is not None


def bsky_url_to_at_uri(url, collection):
    """Convert a bsky.app profile list/feed URL into its AT URI."""
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) < 4 or parts[0] != "profile":
        raise ValueError(f"Onbekende Bluesky URL: {url}")
    actor, item_type, rkey = parts[1], parts[2], parts[3]
    expected = "lists" if collection == "app.bsky.graph.list" else "feeds"
    if item_type != expected:
        raise ValueError(f"Verwachtte een {expected}-URL: {url}")
    if actor.startswith("did:"):
        did = actor
    else:
        did = client.resolve_handle(actor).did
    return f"at://{did}/{collection}/{rkey}"


def get_list_members(list_url):
    list_uri = bsky_url_to_at_uri(list_url, "app.bsky.graph.list")
    members = set()
    cursor = None
    while True:
        params = {"list": list_uri, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        response = client.app.bsky.graph.get_list(params=params)
        for item in (attr(response, "items", []) or []):
            subject = attr(item, "subject")
            did = attr(subject, "did")
            if did:
                members.add(did)
        cursor = attr(response, "cursor")
        if not cursor:
            break
    print(f"List-members opgehaald: {len(members)}")
    return members


def list_feed_posts(list_url, label):
    try:
        list_uri = bsky_url_to_at_uri(list_url, "app.bsky.graph.list")
        response = client.app.bsky.feed.get_list_feed(
            params={"list": list_uri, "limit": 100}
        )
        posts = []
        for item in (attr(response, "feed", []) or []):
            if not is_repost_item(item):
                post = attr(item, "post")
                if post:
                    posts.append(post)
        print(f"{label}: {len(posts)} posts uit lijstfeed")
        return posts
    except Exception as exc:
        print(f"WAARSCHUWING {label}: lijstfeed niet beschikbaar: {exc}")
        return []


def feed_posts(feed_url):
    try:
        feed_uri = bsky_url_to_at_uri(feed_url, "app.bsky.feed.generator")
        response = client.app.bsky.feed.get_feed(
            params={"feed": feed_uri, "limit": 100}
        )
        return [
            attr(item, "post")
            for item in (attr(response, "feed", []) or [])
            if not is_repost_item(item) and attr(item, "post")
        ]
    except Exception as exc:
        print(f"WAARSCHUWING feed {feed_url}: {exc}")
        return []


def mentions_target(post):
    record = attr(post, "record")
    text = attr(record, "text", "") or ""
    # Exact handle text (case-insensitive), not a partial substring.
    if re.search(r"(?<![\w.])@sapphicheaven\.bsky\.social(?![\w.-])",
                 text, re.IGNORECASE):
        return True

    # Also inspect mention facets, including posts whose rendered text differs.
    for facet in (attr(record, "facets", []) or []):
        for feature in (attr(facet, "features", []) or []):
            if "mention" in type(feature).__name__.lower():
                did = attr(feature, "did", "")
                if did == client.me.did:
                    return True
    return False


def get_saved_posts():
    selected, seen, cursor = [], set(), None
    while len(selected) < MAX_SAVED_POSTS:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        response = client.app.bsky.bookmark.get_bookmarks(params=params)
        bookmarks = attr(response, "bookmarks", []) or []
        for bookmark in bookmarks:
            item = attr(bookmark, "item")
            post = item
            for field in ("post", "post_view", "postView"):
                nested = attr(item, field)
                if nested is not None:
                    post = nested
                    break
            uri, cid = attr(post, "uri"), attr(post, "cid")
            if uri and cid and uri not in seen and is_original_media(post):
                selected.append(post)
                seen.add(uri)
                if len(selected) >= MAX_SAVED_POSTS:
                    break
        cursor = attr(response, "cursor")
        if not cursor or not bookmarks:
            break
    print(f"Saved Posts: {len(selected)} geschikte foto/video-posts")
    return selected


def get_account_records(collection):
    records, cursor = [], None
    while True:
        params = {"repo": client.me.did, "collection": collection, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        response = client.com.atproto.repo.list_records(params=params)
        page = attr(response, "records", []) or []
        records.extend(page)
        cursor = attr(response, "cursor")
        if not cursor or not page:
            break
    return records


def record_subject_uri(record):
    value = attr(record, "value")
    return attr(attr(value, "subject"), "uri")


def get_existing_reposts():
    mapping = defaultdict(list)
    for record in get_account_records("app.bsky.feed.repost"):
        subject_uri = record_subject_uri(record)
        if subject_uri:
            mapping[subject_uri].append(attr(record, "uri"))
    return mapping


def get_liked_uris():
    return {
        uri for uri in
        (record_subject_uri(r) for r in get_account_records("app.bsky.feed.like"))
        if uri
    }


def delete_existing_reposts(uri, repost_map):
    for record_uri in repost_map.get(uri, []):
        if not record_uri:
            continue
        try:
            client.delete_repost(record_uri)
            time.sleep(SLEEP_SECONDS)
        except Exception as exc:
            print(f"  Bestaande repost verwijderen mislukt: {exc}")


def repost_and_like(post, label, liked_uris, repost_map, force=False,
                    do_like=True):
    uri, cid = post_uri(post), post_cid(post)
    if not uri or not cid:
        return False
    if force:
        delete_existing_reposts(uri, repost_map)
    try:
        client.repost(uri, cid)
        print(f"REPOST [{label}] @{author_handle(post)} {uri}")
        time.sleep(SLEEP_SECONDS)
    except Exception as exc:
        print(f"REPOST FOUT [{label}] {uri}: {exc}")
        return False

    if do_like and uri not in liked_uris:
        try:
            client.like(uri, cid)
            liked_uris.add(uri)
            print(f"LIKE [{label}] {uri}")
            time.sleep(SLEEP_SECONDS)
        except Exception as exc:
            # Already-liked responses should not fail the run.
            msg = str(exc).lower()
            if "already" in msg or "duplicate" in msg:
                liked_uris.add(uri)
            else:
                print(f"LIKE FOUT [{label}] {uri}: {exc}")
    return True


def get_own_posts():
    """Eyescandy-style author feed: newest originals, direct images/videos only."""
    own_did = client.me.did
    response = client.get_author_feed(actor=own_did, limit=100)
    found = []
    for item in response.feed:
        if attr(item, "reason") is not None:
            continue
        post = attr(item, "post")
        if not post or author_did(post) != own_did:
            continue
        record = attr(post, "record")
        if record is None or attr(record, "reply") is not None:
            continue
        embed = attr(record, "embed")
        if isinstance(embed, (models.AppBskyEmbedRecord.Main,
                              models.AppBskyEmbedRecordWithMedia.Main)):
            continue
        if not isinstance(embed, (models.AppBskyEmbedImages.Main,
                                  models.AppBskyEmbedVideo.Main)):
            continue
        found.append(post)
        if len(found) >= OWN_POSTS_COUNT:
            break
    found.reverse()  # oldest first, newest last
    print(f"Own Posts: {len(found)} eigen originele foto/video-posts")
    return found


def main():
    if not PASSWORD:
        raise SystemExit("SAPPHICHEAVEN_PASSWORD ontbreekt in GitHub Secrets.")

    print("=" * 62)
    print("SAPPHICHEAVEN REPOSTER")
    print(f"Account: {USERNAME}")
    print(f"Lookback: {LOOKBACK_HOURS} uur")
    print("=" * 62)

    client.login(USERNAME, PASSWORD)
    print(f"Ingelogd als {client.me.handle}")

    liked_uris = get_liked_uris()
    repost_map = get_existing_reposts()

    # Build one global normal candidate pool; URI dedup across every source.
    candidates = {}
    allow_members = get_list_members(ALLOW_MENTION_LIST_URL)

    allow_posts = list_feed_posts(ALLOW_MENTION_LIST_URL, "Allow Mention")
    for post in allow_posts:
        dt = post_datetime(post)
        if (author_did(post) in allow_members and mentions_target(post)
                and is_original_media(post) and dt and dt >= cutoff):
            candidates.setdefault(post_uri(post), (dt, post, "Allow Mention"))

    always_posts = list_feed_posts(REPOST_ALWAYS_LIST_URL, "Repost Always")
    for post in always_posts:
        dt = post_datetime(post)
        if is_original_media(post) and dt and dt >= cutoff:
            candidates.setdefault(post_uri(post), (dt, post, "Repost Always"))

    for index, feed_url in enumerate(FEED_URLS, start=1):
        if not feed_url.strip():
            continue
        for post in feed_posts(feed_url.strip()):
            dt = post_datetime(post)
            if is_original_media(post) and dt and dt >= cutoff:
                candidates.setdefault(post_uri(post), (dt, post, f"Feed {index}"))

    # Oldest to newest; maximum 50 normal reposts and 3 per author globally.
    ordered = sorted(candidates.values(), key=lambda item: item[0])
    author_counts = defaultdict(int)
    normal_done = 0
    normal_budget = min(MAX_NORMAL_POSTS, MAX_TOTAL_POSTS - OWN_POSTS_COUNT)

    for _, post, source in ordered:
        if normal_done >= normal_budget:
            break
        did = author_did(post)
        if author_counts[did] >= MAX_PER_AUTHOR:
            continue
        if repost_and_like(post, source, liked_uris, repost_map):
            author_counts[did] += 1
            normal_done += 1

    # Saved Posts are separate from Own Posts and run after normal sources.
    saved_done = 0
    if PROCESS_SAVED_POSTS:
        try:
            saved_posts = get_saved_posts()
            remaining = max(0, MAX_TOTAL_POSTS - OWN_POSTS_COUNT - normal_done)
            for post in saved_posts:
                if saved_done >= min(MAX_SAVED_POSTS, remaining):
                    break
                if repost_and_like(post, "Saved Posts", liked_uris,
                                   repost_map, force=True, do_like=True):
                    saved_done += 1
        except Exception as exc:
            print(f"WAARSCHUWING Saved Posts-module: {exc}")

    # Own Posts are always last; reserve up to three actions for them.
    own_done = 0
    if PROCESS_OWN_POSTS:
        try:
            own_posts = get_own_posts()
            for post in own_posts:
                if own_done >= OWN_POSTS_COUNT:
                    break
                if normal_done + saved_done + own_done >= MAX_TOTAL_POSTS:
                    break
                if repost_and_like(post, "Own Posts", liked_uris,
                                   repost_map, force=True, do_like=False):
                    own_done += 1
        except Exception as exc:
            print(f"WAARSCHUWING Own Posts-module: {exc}")

    total = normal_done + saved_done + own_done
    print("=" * 62)
    print(f"KLAAR — normaal: {normal_done}, Saved Posts: {saved_done}, "
          f"Own Posts: {own_done}, totaal: {total}/{MAX_TOTAL_POSTS}")
    print("=" * 62)


if __name__ == "__main__":
    main()
