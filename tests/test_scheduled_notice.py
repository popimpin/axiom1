"""A scheduled call IS the meeting: a notice that asks nothing is not "invited, no reply" (blind audit, 2026-10-05)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))
import real_inbox as RI  # noqa: E402


def notice(body):
    return RI.scheduled_notice([{"body": body}])


class ScheduledNotice(unittest.TestCase):
    def test_notice_with_dial_in_asking_nothing_is_settled(self):
        # the shape of the audit's two calendar examples (heard-m t00804, steffes-j t00944)
        self.assertTrue(notice("A meeting has been scheduled for Wednesday, November 28, 2001 at 10:00. "
                               "Dial-in: 800-555-0100, passcode 4471."))
        self.assertTrue(notice("The conference call is scheduled for Monday at 11 am. Call-in number 888-555-0199."))

    def test_rsvp_form_without_a_question_mark_still_asks(self):
        # lay-k t01253: no "?" anywhere, but it asks for a reply
        self.assertFalse(notice("Management Committee Meeting Date: Monday, October 1 Time: 11:00 a.m. Please contact "
                                "me for dial-in number and passcode. Please indicate if you plan to attend this meeting. "
                                "Please return your response via e-mail by Friday, September 28."))

    def test_questions_stay_follow_ups(self):
        self.assertFalse(notice("The call has been scheduled for Tuesday at 3pm - can you make it?"))
        self.assertFalse(notice("Meeting is scheduled for Friday at 9:00. Please confirm you can attend."))

    def test_generated_invitations_are_untouched(self):
        # the generated inbox's unanswered invitations all ask; none is a notice
        for body in ("Would tomorrow at 3:30pm work for design review? About an hour.",
                     "Hi all - planning sync next Tuesday at noon? 45 minutes.",
                     "Join our free webinar Thursday at 2pm! Spaces are limited.\n\nUnsubscribe | View in browser"):
            self.assertFalse(notice(body), body)

    def test_an_offer_to_go_in_my_place_is_not_settled(self):
        # steffes-j t00315: the meeting is scheduled, but the sender offers to go "if you wish" and waits on an answer
        body = ("At yesterday's meeting stakeholders were heard. A meeting is scheduled for August 21st at 10am in "
                "Cincinnati. I will attend the meeting if you wish.")
        self.assertFalse(notice(body))
        self.assertTrue(RI.offers([{"body": body}]))
        self.assertFalse(RI.offers([{"body": "A meeting has been scheduled for Monday at 9:00. Dial-in 800-555-0100."}]))

    def test_a_notice_needs_a_day_or_time(self):
        self.assertFalse(notice("The board meeting has been scheduled. Details to follow."))


if __name__ == "__main__":
    unittest.main()
