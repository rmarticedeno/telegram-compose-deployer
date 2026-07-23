import unittest
import os
from pathlib import Path
from unittest.mock import patch

from telegram_compose_deployer import load_config, parse_deploy_command, parse_deployment_message, stash_local_changes


MESSAGE = """New commit on main

Bot: @example_deploy_bot
Branch: main
Subject: ci: include branch in deployment message
Commit: 0123456 (0123456789abcdef0123456789abcdef01234567)
Author: Example User <deploy@example.invalid>
Date: 2026-01-01T00:00:00Z
Details: https://github.com/example-org/sample-dashboard/commit/0123456789abcdef0123456789abcdef01234567"""


class ParseDeploymentMessageTests(unittest.TestCase):
    def test_deploy_command_defaults_to_main(self):
        self.assertEqual(parse_deploy_command("/deploy", "main"), "main")
        self.assertEqual(parse_deploy_command("/deploy@example_deploy_bot", "main"), "main")
        self.assertEqual(parse_deploy_command("/deploy release/2026", "main"), "release/2026")
        self.assertIsNone(parse_deploy_command("deploy main", "main"))

    @patch.dict(
        os.environ,
        {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "-100123",
            "TELEGRAM_TOPIC_ID": "456",
            "TARGET_FOLDER": "/srv/example",
            "TELEGRAM_REPOSITORY": "example-org/sample-dashboard",
        },
        clear=False,
    )
    def test_normalizes_environment_keys_for_runtime(self):
        config = load_config()
        self.assertEqual(config["chat_id"], "-100123")
        self.assertEqual(config["topic_id"], "456")
        self.assertEqual(config["telegram_bot_token"], "test-token")
        self.assertEqual(config["target_folder"], "/srv/example")
        self.assertEqual(config["repository"], "example-org/sample-dashboard")

    def test_parses_example(self):
        parsed = parse_deployment_message(MESSAGE, r"(?s)^New commit on")
        self.assertEqual(parsed.branch, "main")
        self.assertEqual(parsed.commit, "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(parsed.repository, "example-org/sample-dashboard")

    def test_rejects_message_without_regex_match(self):
        self.assertIsNone(parse_deployment_message(MESSAGE, r"^Release completed"))

    def test_rejects_mismatched_short_commit(self):
        invalid = MESSAGE.replace("0123456 (", "deadbee (")
        with self.assertRaises(ValueError):
            parse_deployment_message(invalid, r"(?s)^New commit on")

    @patch("telegram_compose_deployer.run")
    @patch("telegram_compose_deployer.subprocess.run")
    def test_stashes_tracked_changes_but_not_untracked_only(self, subprocess_run, run_command):
        subprocess_run.side_effect = [unittest.mock.Mock(returncode=1), unittest.mock.Mock(returncode=0)]
        self.assertTrue(stash_local_changes(Path("/target")))
        run_command.assert_called_once_with(
            ["git", "stash", "push", "--message", "telegram-compose-deployer"], Path("/target")
        )

        subprocess_run.side_effect = [unittest.mock.Mock(returncode=0), unittest.mock.Mock(returncode=0)]
        run_command.reset_mock()
        self.assertFalse(stash_local_changes(Path("/target")))
        run_command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
