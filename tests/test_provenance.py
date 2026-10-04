"""An invitation from outside the organisation, from someone never written to, is flagged - never dropped."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axiom1 import provenance as P  # noqa: E402

OWNER = "emma.johnson@bluesparrowtech.com"
KNOWN = P.written_to([{"to": ["mark.davies@hotmail.com"], "cc": ["Julie <julie.williams@bluesparrowtech.com>"]}])


class Provenance(unittest.TestCase):
    def test_outside_and_never_written_to_is_flagged(self):
        for s in ["security-facebook-com@gmail.com", "Events <info@best-event-organizers.com>"]:
            self.assertIn("outside your organisation", P.outside_flag(s, OWNER, KNOWN))

    def test_a_colleague_is_never_flagged_even_on_a_subdomain(self):
        self.assertIsNone(P.outside_flag("david.smith@bluesparrowtech.com", OWNER, KNOWN))
        self.assertIsNone(P.outside_flag("ops@mail.bluesparrowtech.com", OWNER, KNOWN))

    def test_an_outside_contact_i_write_to_is_not_flagged(self):
        self.assertIsNone(P.outside_flag("Mark <mark.davies@hotmail.com>", OWNER, KNOWN))

    def test_a_lookalike_domain_is_outside(self):
        self.assertIsNotNone(P.outside_flag("it@bluesparrowtech.com.evil.io", OWNER, KNOWN))
        self.assertIsNotNone(P.outside_flag("it@notbluesparrowtech.com", OWNER, KNOWN))


if __name__ == "__main__":
    unittest.main()
