#!/usr/bin/env python3
"""Deploy a Compose project when an approved Telegram commit notification arrives.

The process uses the Telegram Bot API long-polling endpoint.  It intentionally
does not use Telegram webhooks, so it can run as a small process on the same
machine as Docker Compose without exposing an HTTP port.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("telegram-compose-deployer")
DEFAULT_MESSAGE_REGEX = r"(?ims)^New commit on .+?^Bot:\s*@?\S+.+?^Branch:\s*\S+.+?^Commit:\s*[0-9a-f]{7,40}\s*\([0-9a-f]{40}\).+?^Details:\s*https?://\S+"
FIELD_PATTERNS = {
    "branch": re.compile(r"(?im)^Branch:\s*(?P<value>[^\r\n]+)\s*$"),
    "commit": re.compile(r"(?im)^Commit:\s*(?P<short>[0-9a-f]{7,40})\s*\((?P<full>[0-9a-f]{40})\)\s*$"),
    "details": re.compile(r"(?im)^Details:\s*(?P<value>https?://[^\s]+)\s*$"),
}


class TelegramRateLimitError(RuntimeError):
    """Telegram returned HTTP/API error 429 and supplied a retry delay."""

    def __init__(self, retry_after: int, description: str) -> None:
        super().__init__(description)
        self.retry_after = max(1, retry_after)


@dataclass(frozen=True)
class DeploymentMessage:
    branch: str
    commit: str
    repository: str
    details_url: str


def parse_deployment_message(text: str, message_regex: str) -> DeploymentMessage | None:
    """Validate and parse a bot message, returning None for unrelated text."""
    try:
        matches = re.search(message_regex, text)
    except re.error as exc:
        raise ValueError(f"Invalid TELEGRAM_MESSAGE_REGEX: {exc}") from exc
    if not matches:
        return None

    fields = {name: pattern.search(text) for name, pattern in FIELD_PATTERNS.items()}
    if any(match is None for match in fields.values()):
        return None

    branch = fields["branch"].group("value").strip()
    commit_match = fields["commit"]
    commit = commit_match.group("full").lower()
    if not commit.startswith(commit_match.group("short").lower()):
        raise ValueError("Commit short ID does not match the full commit ID")

    details_url = fields["details"].group("value").rstrip(".,)")
    path_parts = [part for part in urlparse(details_url).path.split("/") if part]
    if len(path_parts) < 4 or path_parts[-2] != "commit":
        raise ValueError("Details URL must contain /<owner>/<repo>/commit/<sha>")
    repository = f"{path_parts[-4]}/{path_parts[-3]}".removesuffix(".git")
    return DeploymentMessage(branch, commit, repository, details_url)


def run(command: list[str], cwd: Path) -> None:
    LOGGER.info("Running: %s", shlex.join(command))
    subprocess.run(command, cwd=cwd, check=True)


def output(command: list[str], cwd: Path) -> str:
    return subprocess.check_output(command, cwd=cwd, text=True, stderr=subprocess.STDOUT).strip()


def repository_from_remote(remote_url: str) -> str:
    """Extract owner/repository from HTTPS, SSH, or scp-style Git remotes."""
    value = remote_url.strip().removesuffix(".git")
    if ":" in value and not value.startswith(("http://", "https://", "ssh://")):
        value = value.rsplit(":", 1)[-1]
    path_parts = [part for part in urlparse(value).path.split("/") if part]
    return "/".join(path_parts[-2:]) if len(path_parts) >= 2 else ""


def stash_local_changes(target: Path) -> bool:
    """Stash tracked changes, leaving untracked deployment files such as .env in place."""
    has_worktree_changes = subprocess.run(["git", "diff", "--quiet"], cwd=target).returncode != 0
    has_index_changes = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=target).returncode != 0
    if not (has_worktree_changes or has_index_changes):
        return False
    run(["git", "stash", "push", "--message", "telegram-compose-deployer"], target)
    LOGGER.info("Stashed local tracked changes before deployment")
    return True


def restore_local_changes(target: Path) -> None:
    """Restore the stash created by stash_local_changes, raising on conflicts."""
    run(["git", "stash", "pop"], target)
    LOGGER.info("Restored local tracked changes after deployment")


def deploy(message: DeploymentMessage, config: dict[str, str], dry_run: bool = False) -> None:
    """Update the target checkout to the exact commit and recreate Compose services."""
    target = Path(config["target_folder"]).expanduser().resolve()
    if not target.is_dir() or not (target / ".git").exists():
        raise RuntimeError(f"TARGET_FOLDER is not a Git repository: {target}")

    allowed_repository = config["repository"].lower()
    if message.repository.lower() != allowed_repository:
        raise RuntimeError(f"Repository {message.repository!r} is not allowed")

    allowed_branches = {item.strip() for item in config.get("branches", "").split(",") if item.strip()}
    if allowed_branches and message.branch not in allowed_branches:
        raise RuntimeError(f"Branch {message.branch!r} is not allowed")
    if not re.fullmatch(r"[0-9a-f]{40}", message.commit):
        raise RuntimeError("Commit ID must be a 40-character hexadecimal SHA")

    remote_url = output(["git", "remote", "get-url", "origin"], target)
    remote_repository = repository_from_remote(remote_url)
    if remote_repository.lower() != allowed_repository:
        raise RuntimeError(f"Git origin {remote_url!r} does not match TELEGRAM_REPOSITORY")
    compose_profiles = [item.strip() for item in config.get("compose_profiles", "production").split(",") if item.strip()]
    compose_file = config.get("compose_file", "").strip()
    compose_services = shlex.split(config.get("compose_services", ""))
    compose = ["docker", "compose"]
    for profile in compose_profiles:
        compose.extend(["--profile", profile])
    if compose_file:
        compose.extend(["-f", compose_file])
    compose.extend(["up", "--detach", "--build", "--remove-orphans", *compose_services])

    if dry_run:
        LOGGER.info("Dry run: would fetch %s, switch to %s, reset to %s, and run %s", message.branch, message.branch, message.commit, shlex.join(compose))
        return

    stash_created = stash_local_changes(target)
    try:
        run(["git", "fetch", "--prune", "origin", message.branch], target)
        run(["git", "cat-file", "-e", f"{message.commit}^{{commit}}"], target)
        run(["git", "merge-base", "--is-ancestor", message.commit, f"origin/{message.branch}"], target)
        local_branch_exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{message.branch}"], cwd=target
        ).returncode == 0
        if local_branch_exists:
            run(["git", "switch", message.branch], target)
        else:
            run(["git", "switch", "--track", "-c", message.branch, f"origin/{message.branch}"], target)
        run(["git", "reset", "--hard", message.commit], target)
        run(compose, target)
    finally:
        if stash_created:
            restore_local_changes(target)


def telegram_request(token: str, method: str, params: dict[str, object]) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(params).encode("utf-8")
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=float(os.getenv("TELEGRAM_HTTP_TIMEOUT", "40"))) as response:
            result = json.load(response)
    except HTTPError as exc:
        try:
            result = json.load(exc)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RuntimeError(f"Telegram HTTP error {exc.code}") from exc
        if exc.code == 429 or result.get("error_code") == 429:
            parameters = result.get("parameters") or {}
            raise TelegramRateLimitError(
                int(parameters.get("retry_after", 60)), result.get("description", "Telegram rate limit exceeded")
            ) from exc
        raise RuntimeError(f"Telegram HTTP error {exc.code}: {result}") from exc
    if not result.get("ok"):
        if result.get("error_code") == 429:
            parameters = result.get("parameters") or {}
            raise TelegramRateLimitError(
                int(parameters.get("retry_after", 60)), result.get("description", "Telegram rate limit exceeded")
            )
        raise RuntimeError(f"Telegram API error: {result}")
    return result


def process_update(update: dict, config: dict[str, str], dry_run: bool) -> None:
    message = update.get("message") or update.get("channel_post")
    if not message or str(message.get("chat", {}).get("id")) != config["chat_id"]:
        return
    topic_id = config.get("topic_id", "").strip()
    if topic_id and str(message.get("message_thread_id")) != topic_id:
        return
    text = message.get("text") or message.get("caption") or ""
    parsed = parse_deployment_message(text, config["message_regex"])
    if parsed is None:
        LOGGER.info("Ignored update %s: message did not match the configured regex", update.get("update_id"))
        return
    LOGGER.info("Accepted update %s for %s@%s", update.get("update_id"), parsed.repository, parsed.commit)
    deploy(parsed, config, dry_run=dry_run)


def load_config() -> dict[str, str]:
    required = {name: os.getenv(name, "").strip() for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TARGET_FOLDER", "TELEGRAM_REPOSITORY")}
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    return {
        **required,
        "message_regex": os.getenv("TELEGRAM_MESSAGE_REGEX", DEFAULT_MESSAGE_REGEX),
        "branches": os.getenv("TELEGRAM_ALLOWED_BRANCHES", ""),
        "topic_id": os.getenv("TELEGRAM_TOPIC_ID", ""),
        "compose_profiles": os.getenv("COMPOSE_PROFILES", "production"),
        "compose_file": os.getenv("COMPOSE_FILE", ""),
        "compose_services": os.getenv("COMPOSE_SERVICES", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process one Telegram poll and exit")
    parser.add_argument("--dry-run", action="store_true", help="Validate and log deployment commands without changing Git or Docker")
    parser.add_argument("--state-file", type=Path, default=Path("telegram-deployer-offset.json"), help="File used to persist the Telegram update offset")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config()
        offset = int(json.loads(args.state_file.read_text(encoding="utf-8"))) if args.state_file.exists() else None
        last_poll_at: float | None = None
        while True:
            minimum_poll_interval = max(0.0, float(os.getenv("TELEGRAM_MIN_POLL_INTERVAL_SECONDS", "0.5")))
            if last_poll_at is not None:
                time.sleep(max(0.0, minimum_poll_interval - (time.monotonic() - last_poll_at)))
            poll_timeout = max(1, int(os.getenv("TELEGRAM_POLL_TIMEOUT", "30")))
            params: dict[str, object] = {"timeout": poll_timeout, "allowed_updates": ["message", "channel_post"]}
            if offset is not None:
                params["offset"] = offset
            try:
                last_poll_at = time.monotonic()
                updates = telegram_request(config["TELEGRAM_BOT_TOKEN"], "getUpdates", params).get("result", [])
            except TelegramRateLimitError as exc:
                LOGGER.error("Telegram rate limit reached; retrying in %ss: %s", exc.retry_after, exc)
                if args.once:
                    return 1
                time.sleep(exc.retry_after)
                continue
            except (HTTPError, URLError, TimeoutError, RuntimeError) as exc:
                LOGGER.error("Telegram poll failed: %s", exc)
                if args.once:
                    return 1
                time.sleep(5)
                continue
            failed_update = False
            for update in updates:
                try:
                    process_update(update, config, args.dry_run)
                except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError) as exc:
                    LOGGER.error("Update %s failed and will be retried: %s", update.get("update_id"), exc)
                    failed_update = True
                    break
                offset = int(update["update_id"]) + 1
                args.state_file.write_text(json.dumps(offset), encoding="utf-8")
            if args.once:
                return 1 if failed_update else 0
            if failed_update:
                time.sleep(min(60, max(5, int(os.getenv("TELEGRAM_FAILURE_RETRY_SECONDS", "30")))))
    except (ValueError, OSError) as exc:
        LOGGER.error("Configuration failed: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
