# Telegram Compose Deployer

Small, dependency-free Python service that listens for Telegram Bot API
messages and updates a live Docker Compose checkout to an approved Git commit.

The deployer is repository-agnostic. Configure the target repository, chat,
branch allowlist, target folder, Compose profile, and message regex through
environment variables. The default matcher accepts notifications containing:

```text
Branch: main
Commit: abc1234 (full-40-character-sha)
Details: https://github.com/owner/repository/commit/full-40-character-sha
```

## Behavior

- Uses Telegram long polling; no public webhook endpoint is required.
- Filters updates by `TELEGRAM_CHAT_ID` and optional `TELEGRAM_TOPIC_ID`.
- Requires `TELEGRAM_MESSAGE_REGEX` or the built-in notification pattern.
- Validates the branch, full SHA, repository, Git origin, and remote ancestry.
- Stashes tracked local changes before switching commits and restores them with
  `git stash pop` after Compose finishes, including after deployment failures.
- Leaves untracked files in place so deployment-local files such as `.env`
  remain available to Compose.
- Runs `docker compose --profile <profile> up --detach --build --remove-orphans`.
- Persists Telegram offsets so already-processed updates are not repeated.
- Uses long polling with a minimum timeout, honors Telegram `retry_after` on
  HTTP/API 429 responses, spaces rapid backlog polls, and backs off failed
  local deployments.
- Logs each successful deployment with the repository, branch, and first 12
  characters of the deployed commit SHA.

The target checkout must be clean of unresolved conflicts and have an
`origin` remote. Do not run multiple workers against the same checkout. If
stash restoration conflicts, the stash remains available for manual recovery.

## Configuration and execution

```bash
cp .env.example .env
# Edit .env, then export the variables or load them with your service manager.
python3 telegram_compose_deployer.py \
  --state-file /var/lib/telegram-compose-deployer/offset.json
```

For a configuration check without changing Git or Docker:

```bash
python3 telegram_compose_deployer.py --once --dry-run
```

## systemd installation

Copy the worker to a stable location outside the target repository, for
example `/opt/telegram-compose-deployer/telegram_compose_deployer.py`, and
create `/etc/telegram-compose-deployer.env` from `.env.example` with mode 600.

Create the service user and shared deployment group:

```bash
getent group docker >/dev/null || sudo groupadd --system docker
sudo groupadd --system compose-deployers
sudo useradd --system --home-dir /opt/telegram-compose-deployer \
  --create-home --shell /usr/sbin/nologin telegram-deployer
sudo usermod --append --groups docker,compose-deployers telegram-deployer
sudo chown -R telegram-deployer:compose-deployers /opt/telegram-compose-deployer
sudo mkdir -p /var/lib/telegram-compose-deployer
sudo chown telegram-deployer:compose-deployers /var/lib/telegram-compose-deployer
sudo chown -R telegram-deployer:compose-deployers /srv/my-compose-project
```

Create `/etc/systemd/system/telegram-compose-deployer.service`:

```ini
[Unit]
Description=Telegram Compose Deployer
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=simple
User=telegram-deployer
Group=docker
WorkingDirectory=/opt/telegram-compose-deployer
EnvironmentFile=/etc/telegram-compose-deployer.env
ExecStart=/usr/bin/python3 /opt/telegram-compose-deployer/telegram_compose_deployer.py --state-file /var/lib/telegram-compose-deployer/offset.json
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then enable it at boot:

```bash
sudo chmod 600 /etc/telegram-compose-deployer.env
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-compose-deployer.service
sudo journalctl -u telegram-compose-deployer.service -f
```

Membership in the Docker group grants root-equivalent access to the host.

## License

This project is licensed under the GNU General Public License version 3 or
later. See [LICENSE](LICENSE) or the [official GPLv3 text](https://www.gnu.org/licenses/gpl-3.0.html).
