import json
import os
import shutil
import subprocess
from pathlib import Path

from atproto import Client, models
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PIL import Image, ImageOps

HANDLE = os.getenv("BLUESKY_HANDLE", "sapphicheaven.bsky.social")
PASSWORD = os.getenv("SAPPHICHEAVEN_PASSWORD") or os.getenv("BLUESKY_PASSWORD")
RAW_SERVICE_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
# Existing Bleusky Photos root ID from the original photo_manager.py.
DRIVE_ROOT_ID = os.getenv("DRIVE_ROOT_FOLDER_ID", "1JQx_1SBOETCSMlmlQSIFOjSBuL4op5KA")
ACCOUNT = "sapphicheaven"
DOWNLOAD = Path("photo_downloads")
CONVERTED = DOWNLOAD / "converted"
SCOPES = ["https://www.googleapis.com/auth/drive"]
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".wmv",
             ".flv", ".mpeg", ".mpg", ".3gp", ".mts", ".m2ts", ".ogv", ".gif"}


def log(message):
    print(f"[SapphicHeaven New Post] {message}", flush=True)


def drive_service():
    if not RAW_SERVICE_JSON:
        raise RuntimeError("Missing GitHub Secret GOOGLE_SERVICE_ACCOUNT_JSON.")
    try:
        info = json.loads(RAW_SERVICE_JSON)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GOOGLE_SERVICE_ACCOUNT_JSON is invalid JSON: {exc}") from exc
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_folder(drive, folder_id):
    return drive.files().get(fileId=folder_id, fields="id,name,mimeType,parents",
                             supportsAllDrives=True).execute()


def child_folder(drive, name, parent_id):
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    q = (f"name='{safe}' and '{parent_id}' in parents "
         "and mimeType='application/vnd.google-apps.folder' and trashed=false")
    result = drive.files().list(q=q, fields="files(id,name)", pageSize=100,
                                supportsAllDrives=True,
                                includeItemsFromAllDrives=True).execute()
    matches = result.get("files", [])
    if len(matches) > 1:
        raise RuntimeError(f"Duplicate '{name}' folders found under the expected parent.")
    return matches[0] if matches else None


def verify_folders(drive):
    root = get_folder(drive, DRIVE_ROOT_ID)
    if root.get("mimeType") != "application/vnd.google-apps.folder" or root.get("name") != "Bleusky Photos":
        raise RuntimeError(
            f"Drive root ID did not resolve to 'Bleusky Photos' (found '{root.get('name')}'). "
            "Check DRIVE_ROOT_FOLDER_ID."
        )
    account = child_folder(drive, ACCOUNT, root["id"])
    if not account:
        raise RuntimeError("Folder Bleusky Photos/sapphicheaven was not found.")
    folders = {}
    for key in ("ready", "used", "failed"):
        folders[key] = child_folder(drive, key, account["id"])
        if not folders[key]:
            raise RuntimeError(f"Required folder sapphicheaven/{key} was not found.")
    log("Verified Bleusky Photos/sapphicheaven/{ready,used,failed}.")
    return folders


def list_media(drive, ready_id):
    found, token = [], None
    while True:
        result = drive.files().list(
            q=f"'{ready_id}' in parents and trashed=false",
            pageSize=1000, pageToken=token, orderBy="createdTime",
            fields="nextPageToken,files(id,name,mimeType,size,createdTime)",
            supportsAllDrives=True, includeItemsFromAllDrives=True
        ).execute()
        for item in result.get("files", []):
            ext = Path(item["name"]).suffix.lower()
            if ext in IMAGE_EXT or ext in VIDEO_EXT:
                found.append(item)
        token = result.get("nextPageToken")
        if not token:
            return found


def download(drive, item):
    DOWNLOAD.mkdir(parents=True, exist_ok=True)
    path = DOWNLOAD / Path(item["name"]).name
    path.unlink(missing_ok=True)
    req = drive.files().get_media(fileId=item["id"], supportsAllDrives=True)
    with path.open("wb") as out:
        grab = MediaIoBaseDownload(out, req, chunksize=1024 * 1024)
        done = False
        while not done:
            status, done = grab.next_chunk()
            if status:
                log(f"Download {int(status.progress() * 100)}%")
    expected = int(item.get("size", 0))
    if expected and path.stat().st_size != expected:
        path.unlink(missing_ok=True)
        raise RuntimeError("Downloaded file size does not match Google Drive.")
    return path


def move_file(drive, item, source_id, target_id):
    drive.files().update(fileId=item["id"], addParents=target_id,
                         removeParents=source_id, fields="id,name,parents",
                         supportsAllDrives=True).execute()


def ffmpeg(args):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is not installed.")
    p = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                       capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout or "FFmpeg failed")[-2500:])


def prepare_image(path):
    with Image.open(path) as source:
        im = ImageOps.exif_transpose(source)
        width, height = im.size
        convert = (path.suffix.lower() == ".webp" or width > 2000 or height > 2000
                   or path.stat().st_size > 900_000 or im.mode not in ("RGB", "L"))
        if not convert:
            return path, width, height
        CONVERTED.mkdir(parents=True, exist_ok=True)
        out = CONVERTED / f"{path.stem}_converted.jpg"
        im.thumbnail((2000, 2000))
        if im.mode in ("RGBA", "LA") or "transparency" in im.info:
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, "white")
            bg.paste(rgba, mask=rgba.getchannel("A"))
            im = bg
        else:
            im = im.convert("RGB")
        im.save(out, "JPEG", quality=90, optimize=True)
    with Image.open(out) as check:
        return out, check.width, check.height


def prepare_video(path):
    CONVERTED.mkdir(parents=True, exist_ok=True)
    out = CONVERTED / f"{path.stem}_compatible.mp4"
    if path.suffix.lower() == ".gif":
        ffmpeg(["-i", str(path), "-vf",
                "scale='min(1920,iw)':-2:force_original_aspect_ratio=decrease,fps=30,format=yuv420p",
                "-an", "-movflags", "+faststart", str(out)])
    else:
        ffmpeg(["-i", str(path), "-vf",
                "scale='min(1920,iw)':-2:force_original_aspect_ratio=decrease,format=yuv420p",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)])
    if not out.exists() or not out.stat().st_size:
        raise RuntimeError("Video conversion did not produce an MP4.")
    return out


def main():
    if not PASSWORD:
        raise RuntimeError("Missing GitHub Secret SAPPHICHEAVEN_PASSWORD.")
    if not RAW_SERVICE_JSON:
        raise RuntimeError("Missing GitHub Secret GOOGLE_SERVICE_ACCOUNT_JSON.")

    drive = drive_service()
    folders = verify_folders(drive)
    ready_id = folders["ready"]["id"]
    candidates = list_media(drive, ready_id)
    if not candidates:
        log("No supported media in ready. Nothing to post.")
        return

    item = candidates[0]  # Original manager selected the oldest file first.
    log(f"Selected oldest ready file: {item['name']}")
    local = download(drive, item)
    ext = local.suffix.lower()

    try:
        if ext in IMAGE_EXT:
            upload_path, width, height = prepare_image(local)
            kind = "image"
        elif ext in VIDEO_EXT:
            upload_path = prepare_video(local)
            width = height = None
            kind = "video"
        else:
            raise RuntimeError(f"Unsupported file type: {ext}")
    except Exception as exc:
        log(f"Media preparation failed: {exc}; moving file to failed.")
        move_file(drive, item, ready_id, folders["failed"]["id"])
        raise

    client = Client()
    client.login(HANDLE, PASSWORD)
    log(f"Logged in as {HANDLE}.")

    with upload_path.open("rb") as f:
        blob = client.upload_blob(f.read()).blob

    if kind == "image":
        image = models.AppBskyEmbedImages.Image(
            alt="", image=blob, aspect_ratio={"width": width, "height": height})
        embed = models.AppBskyEmbedImages.Main(images=[image])
    else:
        embed = models.AppBskyEmbedVideo.Main(video=blob, alt=None)

    response = client.send_post(text="", embed=embed)
    if not response:
        raise RuntimeError("Bluesky returned no post response; original stays in ready.")
    log(f"Post successful: {getattr(response, 'uri', 'URI unavailable')}")

    move_file(drive, item, ready_id, folders["used"]["id"])
    log("Original file moved from sapphicheaven/ready to sapphicheaven/used.")
    log("NEW POST COMPLETE")


if __name__ == "__main__":
    main()
