"""Tests for app.security — password hashing and HMAC signing."""
import unittest

from app.security import (
    hash_password, verify_password, random_token, sign, verify_signature,
)


class PasswordTests(unittest.TestCase):
    def test_round_trip(self):
        h, s = hash_password("correct horse battery staple")
        self.assertTrue(verify_password("correct horse battery staple", h, s))

    def test_wrong_password(self):
        h, s = hash_password("real-password")
        self.assertFalse(verify_password("wrong-password", h, s))

    def test_different_salts(self):
        # Same password hashed twice should produce different salts/hashes.
        h1, s1 = hash_password("p")
        h2, s2 = hash_password("p")
        self.assertNotEqual(s1, s2)
        self.assertNotEqual(h1, h2)
        # Both still verify
        self.assertTrue(verify_password("p", h1, s1))
        self.assertTrue(verify_password("p", h2, s2))

    def test_empty_password_handled(self):
        # Hashing empty string shouldn't crash; it should still round-trip.
        h, s = hash_password("")
        self.assertTrue(verify_password("", h, s))


class TokenTests(unittest.TestCase):
    def test_random_token_unique(self):
        tokens = {random_token() for _ in range(100)}
        self.assertEqual(len(tokens), 100)

    def test_random_token_length(self):
        # default nbytes=32 → base64-url string of ~43 chars
        self.assertGreaterEqual(len(random_token()), 32)


class SignTests(unittest.TestCase):
    def test_sign_verify_round_trip(self):
        sig = sign("hello")
        self.assertTrue(verify_signature("hello", sig))

    def test_different_payload_fails(self):
        sig = sign("hello")
        self.assertFalse(verify_signature("hello!", sig))

    def test_tampered_signature_fails(self):
        sig = sign("hello")
        tampered = sig[:-3] + ("aaa" if not sig.endswith("aaa") else "bbb")
        self.assertFalse(verify_signature("hello", tampered))


if __name__ == "__main__":
    unittest.main()
