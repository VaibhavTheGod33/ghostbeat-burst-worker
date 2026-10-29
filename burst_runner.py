#!/usr/bin/env python3
"""
GhostBeat Cloud Burst Worker — Ephemeral 7GB Studio Audio Engine.
Executes on GitHub Actions ubuntu-latest runners for heavy multi-track mixing.
Zero persistent disk footprint. Closes and unlinks all assets on job termination.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles
import aiohttp
from motor.motor_asyncio import AsyncIOMotorClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("GhostBeatBurst")

# Configuration from runner environment & secrets
JOB_ID = os.getenv("JOB_ID", "").strip()
USER_ID = os.getenv("USER_ID", "").strip()
FEATURE_KEY = os.getenv("FEATURE_KEY", "dj_mix_studio").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "").strip()
DB_NAME = os.getenv("DB_NAME", "ghostbeat_db").strip()

DJ_MIX_STYLES = ["Smooth Blend", "Festival Drop", "Phonk Chaos", "Night Drive", "Club DJ"]
DJ_MIX_ENERGIES = ["Chill", "Balanced", "Aggressive"]


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    total = max(0, int(round(seconds)))
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


async def download_file_from_telegram(
    session: aiohttp.ClientSession,
    bot_token: str,
    file_id: str,
    destination: Path,
) -> Path:
    """Download audio file directly via Telegram Bot API."""
    info_url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
    async with session.get(info_url) as resp:
        if resp.status != 200:
            raise RuntimeError(f"getFile returned HTTP {resp.status}")
        data = await resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram getFile error: {data.get('description')}")
        file_path = data["result"]["file_path"]

    download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
    async with session.get(download_url) as resp:
        if resp.status != 200:
            raise RuntimeError(f"File download failed HTTP {resp.status}")
        async with aiofiles.open(destination, "wb") as f:
            while True:
                chunk = await resp.content.read(524288)
                if not chunk:
                    break
                await f.write(chunk)

    if not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError("Downloaded file is empty (0 bytes).")
    return destination


async def send_audio_to_telegram(
    session: aiohttp.ClientSession,
    bot_token: str,
    chat_id: int,
    audio_path: Path,
    caption: str,
    title: str,
    performer: str = "GhostBeat Studio Engine",
) -> None:
    """Upload master MP3 back to the user via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{bot_token}/sendAudio"
    data = aiohttp.FormData()
    data.add_field("chat_id", str(chat_id))
    data.add_field("caption", caption)
    data.add_field("parse_mode", "HTML")
    data.add_field("title", title)
    data.add_field("performer", performer)
    data.add_field(
        "audio",
        open(audio_path, "rb"),
        filename=audio_path.name,
        content_type="audio/mpeg",
    )

    async with session.post(url, data=data) as resp:
        if resp.status != 200:
            err = await resp.text()
            raise RuntimeError(f"Telegram sendAudio failed HTTP {resp.status}: {err}")
        logger.info("Successfully delivered audio to user %s", chat_id)


async def render_dj_mix(
    track_paths: list[Path],
    output_path: Path,
    style_idx: int = 1,
    energy_idx: int = 1,
) -> Path:
    """Multi-track seamless DJ mixing engine with equal-power crossfades."""
    if not track_paths:
        raise ValueError("No tracks provided for mix")

    base_xfade = [10.0, 5.0, 4.0, 12.0, 7.0][max(0, min(style_idx, 4))]
    energy_mult = [1.2, 1.0, 0.8][max(0, min(energy_idx, 2))]
    xfade_sec = round(max(2.5, base_xfade * energy_mult), 1)

    cmd = ["ffmpeg", "-y"]
    for p in track_paths:
        cmd += ["-i", str(p)]

    filter_parts = []
    # 1. Headroom pre-scaling
    for i in range(len(track_paths)):
        filter_parts.append(f"[{i}:a]volume=0.82[in{i}]")

    # 2. Equal-power quarter-sine crossfades
    last_lbl = "in0"
    for i in range(1, len(track_paths)):
        next_lbl = f"xf{i}" if i < len(track_paths) - 1 else "xf_pre"
        filter_parts.append(
            f"[{last_lbl}][in{i}]acrossfade=d={xfade_sec}:c1=qsin:c2=qsin[{next_lbl}]"
        )
        last_lbl = next_lbl

    # 3. Analog tanh waveshaping + true-peak limiter
    filter_parts.append(
        f"[{last_lbl}]asoftclip=type=tanh:param=0.88:threshold=0.85,alimiter=limit=-0.8dB:attack=3:release=45:asc=1[aout]"
    )
    filter_complex = ";".join(filter_parts)

    cmd += [
        "-vn", "-sn", "-dn",
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        "-c:a", "libmp3lame",
        "-b:a", "320k",
        "-ar", "48000",
        "-metadata", "artist=GhostBeat Cloud Studio",
        "-metadata", "title=GhostBeat High-Power Master",
        str(output_path),
    ]

    logger.info("Executing FFmpeg multi-track mix (xfade=%ss)...", xfade_sec)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg failed (code {proc.returncode}): {stderr.decode('utf-8', errors='ignore')[-400:]}")

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("Rendered output file is empty.")
    return output_path


async def main() -> None:
    if not JOB_ID:
        logger.error("JOB_ID environment variable is missing.")
        sys.exit(1)
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN secret is missing.")
        sys.exit(1)
    if not MONGO_URI:
        logger.error("MONGO_URI secret is missing.")
        sys.exit(1)

    logger.info("⚡ GhostBeat Cloud Burst Worker starting for Job %s...", JOB_ID)
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    job = await db.audio_jobs.find_one({"job_id": JOB_ID})
    if not job:
        logger.error("Job %s not found in MongoDB.", JOB_ID)
        sys.exit(1)

    user_id = int(job.get("user_id") or USER_ID or 0)
    now = datetime.now(timezone.utc)
    await db.audio_jobs.update_one(
        {"job_id": JOB_ID},
        {
            "$set": {
                "status": "processing",
                "started_at": now,
                "updated_at": now,
                "worker_id": "github_actions_runner_7gb",
            }
        },
    )

    work_dir = Path(tempfile.mkdtemp(prefix="ghostbeat_burst_"))
    try:
        timeout = aiohttp.ClientTimeout(total=300.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            raw_tracks = job.get("tracks") or []
            if len(raw_tracks) < 2:
                raise ValueError("Job has fewer than 2 tracks.")

            local_tracks: list[Path] = []
            logger.info("Downloading %d tracks from Telegram...", len(raw_tracks))
            for i, t in enumerate(raw_tracks, 1):
                file_id = t.get("file_id")
                if not file_id:
                    raise ValueError(f"Track {i} is missing file_id.")
                orig_name = t.get("original_name") or f"track_{i}.mp3"
                suffix = Path(orig_name).suffix.lower()
                if suffix not in (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"):
                    suffix = ".mp3"
                dest_path = work_dir / f"track_{i}{suffix}"
                await download_file_from_telegram(session, BOT_TOKEN, file_id, dest_path)
                local_tracks.append(dest_path)

            params = job.get("params") or {}
            style_idx = int(params.get("style_index", 1))
            energy_idx = int(params.get("energy_index", 1))

            master_output = work_dir / "ghostbeat_master_set.mp3"
            await render_dj_mix(
                track_paths=local_tracks,
                output_path=master_output,
                style_idx=style_idx,
                energy_idx=energy_idx,
            )

            style_name = DJ_MIX_STYLES[max(0, min(style_idx, len(DJ_MIX_STYLES) - 1))]
            energy_name = DJ_MIX_ENERGIES[max(0, min(energy_idx, len(DJ_MIX_ENERGIES) - 1))]
            total_sec = float(job.get("track_total_seconds") or 0)

            caption = (
                f"🎚 <b>DJ Mix Studio Master Set</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ <b>Engine:</b> High-Power Cloud Core (7GB RAM)\n"
                f"Tracks: <b>{len(local_tracks)}</b> | Total Duration: <b>{format_duration(total_sec)}</b>\n"
                f"Style: <b>{style_name}</b> | Energy: <b>{energy_name}</b>\n"
                f"<i>Seamless equal-power transitions · Zero clipping</i>"
            )

            await send_audio_to_telegram(
                session=session,
                bot_token=BOT_TOKEN,
                chat_id=user_id,
                audio_path=master_output,
                caption=caption,
                title=f"GhostBeat Set ({style_name})",
            )

            finish_time = datetime.now(timezone.utc)
            await db.audio_jobs.update_one(
                {"job_id": JOB_ID},
                {
                    "$set": {
                        "status": "done",
                        "finished_at": finish_time,
                        "updated_at": finish_time,
                        "render_engine": "github_actions_7gb",
                    }
                },
            )
            logger.info("Job %s completed and closed in MongoDB.", JOB_ID)

    except Exception as exc:
        logger.exception("Burst processing failed for job %s: %s", JOB_ID, exc)
        fail_time = datetime.now(timezone.utc)
        await db.audio_jobs.update_one(
            {"job_id": JOB_ID},
            {
                "$set": {
                    "status": "failed",
                    "error": str(exc),
                    "finished_at": fail_time,
                    "updated_at": fail_time,
                }
            },
        )
        sys.exit(1)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        client.close()
        logger.info("Cleaned up temp directory and closed database connection.")


if __name__ == "__main__":
    asyncio.run(main())
