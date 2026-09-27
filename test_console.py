"""
Console smoke test — verifies the /v1/dashboard/* layer end to end.
Run: python test_console.py
"""

import json
import unittest

from fastapi.testclient import TestClient

from bot import app, contexts, suppressed_keys
from conversation_handlers import conv_manager


class TestConsole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.client.post("/v1/dashboard/reset")

    def setUp(self):
        # Reset through the API so the module-level console_state singleton is
        # cleared too — otherwise telemetry leaks between tests.
        self.client.post("/v1/dashboard/reset")
        contexts.clear()
        suppressed_keys.clear()
        conv_manager.conversations.clear()

    # -- static console ---------------------------------------------------
    def test_console_is_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Vera Elite", r.text)
        for asset in ("/styles.css", "/app.js"):
            self.assertEqual(self.client.get(asset).status_code, 200, asset)

    def test_api_still_not_shadowed(self):
        self.assertEqual(self.client.get("/v1/healthz").status_code, 200)
        self.assertEqual(self.client.get("/v1/metadata").status_code, 200)
        self.assertEqual(self.client.get("/docs").status_code, 200)
        self.assertEqual(self.client.get("/openapi.json").status_code, 200)

    # -- seeding ----------------------------------------------------------
    def test_seed_loads_full_dataset(self):
        r = self.client.post("/v1/dashboard/seed", json={})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["loaded"]["category"], 5)
        self.assertEqual(data["loaded"]["merchant"], 50)
        self.assertEqual(data["loaded"]["customer"], 200)
        self.assertEqual(data["loaded"]["trigger"], 100)

    def test_overview_shape(self):
        self.client.post("/v1/dashboard/seed", json={})
        r = self.client.get("/v1/dashboard/overview")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        for key in ("engine", "contexts", "activity", "latency", "quality",
                    "guard_matrix", "cohorts", "top_triggers", "timeline"):
            self.assertIn(key, d)
        self.assertEqual(d["contexts"]["merchant"], 50)
        self.assertEqual(len(d["cohorts"]), 5)
        self.assertEqual(d["latency"]["budget_ms"], 30000)

    # -- directories ------------------------------------------------------
    def test_merchant_directory(self):
        self.client.post("/v1/dashboard/seed", json={})
        r = self.client.get("/v1/dashboard/merchants")
        self.assertEqual(r.json()["count"], 50)

        r = self.client.get("/v1/dashboard/merchants", params={"category": "dentists"})
        self.assertTrue(all(m["category_slug"] == "dentists" for m in r.json()["merchants"]))

        r = self.client.get("/v1/dashboard/merchants", params={"q": "lajpat"})
        self.assertGreaterEqual(r.json()["count"], 1)

    def test_merchant_detail_has_four_contexts(self):
        self.client.post("/v1/dashboard/seed", json={})
        r = self.client.get("/v1/dashboard/merchants/m_001_drmeera_dentist_delhi")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["summary"]["name"], "Dr. Meera's Dental Clinic")
        self.assertIn("voice", d["category_context"])
        self.assertIn("performance", d["merchant_context"])
        self.assertTrue(len(d["triggers"]) > 0)
        self.assertTrue(len(d["customers"]) > 0)

    def test_merchant_detail_404_when_unloaded(self):
        r = self.client.get("/v1/dashboard/merchants/m_nope")
        self.assertEqual(r.status_code, 404)

    # -- tick -------------------------------------------------------------
    def test_console_tick_composes_real_actions(self):
        self.client.post("/v1/dashboard/seed", json={})
        r = self.client.post("/v1/dashboard/tick", json={"limit": 5})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(len(data["actions"]), 5)
        first = data["actions"][0]
        self.assertIn("body", first)
        self.assertTrue(first["body"].strip())

        ov = self.client.get("/v1/dashboard/overview").json()
        self.assertEqual(ov["activity"]["actions_composed"], 5)
        self.assertEqual(ov["quality"]["samples"], 5)
        self.assertGreater(ov["quality"]["total"], 0)

    def test_latency_series_is_one_sample_per_composition(self):
        """The sparkline must stay on a single scale: one sample per composed
        action, not one per tick (which mixed a whole-tick total with
        single-message times)."""
        self.client.post("/v1/dashboard/seed", json={})
        self.client.post("/v1/dashboard/tick", json={"limit": 6})
        lat = self.client.get("/v1/dashboard/overview").json()["latency"]
        self.assertEqual(lat["samples"], 6)
        self.assertEqual(len(lat["series"]), 6)
        self.assertTrue(all(v > 0 for v in lat["series"]))
        self.assertEqual(lat["max_ms"], max(lat["series"]))
        self.assertEqual(lat["min_ms"], min(lat["series"]))

    def test_console_tick_works_when_bot_runs_as_main(self):
        """
        Regression: `python bot.py` (the documented run command) executes bot.py
        as __main__, so a late `import bot` inside the console handler loaded a
        *second* copy of the module — with its own empty `contexts` — and the
        tick silently composed zero actions. pytest imports bot as `bot`, so
        this has to shell out to the real command to be observable.
        """
        import json
        import os
        import subprocess
        import sys
        import time
        import urllib.error
        import urllib.request

        root = os.path.dirname(os.path.abspath(__file__))
        port = 8765
        base = f"http://127.0.0.1:{port}"

        proc = subprocess.Popen(
            [sys.executable, "bot.py"],
            cwd=root,
            env=dict(os.environ, PORT=str(port)),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

        def post(path, payload):
            req = urllib.request.Request(
                base + path, data=json.dumps(payload).encode(),
                headers={"content-type": "application/json"},
            )
            return json.loads(urllib.request.urlopen(req, timeout=20).read())

        try:
            for _ in range(150):                       # wait for the port to open
                if proc.poll() is not None:
                    self.fail(f"bot.py exited early:\n{proc.stdout.read()}")
                try:
                    urllib.request.urlopen(base + "/v1/healthz", timeout=1).read()
                    break
                except (urllib.error.URLError, ConnectionError, OSError):
                    time.sleep(0.2)
            else:
                self.fail("bot.py did not become healthy in time")

            post("/v1/dashboard/seed", {})
            ticked = post("/v1/dashboard/tick", {"limit": 5})
            self.assertEqual(ticked["trigger_count"], 5)
            self.assertEqual(len(ticked["actions"]), 5, ticked)
            self.assertTrue(all(a.get("body", "").strip() for a in ticked["actions"]))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()

    # -- simulator --------------------------------------------------------
    def test_simulator_cold_open_then_commitment(self):
        self.client.post("/v1/dashboard/seed", json={})
        mid = "m_001_drmeera_dentist_delhi"

        r = self.client.post("/v1/dashboard/simulate", json={
            "conversation_id": "c1", "merchant_id": mid, "message": ""
        })
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["turn"]["action"], "send")
        self.assertIn("2,410", d["turn"]["message"])          # real views figure
        self.assertEqual(len(d["turn"]["trace"]), 5)
        self.assertGreaterEqual(d["turn"]["score"]["total"], 30)

        r = self.client.post("/v1/dashboard/simulate", json={
            "conversation_id": "c1", "merchant_id": mid, "message": "Ok lets do it. Whats next?"
        })
        d = r.json()
        self.assertEqual(d["turn"]["action"], "send")
        self.assertIn("intent_handoff", d["turn"]["guards"])
        body = d["turn"]["contract"]["body"].lower()
        self.assertFalse(any(w in body for w in ["would you", "can you tell", "how about"]))

    def test_simulator_auto_reply_backoff(self):
        self.client.post("/v1/dashboard/seed", json={})
        mid = "m_001_drmeera_dentist_delhi"
        r = self.client.post("/v1/dashboard/simulate", json={
            "conversation_id": "c2", "merchant_id": mid, "message": ""
        })
        r = self.client.post("/v1/dashboard/simulate", json={
            "conversation_id": "c2", "merchant_id": mid,
            "message": "Thank you for contacting us! Our team will respond shortly."
        })
        d = r.json()
        self.assertEqual(d["turn"]["action"], "wait")
        self.assertEqual(d["turn"]["wait_seconds"], 14400)
        self.assertIn("auto_reply_detected", d["turn"]["guards"])

    def test_simulator_hostile_closes_thread(self):
        self.client.post("/v1/dashboard/seed", json={})
        mid = "m_001_drmeera_dentist_delhi"
        self.client.post("/v1/dashboard/simulate", json={
            "conversation_id": "c3", "merchant_id": mid, "message": ""
        })
        r = self.client.post("/v1/dashboard/simulate", json={
            "conversation_id": "c3", "merchant_id": mid,
            "message": "Stop messaging me. This is useless spam."
        })
        d = r.json()
        self.assertEqual(d["turn"]["action"], "end")
        self.assertTrue(d["closed"])

        # closed threads reject further traffic
        r = self.client.post("/v1/dashboard/simulate", json={
            "conversation_id": "c3", "merchant_id": mid, "message": "hello?"
        })
        self.assertEqual(r.status_code, 409)

    def test_simulator_with_trigger_opener(self):
        self.client.post("/v1/dashboard/seed", json={})
        r = self.client.post("/v1/dashboard/simulate", json={
            "conversation_id": "c4",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "trigger_id": "trg_001_research_digest_dentists",
            "message": "",
        })
        d = r.json()
        self.assertIn("JIDA", d["turn"]["message"])
        self.assertEqual(d["turn"]["source"], "trigger:research_digest")

    def test_simulator_requires_loaded_merchant(self):
        r = self.client.post("/v1/dashboard/simulate", json={
            "conversation_id": "c5", "merchant_id": "m_unknown", "message": ""
        })
        self.assertEqual(r.status_code, 404)

    # -- trace honesty ----------------------------------------------------
    def _stage(self, turn, layer):
        return next(s for s in turn["trace"] if s["layer"] == layer)

    def test_trace_never_claims_a_guard_that_did_not_fire(self):
        """The auto-reply guard is evaluated *inside* handle_reply, before the
        turn is recorded. Re-running it after the fact reads a longer history
        and used to mislabel a normal send as a backoff."""
        self.client.post("/v1/dashboard/seed", json={})
        mid = "m_001_drmeera_dentist_delhi"
        self.client.post("/v1/dashboard/simulate", json={
            "conversation_id": "c6", "merchant_id": mid, "message": ""
        })

        for i, msg in enumerate(["What are your weekend timings?",
                                 "Do you offer home visits?"]):
            d = self.client.post("/v1/dashboard/simulate", json={
                "conversation_id": "c6", "merchant_id": mid, "message": msg
            }).json()
            turn = d["turn"]
            self.assertEqual(turn["action"], "send", msg)
            guard = self._stage(turn, "Auto-Reply State Guard")
            self.assertEqual(guard["status"], "clear", msg)
            self.assertIn("No canned", guard["detail"])
            self.assertNotIn("auto_reply_detected", turn["guards"])

    def test_trace_labels_persistent_auto_reply_exit(self):
        """A long enough history trips the engine's repeat heuristic; the exit
        must be reported as an auto-reply close, not a hostile one."""
        self.client.post("/v1/dashboard/seed", json={})
        mid = "m_001_drmeera_dentist_delhi"
        self.client.post("/v1/dashboard/simulate", json={
            "conversation_id": "c7", "merchant_id": mid, "message": ""
        })
        for _ in range(4):
            r = self.client.post("/v1/dashboard/simulate", json={
                "conversation_id": "c7", "merchant_id": mid,
                "message": "Kindly share the timing",
            })
            if r.json().get("closed"):
                break
        turn = r.json()["turn"]
        self.assertEqual(turn["action"], "end")
        self.assertIn("auto_reply_persistent", turn["guards"])
        self.assertEqual(self._stage(turn, "Auto-Reply State Guard")["status"], "fired")
        self.assertIn("Persistent", self._stage(turn, "Auto-Reply State Guard")["detail"])
        # The engine exits on the auto-reply guard before the intent matrix is
        # ever consulted, so the trace must not claim "no commitment language".
        intent = self._stage(turn, "Intent Handoff Matrix")
        self.assertEqual(intent["status"], "skipped")
        self.assertIn("Not reached", intent["detail"])

    def test_intent_stage_fires_on_a_fresh_commitment(self):
        self.client.post("/v1/dashboard/seed", json={})
        d = self.client.post("/v1/dashboard/simulate", json={
            "conversation_id": "c8", "merchant_id": "m_001_drmeera_dentist_delhi", "message": ""
        }).json()
        d = self.client.post("/v1/dashboard/simulate", json={
            "conversation_id": "c8", "merchant_id": "m_001_drmeera_dentist_delhi",
            "message": "Ok lets do it. Whats next?",
        }).json()
        intent = self._stage(d["turn"], "Intent Handoff Matrix")
        self.assertEqual(intent["status"], "fired")
        self.assertIn("action mode", intent["detail"])

    def test_simulator_reports_real_latency(self):
        """The opener used to time two back-to-back calls, always reporting
        0.00 ms. The clock must span the real composition work."""
        self.client.post("/v1/dashboard/seed", json={})
        d = self.client.post("/v1/dashboard/simulate", json={
            "conversation_id": "c9",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "message": "",
        }).json()
        self.assertGreater(d["turn"]["latency_ms"], 0.0)

    # -- rubric -----------------------------------------------------------
    def test_rubric_contract(self):
        from rubric import score_message
        strong = score_message(
            "Dr. Meera, JIDA's 2,100-patient trial showed 38% better outcomes; cleaning is ₹299.",
            "binary_yes_no", {"voice": {"vocab_taboo": ["guaranteed"]}},
            {"identity": {"name": "Dr. Meera's Dental Clinic", "owner_first_name": "Meera"}}
        )
        self.assertGreaterEqual(strong["total"], 40)
        self.assertEqual(strong["verdict"], "EXCELLENT")
        self.assertTrue(strong["qualification_free"])
        self.assertIn("JIDA", strong["facts"]["citations"])
        self.assertEqual(strong["facts"]["percentages"], ["38%"])
        self.assertEqual(strong["facts"]["prices"], ["₹299"])

        # Dimension maths must stay identical to the judge harness heuristic:
        # 3 numeric anchors + citation  -> specificity 9
        # no taboo hit                  -> category    10
        # owner name in body            -> merchant    10
        # body > 30 chars               -> decision    10
        # no question/reply in body     -> engagement   8
        self.assertEqual(strong["total"], 47)

        weak = score_message("Buy now", "none", {}, {})
        self.assertEqual(weak["dimensions"]["specificity"]["score"], 4)
        self.assertEqual(weak["dimensions"]["engagement"]["score"], 8)
        self.assertLess(strong["total"], 50)

        taboo = score_message("Results guaranteed, visit us today?", "binary_yes_no",
                              {"voice": {"vocab_taboo": ["guaranteed"]}}, {})
        self.assertEqual(taboo["taboo_hit"], "guaranteed")
        self.assertEqual(taboo["dimensions"]["category_fit"]["score"], 6)

        stalling = score_message("Would you like me to send the details?", "none", {}, {})
        self.assertFalse(stalling["qualification_free"])

    # -- reset ------------------------------------------------------------
    def test_reset_clears_everything(self):
        self.client.post("/v1/dashboard/seed", json={})
        self.client.post("/v1/dashboard/reset")
        d = self.client.get("/v1/dashboard/overview").json()
        self.assertEqual(d["contexts"]["total"], 0)
        self.assertEqual(d["quality"]["samples"], 0)
        # the reset itself is the only surviving timeline entry
        self.assertEqual([e["kind"] for e in d["timeline"]], ["system"])
        self.assertEqual(self.client.get("/v1/dashboard/simulations").json()["simulations"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
