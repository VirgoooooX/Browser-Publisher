"""Command-line interface for Browser Publisher (publisher-cli)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx


def _get_api_client(base_url: str, token: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "publisher-cli/0.2.0",
        },
        timeout=30.0,
    )


def normalize_platform(name: str) -> str:
    name = name.lower()
    if name in ("wechat", "mp", "wechat_mp", "wx"):
        return "wechat_mp"
    if name in ("xhs", "xiaohongshu", "red"):
        return "xiaohongshu"
    return name


def cmd_login(args: argparse.Namespace, client: httpx.Client) -> None:
    platform = normalize_platform(args.platform)
    print(f"[*] Requesting login for platform: {platform}...")
    resp = client.post(f"/v1/platforms/{platform}/login")
    if resp.status_code != 200:
        print(f"[!] Error ({resp.status_code}): {resp.text}")
        sys.exit(1)

    data = resp.json()
    status = data.get("status")
    print(f"[+] Status: {status}")
    if status == "already_authenticated":
        print("[+] Session is already authenticated and ready.")
        return

    qr_url = f"{client.base_url}/v1/platforms/{platform}/qr.png"
    print(f"[+] QR code available at: {qr_url}")
    if args.save_qr:
        qr_resp = client.get(f"/v1/platforms/{platform}/qr.png")
        if qr_resp.status_code == 200:
            out_file = Path(args.save_qr)
            out_file.write_bytes(qr_resp.content)
            print(f"[+] QR image saved to: {out_file.resolve()}")
        else:
            print("[!] Could not download QR image bytes.")


def cmd_publish(args: argparse.Namespace, client: httpx.Client) -> None:
    platform = normalize_platform(args.platform)
    file_path = Path(args.file)
    if not file_path.is_file():
        print(f"[!] File not found: {file_path}")
        sys.exit(1)

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[!] Failed to parse JSON: {exc}")
        sys.exit(1)

    # Automatically upload local files referenced in media
    media_items = data.get("media", [])
    processed_media = []
    for item in media_items:
        if isinstance(item, str):
            p = Path(item)
            if p.is_file():
                # Upload local file
                print(f"[*] Uploading local media: {p.name}...")
                with open(p, "rb") as f:
                    up_res = client.post(
                        "/v1/media",
                        files={"file": (p.name, f, "image/jpeg")},
                    )
                if up_res.status_code != 201:
                    print(f"[!] Failed to upload {p}: {up_res.text}")
                    sys.exit(1)
                med_id = up_res.json()["media_id"]
                processed_media.append({"kind": "uploaded", "media_id": med_id})
            else:
                # URL
                processed_media.append({"kind": "url", "url": item})
        elif isinstance(item, dict):
            processed_media.append(item)

    client_req_id = (
        data.get("client_request_id")
        or f"cli-{file_path.stem}-{int(Path(args.file).stat().st_mtime)}"
    )
    payload: dict[str, Any] = {
        "client_request_id": client_req_id,
        "platform": platform,
        "mode": args.mode or data.get("mode"),
        "content": data.get("content", data),
        "media": processed_media,
        "source_url": data.get("source_url"),
        "topics": data.get("topics", []),
    }

    print(
        f"[*] Submitting publish job to {platform} (mode: {payload['mode'] or 'default'})..."
    )
    resp = client.post("/v1/jobs", json=payload)
    if resp.status_code not in (200, 202):
        print(f"[!] Submit error ({resp.status_code}): {resp.text}")
        sys.exit(1)

    res_data = resp.json()
    print(
        f"[+] Job accepted! ID: {res_data['id']}, Status: {res_data['status']}, Effective Mode: {res_data['effective_mode']}"
    )


def cmd_status(args: argparse.Namespace, client: httpx.Client) -> None:
    resp = client.get(f"/v1/jobs/{args.job_id}")
    if resp.status_code != 200:
        print(f"[!] Error ({resp.status_code}): {resp.text}")
        sys.exit(1)

    job = resp.json()
    print("=" * 50)
    print(f"Job ID:          {job['id']}")
    print(f"Platform:        {job['platform']}")
    print(f"Mode:            {job['mode']}")
    print(f"Status:          {job['status']}")
    print(f"Phase:           {job.get('publish_phase') or '-'}")
    print(f"Attempts:        {job['attempt_count']} / {job['max_attempts']}")
    print(f"Title:           {job['content'].get('title', '')}")
    if job.get("final_url"):
        print(f"Final URL:       {job['final_url']}")
    if job.get("error_code"):
        print(f"Error Code:      {job['error_code']}")
        print(f"Error Summary:   {job.get('error_summary')}")
    print(f"Created At:      {job['created_at']}")
    print("=" * 50)


def cmd_resume(args: argparse.Namespace, client: httpx.Client) -> None:
    platform = normalize_platform(args.platform)
    print(f"[*] Resuming platform: {platform}...")
    resp = client.post(f"/v1/platforms/{platform}/resume")
    if resp.status_code != 200:
        print(f"[!] Error ({resp.status_code}): {resp.text}")
        sys.exit(1)
    print(f"[+] Platform {platform} risk pause successfully resumed.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="publisher-cli",
        description="Browser Publisher command-line interface",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("PUBLISHER_API_URL", "http://localhost:8790"),
        help="Base URL of Browser Publisher service",
    )
    parser.add_argument(
        "--token",
        default=os.getenv(
            "PUBLISHER_ACCESS_TOKEN", "dev-local-insecure-token-1234567890"
        ),
        help="Publisher access token",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # login
    p_login = subparsers.add_parser("login", help="Request platform QR login")
    p_login.add_argument("platform", help="wechat or xhs")
    p_login.add_argument("--save-qr", help="Path to save QR image to")

    # publish
    p_pub = subparsers.add_parser(
        "publish", help="Submit article/note JSON for publishing"
    )
    p_pub.add_argument("platform", help="wechat or xhs")
    p_pub.add_argument("file", help="Path to article JSON file")
    p_pub.add_argument(
        "--mode", choices=["draft", "publish"], help="Override publish mode"
    )

    # status
    p_stat = subparsers.add_parser("status", help="Get publish job status")
    p_stat.add_argument("job_id", help="Job ID (e.g. job_xxx)")

    # resume
    p_res = subparsers.add_parser(
        "resume", help="Resume platform paused due to risk control"
    )
    p_res.add_argument("platform", help="wechat or xhs")

    args = parser.parse_args()
    client = _get_api_client(args.url, args.token)

    if args.command == "login":
        cmd_login(args, client)
    elif args.command == "publish":
        cmd_publish(args, client)
    elif args.command == "status":
        cmd_status(args, client)
    elif args.command == "resume":
        cmd_resume(args, client)


if __name__ == "__main__":
    main()
