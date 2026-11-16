"""Buddy -> arm server: the arm page, camera and SAM are replaced; nothing real is contacted."""
import argparse
import io
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

import dispense_server as ds


def request(kind, **changes):
    body = {"schema": ds.SCHEMA, "message_type": kind, "request_id": "r1", "dry_run": False, "one_shot_detect": True}
    if kind == "dispense":
        body.update(medicine={"id": 1, "name": "Aspirin", "person": None}, ocr_min_confidence=.65,
                    actions=["locate", "pick_to_bowl", "verify_place"], slot_hint=None)
    else:
        body.update(object={"label": "water bottle"}, destination="bowl", actions=["locate", "pick", "place"])
    return {**body, **changes}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.a = argparse.Namespace(arm_url="http://arm.invalid", meds={"aspirin": "small bottle"}, read_labels=False,
                                    bowl=[-20., 15.], bowl_text="bowl", handover=None, listen_only=False,
                                    bowl_height=8., close=5., above=14.)
        frame = io.BytesIO()
        Image.new("RGB", (64, 48)).save(frame, format="JPEG")
        self.pressed, self.holding, self.asked = [], True, []
        mask = np.zeros((48, 64), bool)
        mask[20:40, 10:30] = True
        self.candidates = [{"mask": mask, "target_score": .8}]

        def get(url, **_):
            return Mock(json=Mock(return_value={"read_only": False}), content=frame.getvalue())

        def post(url, json=None, **_):
            self.pressed.append((url.rsplit("/", 1)[1], json))
            return Mock(json=Mock(return_value={"ok": True, "x": -3., "y": 22., "holding": self.holding}))

        def decide(img, text, category):
            self.asked.append(text)
            return {"candidates": self.candidates}

        for target, attr, value in ((ds.requests, "get", get), (ds.requests, "post", post), (ds.ts, "decide", decide)):
            patcher = patch.object(target, attr, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def buttons(self):
        return [name for name, _ in self.pressed]

    def test_medicine_still_works_as_before(self):
        out = ds.dispense(self.a, request("dispense"))
        self.assertEqual((out["ok"], out["final_state"], out["message_type"]), (True, "placed", "dispense_result"))
        self.assertEqual(self.asked, ["small bottle"])
        self.assertEqual(self.buttons(), ["click", "grab", "place", "park"])

    def test_everyday_object_goes_to_the_bowl_or_the_handover_spot(self):
        out = ds.dispense(self.a, request("move_object"), "move_object")
        self.assertEqual((out["ok"], out["final_state"], out["message_type"], out["label"]),
                         (True, "placed", "move_object_result", "water bottle"))
        self.assertEqual(self.asked, ["water bottle"])
        self.assertEqual(self.pressed[2][1]["x"], -20.)
        refused = ds.dispense(self.a, request("move_object", destination="handover"), "move_object")
        self.assertEqual((refused["ok"], refused["final_state"]), (False, "aborted"))
        self.a.handover = [-5., 30.]
        self.pressed.clear()
        self.assertTrue(ds.dispense(self.a, request("move_object", destination="handover"), "move_object")["ok"])
        self.assertEqual((self.pressed[2][1]["x"], self.pressed[2][1]["y"]), (-5., 30.))

    def test_refusals_and_failures_never_move_or_claim_placement(self):
        for changes in ({"object": {"label": "a knife"}}, {"object": {"label": ""}}, {"destination": "kitchen"},
                        {"message_type": "dispense"}):
            out = ds.dispense(self.a, request("move_object", **changes), "move_object")
            self.assertEqual((out["ok"], out["final_state"]), (False, "aborted"), changes)
        self.assertEqual(self.pressed, [])
        self.candidates = []
        out = ds.dispense(self.a, request("move_object"), "move_object")
        self.assertEqual((out["ok"], out["detect_detail"], self.pressed), (False, "not_found", []))

    def test_listen_only_shows_the_request_and_touches_nothing(self):
        self.a.listen_only = True
        for kind in ("dispense", "move_object"):
            out = ds.dispense(self.a, request(kind), kind)
            self.assertEqual((out["ok"], out["final_state"]), (False, "aborted"))
            self.assertIn("connection test", out["error"])
        self.assertEqual((self.pressed, self.asked), ([], []))
        self.assertIsNone(ds.check_taken(request("dispense"))["taken"])


if __name__ == "__main__":
    unittest.main()
