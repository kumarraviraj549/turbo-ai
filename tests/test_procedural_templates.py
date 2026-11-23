import unittest

from tubeforge import planner, procedural


class ProceduralTemplateTests(unittest.TestCase):
    def _render(self, template, props=None, sources=()):
        scene = planner.Scene(
            id="template-test",
            title="A finding",
            narration="A measured change of 42 percent.",
            proceduralTemplate=template,
            proceduralProps=props or {},
        )
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


if __name__ == "__main__":
    unittest.main()
