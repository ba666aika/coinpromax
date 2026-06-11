"""Tests for the community-engagement source (bot.community).

Covers the manual allowlist parser and the coin-communities server messages
client (httpx mocked). The safety contract is the point: the API client RAISES
on any failure so the caller can fail SAFE, and the allowlist NEVER raises.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from solders.keypair import Keypair as _TestKp  # noqa: E402

_test_kp = _TestKp()
os.environ.setdefault("HELIUS_API_KEY", "test")
os.environ.setdefault("WALLET_PRIVATE_KEY", str(_test_kp))
os.environ.setdefault("LOYALTY_MINT", "2jCt3hj9vd7YpV7Sr3VA5nk3tdSpJtZezeoJXW4Xpump")
os.environ.setdefault("OPERATOR_WALLET", str(_TestKp().pubkey()))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("MIN_HOLDING_RAW", "1")

from bot import community  # noqa: E402

_CREDS = {"api_key": "cck_test", "api_secret": "ccs_test"}


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _write(tmp, name, content):
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


class TestAllowlist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_missing_file_returns_empty(self):
        self.assertEqual(community.load_allowlist(os.path.join(self.tmp, "nope.json")), set())

    def test_empty_file_returns_empty(self):
        self.assertEqual(community.load_allowlist(_write(self.tmp, "a.json", "  \n ")), set())

    def test_json_list(self):
        p = _write(self.tmp, "a.json", json.dumps(["AAA", "BBB", " CCC "]))
        self.assertEqual(community.load_allowlist(p), {"AAA", "BBB", "CCC"})

    def test_json_object_with_wallets(self):
        p = _write(self.tmp, "a.json", json.dumps({"wallets": ["AAA", "BBB"]}))
        self.assertEqual(community.load_allowlist(p), {"AAA", "BBB"})

    def test_newline_list_with_comments(self):
        p = _write(self.tmp, "a.txt", "# header\nAAA\n  BBB \n\n# note\nCCC\n")
        self.assertEqual(community.load_allowlist(p), {"AAA", "BBB", "CCC"})


class TestFetchEngagedFromApi(unittest.TestCase):
    MINT = "MintMintMintMintMintMintMintMintMintMintMint"

    def test_missing_creds_raises(self):
        with self.assertRaises(community.CommunityError):
            community.fetch_engaged_from_api(self.MINT, api_key="", api_secret="ccs")
        with self.assertRaises(community.CommunityError):
            community.fetch_engaged_from_api(self.MINT, api_key="cck", api_secret="")

    def test_collects_wallets_incl_field_fallbacks(self):
        payload = {"items": [
            {"walletAddress": "W1", "content": "gm"},
            {"wallet": "W2"},                          # fallback field
            {"author": {"walletAddress": "W3"}},        # nested author object
            {"content": "no wallet here"},              # ignored
            {"walletAddress": "W1"},                    # dup
        ]}
        with mock.patch.object(community.httpx, "get", return_value=_Resp(200, payload)):
            got = community.fetch_engaged_from_api(self.MINT, **_CREDS)
        self.assertEqual(got, {"W1", "W2", "W3"})

    def test_counts_only_top_level_posts(self):
        # Real schema: only a top-level POST in OUR community counts — replies,
        # deleted posts, and other coins' posts are excluded. (Likes never appear
        # as messages, so they can't sneak in.)
        payload = {"messages": [
            {"walletAddress": "POST1", "parentMessageId": None, "deletedAt": None, "tokenAddress": self.MINT},
            {"walletAddress": "REPLY",  "parentMessageId": "abc", "deletedAt": None, "tokenAddress": self.MINT},
            {"walletAddress": "DELETED","parentMessageId": None, "deletedAt": "2026-05-31T00:00:00Z", "tokenAddress": self.MINT},
            {"walletAddress": "FOREIGN","parentMessageId": None, "deletedAt": None, "tokenAddress": "OtherCoinMint"},
            {"walletAddress": "POST2",  "parentMessageId": None, "deletedAt": None, "tokenAddress": self.MINT},
        ]}
        with mock.patch.object(community.httpx, "get", return_value=_Resp(200, payload)):
            got = community.fetch_engaged_from_api(self.MINT, **_CREDS)
        self.assertEqual(got, {"POST1", "POST2"})

    def test_sends_server_key_headers(self):
        seen = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            seen["url"] = url
            seen["headers"] = headers
            return _Resp(200, {"items": []})

        with mock.patch.object(community.httpx, "get", side_effect=fake_get):
            community.fetch_engaged_from_api(self.MINT, **_CREDS)
        self.assertIn(self.MINT, seen["url"])
        self.assertEqual(seen["headers"].get("x-server-key"), "cck_test")
        self.assertEqual(seen["headers"].get("x-server-secret"), "ccs_test")
        self.assertNotIn("x-api-key", seen["headers"])

    def test_paging_until_short_page(self):
        pages = [
            {"items": [{"walletAddress": "A"}, {"walletAddress": "B"}]},  # full page (limit=2)
            {"items": [{"walletAddress": "C"}]},                          # short page → stop
        ]
        calls = {"n": 0}

        def fake_get(url, params=None, headers=None, timeout=None):
            i = calls["n"]; calls["n"] += 1
            return _Resp(200, pages[i] if i < len(pages) else {"items": []})

        with mock.patch.object(community, "_PAGE_LIMIT", 2):
            with mock.patch.object(community.httpx, "get", side_effect=fake_get):
                got = community.fetch_engaged_from_api(self.MINT, **_CREDS)
        self.assertEqual(got, {"A", "B", "C"})
        self.assertEqual(calls["n"], 2)

    def test_offset_ignored_no_new_wallets_stops(self):
        # API that always returns the same full page must not loop forever.
        same = {"items": [{"walletAddress": "A"}, {"walletAddress": "B"}]}
        calls = {"n": 0}

        def fake_get(url, params=None, headers=None, timeout=None):
            calls["n"] += 1
            return _Resp(200, same)

        with mock.patch.object(community, "_PAGE_LIMIT", 2):
            with mock.patch.object(community.httpx, "get", side_effect=fake_get):
                got = community.fetch_engaged_from_api(self.MINT, **_CREDS)
        self.assertEqual(got, {"A", "B"})
        self.assertLessEqual(calls["n"], 2)  # stopped once no new wallets appeared

    def test_parses_large_message_volume_fast(self):
        import time
        # 50 pages × 100 = 5000 messages (half replies); only top-level posts count.
        def make_page(start):
            msgs = []
            for i in range(start, start + 100):
                msgs.append({"walletAddress": f"W{i}",
                             "parentMessageId": ("p" if i % 2 else None),  # odd = reply
                             "deletedAt": None, "tokenAddress": self.MINT})
            return {"messages": msgs}
        calls = {"n": 0}

        def fake_get(url, params=None, headers=None, timeout=None):
            n = calls["n"]; calls["n"] += 1
            return _Resp(200, make_page(n * 100) if n < 50 else {"messages": []})

        with mock.patch.object(community, "_PAGE_LIMIT", 100):
            t = time.perf_counter()
            with mock.patch.object(community.httpx, "get", side_effect=fake_get):
                got = community.fetch_engaged_from_api(self.MINT, **_CREDS)
            elapsed = time.perf_counter() - t
        self.assertEqual(len(got), 2500)          # only the 2500 top-level posts
        self.assertLess(elapsed, 1.0)             # 5000 messages parsed well under 1s

    def test_non_200_raises(self):
        with mock.patch.object(community.httpx, "get", return_value=_Resp(401, {})):
            with self.assertRaises(community.CommunityError):
                community.fetch_engaged_from_api(self.MINT, **_CREDS)

    def test_http_error_raises(self):
        import httpx
        with mock.patch.object(community.httpx, "get", side_effect=httpx.HTTPError("boom")):
            with self.assertRaises(community.CommunityError):
                community.fetch_engaged_from_api(self.MINT, **_CREDS)

    def test_bad_json_raises(self):
        with mock.patch.object(community.httpx, "get", return_value=_Resp(200, ValueError("nope"))):
            with self.assertRaises(community.CommunityError):
                community.fetch_engaged_from_api(self.MINT, **_CREDS)


if __name__ == "__main__":
    unittest.main()
