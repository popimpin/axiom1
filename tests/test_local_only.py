"""AXIOM_LOCAL_ONLY: a model endpoint off this machine / the private network is refused - mail never leaves."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axiom1 import agent  # noqa: E402


class LocalOnly(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"AXIOM_LOCAL_ONLY": "1", "NEBIUS_API_KEY": "x"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_local_and_private_endpoints_are_allowed(self):
        for url in ["http://127.0.0.1:11434/v1", "http://localhost:8000/v1", "http://192.168.1.20:11434/v1",
                    "http://10.0.0.5/v1", "http://100.100.157.46:11435/v1"]:        # the last: Bee, over Tailscale
            agent.ChatModel(model="m", base_url=url)

    def test_a_public_endpoint_is_refused_at_start(self):
        for url in ["http://8.8.8.8/v1", "https://1.1.1.1/v1"]:
            with self.assertRaises(SystemExit) as e:
                agent.ChatModel(model="m", base_url=url)
            self.assertIn("would leave the building", str(e.exception))

    def test_refused_on_every_call_if_the_host_moves(self):
        m = agent.ChatModel(model="m", base_url="http://127.0.0.1:9/v1")
        with mock.patch.object(agent, "endpoint_is_local", return_value=False):
            with self.assertRaises(SystemExit):
                m([{"role": "user", "content": "hi"}], [])

    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {"AXIOM_LOCAL_ONLY": ""}):
            agent.ChatModel(model="m", base_url="http://8.8.8.8/v1")


if __name__ == "__main__":
    unittest.main()
