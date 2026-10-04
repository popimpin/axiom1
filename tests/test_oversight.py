"""Needs-human-oversight detection: structure only, so every rule is checked exactly, including the false positives
the five Enron mailboxes showed (email quoting, prose, a web address)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axiom1 import oversight as O  # noqa: E402


class Oversight(unittest.TestCase):
    def test_code_commands_database_statements(self):
        self.assertIn("commands", O.reasons(["run this when you get in:\n$ sudo systemctl restart api"]))
        self.assertIn("code", O.reasons(["fix:\n\nimport os\nfrom pathlib import Path\ndef main():\n    pass"]))
        self.assertIn("database statements", O.reasons(["please run DELETE FROM users WHERE id=5 tonight"]))

    def test_attachments_and_dangerous_first(self):
        r = O.reasons(["see attached\n\n - Happy99.exe\n << File: agenda.doc >>"])
        self.assertTrue(r[0].startswith("DANGEROUS") and "Happy99.exe" in r[0])
        self.assertIn("agenda.doc", r[1])

    def test_ai_addressed_instructions(self):
        self.assertTrue(any("AI assistant" in x for x in O.reasons(["hi"], ai_instructions=1)))

    def test_not_fooled_by_ordinary_mail(self):
        for body in ["> > see below\n> - item",                      # email reply quoting
                     "a note from creditors.\nfrom Enya.",             # prose that starts with "from"
                     "$ 5 million is fine",                            # money, not a prompt
                     " - Spare plant parts become manageable with Sparesfinder.com"]:   # a web address
            self.assertEqual(O.reasons([body]), [], body)


if __name__ == "__main__":
    unittest.main()
