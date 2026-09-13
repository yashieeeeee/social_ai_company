"""Platform ground-truth checks: each hidden rule is verified in isolation."""
import unittest

from prodigal_social.platform import MockPlatform, _apply_hidden_rules


class TestHiddenRules(unittest.TestCase):
    def test_r1_prime_time(self):
        eve = _apply_hidden_rules({"channel": "buzz", "slot": "19:00", "hashtags": ["a"],
                                   "copy": "short post here", "word_count": 10}, [])
        night = _apply_hidden_rules({"channel": "buzz", "slot": "03:00", "hashtags": ["a"],
                                     "copy": "short post here", "word_count": 10}, [])
        self.assertGreater(eve["time_mult"], night["time_mult"] * 2)

    def test_r2_question_boost(self):
        q = _apply_hidden_rules({"channel": "forum", "slot": "12:00", "hashtags": ["a"],
                                 "copy": "Do you brew at home?", "word_count": 10,
                                 "ends_with_question": True}, [])
        s = _apply_hidden_rules({"channel": "forum", "slot": "12:00", "hashtags": ["a"],
                                 "copy": "I brew at home.", "word_count": 10,
                                 "ends_with_question": False}, [])
        self.assertGreater(q["q_mult"], s["q_mult"] * 2)

    def test_r3_hashtag_curve(self):
        def m(n):
            return _apply_hidden_rules({"channel": "buzz", "slot": "12:00",
                                        "hashtags": ["x"] * n, "copy": "hi there",
                                        "word_count": 5}, [])["tag_mult"]
        self.assertEqual((m(0) < m(2), m(2) > m(4), m(3) > m(4)), (True, True, True))

    def test_r4_length_penalty(self):
        long_buzz = _apply_hidden_rules({"channel": "buzz", "slot": "12:00", "hashtags": ["a"],
                                         "copy": "w " * 120, "word_count": 120}, [])
        short_buzz = _apply_hidden_rules({"channel": "buzz", "slot": "12:00", "hashtags": ["a"],
                                          "copy": "short", "word_count": 10}, [])
        self.assertLess(long_buzz["len_mult"], short_buzz["len_mult"])

    def test_r5_novelty_decay(self):
        fresh = _apply_hidden_rules({"channel": "buzz", "slot": "12:00", "hashtags": ["a"],
                                     "copy": "hi", "word_count": 5, "format": "meme"},
                                    ["story", "poll"])
        stale = _apply_hidden_rules({"channel": "buzz", "slot": "12:00", "hashtags": ["a"],
                                     "copy": "hi", "word_count": 5, "format": "meme"},
                                    ["meme", "meme", "meme"])
        self.assertLess(stale["decay_mult"], fresh["decay_mult"])

    def test_end_to_end_publish(self):
        p = MockPlatform(db_path=":memory:")
        out = p.publish_post({"post_id": "t1", "campaign_id": "c1", "channel": "buzz",
                              "day": 1, "slot": "19:00", "copy": "Students, espresso for less?",
                              "hashtags": ["coffee", "students"], "format": "meme",
                              "cta": "question", "creative_brief": "dorm desk shot"})
        self.assertGreater(out["impressions"], 1000)
        self.assertTrue(p.get_metrics("t1"))
        self.assertTrue(p.get_comments("t1"))


if __name__ == "__main__":
    unittest.main()
