import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from tubeforge import planner, procedural


class ProceduralTemplateTests(unittest.TestCase):
    def _render(self, template, props=None, sources=(), model_public_dir=None):
        scene = planner.Scene(
            id="template-test",
            title="A finding",
            narration="A measured change of 42 percent.",
            proceduralTemplate=template,
            proceduralProps=props or {},
        )
        if model_public_dir is not None:
            return procedural.scene_props(scene, {"channel_name": "Test"}, 4, sources,
                                           model_public_dir=model_public_dir)
        return procedural.scene_props(scene, {"channel_name": "Test"}, 4, sources)

    def test_3d_bar_chart_props(self):
        props = self._render("3d_bar_chart")
        self.assertEqual(props["template"], "3d_bar_chart")
        self.assertEqual(props["bars"][0]["value"], 42.0)

    def test_3d_stat_callout_props(self):
        props = self._render("3d_stat_callout", {"value": 42})
        self.assertEqual(props["value"], 42.0)
        self.assertEqual(props["template"], "3d_stat_callout")

    def test_comparison_split_props(self):
        props = self._render("comparison_split")
        self.assertEqual(props["left"]["label"], "Option A")
        self.assertEqual(props["right"]["label"], "Option B")

    def test_timeline_journey_props(self):
        props = self._render("timeline_journey")
        self.assertEqual(len(props["milestones"]), 2)

    def test_process_flow_props(self):
        props = self._render("process_flow")
        self.assertEqual([step["label"] for step in props["steps"]], ["Discover", "A finding"])

    def test_quote_evidence_card_requires_a_matching_source(self):
        props = self._render(
            "quote_evidence_card", {"quote": "A verified fact", "source": "Research A"}, ["Research A"]
        )
        self.assertEqual(props["source"], "Research A")
        with self.assertRaisesRegex(ValueError, "must match a source"):
            self._render("quote_evidence_card", {"source": "Unlisted"}, ["Research A"])

    def test_2d_character_explainer_props_do_not_require_a_research_source(self):
        props = self._render(
            "2d_character_explainer",
            {"characterName": "Mira", "emotion": "happy",
             "mouthStateTimeline": [{"startFrame": 0, "endFrame": 2, "mouth": "open"}]},
        )
        self.assertEqual(props["characterName"], "Mira")
        self.assertEqual(props["mouthStateTimeline"][0]["mouth"], "open")

    def test_kinetic_hook_title_props_do_not_require_a_research_source(self):
        props = self._render("kinetic_hook_title", {"hookText": "Look closer", "kicker": "A clue"})
        self.assertEqual(props["hookText"], "Look closer")
        self.assertEqual(props["kicker"], "A clue")

    def test_map_route_animator_defaults_and_coordinate_validation(self):
        props = self._render("map_route_animator")
        self.assertEqual(len(props["cities"]), 2)
        self.assertEqual(props["routes"][0]["from"], "Origin")
        self.assertEqual(props["mapCenter"]["longitude"], 0.0)
        with self.assertRaisesRegex(ValueError, "Longitude"):
            self._render("map_route_animator", {"cities": [
                {"name": "Bad", "longitude": 181, "latitude": 10},
                {"name": "Good", "longitude": 0, "latitude": 0},
            ]})

    def test_3d_model_showcase_rejects_paths_outside_public_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            public_dir = Path(temp_dir)
            model_path = public_dir / "models" / "sample.glb"
            model_path.parent.mkdir()
            model_path.write_bytes(b"glTF")
            props = self._render("3d_model_showcase", {
                "modelUrl": "models/sample.glb", "calloutTitle": "Model",
            }, model_public_dir=public_dir)
            self.assertEqual(props["modelUrl"], "models/sample.glb")
            with self.assertRaisesRegex(ValueError, "stay inside"):
                self._render("3d_model_showcase", {
                    "modelUrl": "models/../../outside.glb",
                }, model_public_dir=public_dir)


if __name__ == "__main__":
    unittest.main()
