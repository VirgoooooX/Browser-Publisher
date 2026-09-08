"""Tests for CLI helper and command parsing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from publisher.cli import normalize_platform


def test_normalize_platform() -> None:
    assert normalize_platform("wechat") == "wechat_mp"
    assert normalize_platform("wechat_mp") == "wechat_mp"
    assert normalize_platform("mp") == "wechat_mp"
    assert normalize_platform("xhs") == "xiaohongshu"
    assert normalize_platform("xiaohongshu") == "xiaohongshu"
    assert normalize_platform("other") == "other"


def test_cli_status_command(capsys: pytest.CaptureFixture[str]) -> None:
    from argparse import Namespace
    from publisher.cli import cmd_status

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "id": "job_12345",
        "platform": "wechat_mp",
        "mode": "publish",
        "status": "published",
        "publish_phase": "publish_clicked",
        "attempt_count": 1,
        "max_attempts": 2,
        "content": {"title": "CLI测试文章"},
        "final_url": "https://mp.weixin.qq.com/s/xyz",
        "created_at": "2026-09-08T10:00:00Z",
    }
    mock_client.get.return_value = mock_resp

    args = Namespace(job_id="job_12345")
    cmd_status(args, mock_client)

    captured = capsys.readouterr().out
    assert "Job ID:          job_12345" in captured
    assert "Status:          published" in captured
    assert "https://mp.weixin.qq.com/s/xyz" in captured


def test_cli_resume_command(capsys: pytest.CaptureFixture[str]) -> None:
    from argparse import Namespace
    from publisher.cli import cmd_resume

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client.post.return_value = mock_resp

    args = Namespace(platform="xhs")
    cmd_resume(args, mock_client)

    captured = capsys.readouterr().out
    assert "Platform xiaohongshu risk pause successfully resumed" in captured

