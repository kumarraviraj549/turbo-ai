import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tubeforge import jobs, planner, procedural, render
from workers import procedural_worker


class PhaseFiveTests(unittest.TestCase):
    def test_procedural_scene_fields_round_trip_without_changing_legacy_default(self):
        scene = planner.Scene(
            id="proc",
            videoSource="procedural_3d_2d",
            proceduralTemplate="3d_stat_callout",
            proceduralProps={"value": 42, "prefix": "$"},
        )
        legacy = planner.Scene(id="legacy")
        restored = planner.Scene(**scene.model_dump(by_alias=True))

        self.assertEqual(restored.video_source, "procedural_3d_2d")
        self.assertEqual(restored.procedural_template, "3d_stat_callout")
        self.assertEqual(restored.procedural_props["value"], 42)
        self.assertEqual(legacy.video_source, "original_motion")

    def test_scene_props_inherit_project_identity_and_require_story_citations(self):
        settings = {
            "aspect": "9:16 Shorts", "channel_name": "Field Notes",
            "brand_primary_color": "#123456", "brand_secondary_color": "#234567",
            "brand_accent_color": "#345678",
        }
        scene = planner.Scene(
            id="quote", title="A finding", narration="Researchers reported a 42% change.",
            proceduralTemplate="quote_evidence_card",
            proceduralProps={"quote": "A 42% change", "source": "Source A", "context": "Study"},
        )

        with self.assertRaisesRegex(ValueError, "must match a source"):
            procedural.scene_props(scene, settings, 5, ["Source B"])

        props = procedural.scene_props(scene, settings, 5, ["Source A"])
        self.assertEqual(props["aspect"], "9:16")
        self.assertEqual(props["channelName"], "Field Notes")
        self.assertEqual(props["brand"]["primary"], "#123456")
        self.assertEqual(props["source"], "Source A")
        scene.procedural_props["unrecognized"] = True
        with self.assertRaisesRegex(ValueError, "Unexpected template props"):
            procedural.scene_props(scene, settings, 5, ["Source A"])

    def test_only_procedural_props_change_the_procedural_scene_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "source.png"
            Image.new("RGB", (64, 36), "navy").save(image)
            spec = render.SceneRenderSpec(
                out_path=root / "clip.mp4", width=64, height=36, fps=12, duration=2,
                source_type="image", source_path=image,
            )
            baseline = jobs.spec_fingerprint(spec)
            ordinary_props = jobs.spec_fingerprint(
                render.SceneRenderSpec(**{**spec.__dict__, "procedural_props": {"value": 2}})
            )
            proc = jobs.spec_fingerprint(render.SceneRenderSpec(
                **{**spec.__dict__, "video_source": "procedural_3d_2d", "procedural_props": {"value": 2}}
            ))
            changed_proc = jobs.spec_fingerprint(render.SceneRenderSpec(
                **{**spec.__dict__, "video_source": "procedural_3d_2d", "procedural_props": {"value": 3}}
            ))

        self.assertEqual(baseline, ordinary_props)
        self.assertNotEqual(proc, changed_proc)

    def test_worker_caches_scene_clip_by_image_and_props(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "source.png"
            output = root / "scene_01_proc.mp4"
            Image.new("RGB", (64, 36), "navy").save(image)
            props = {
                "template": "3d_stat_callout", "durationInSeconds": 2, "aspect": "16:9",
                "channelName": "Test", "title": "Stat",
                "brand": {"primary": "#8B5CF6", "secondary": "#22D3EE", "accent": "#F472B6"},
                "value": 12, "prefix": "", "suffix": "%", "subtitle": "A finding",
            }
            config = {
                "scene_dir": str(root), "scene_index": 1, "image": str(image), "output": str(output),
                "props": props, "duration": 2, "width": 64, "height": 36, "fps": 12,
            }

            def fake_cli(arguments, _timeout):
                Path(arguments[3]).write_bytes(b"mp4-bytes")

            with patch.object(procedural_worker, "_run_cli", side_effect=fake_cli) as cli:
                first = procedural_worker.run(config)
                second = procedural_worker.run(config)
            saved_props = json.loads((root / "scene_01_props.json").read_text(encoding="utf-8"))

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(cli.call_count, 1)
        self.assertEqual(saved_props, props)

    def test_procedural_source_overrides_stock_video_and_enters_scene_pipeline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "stock.mp4"
            source.write_bytes(b"source video")
            output = root / "clip.mp4"
            spec = render.SceneRenderSpec(
                out_path=output, width=64, height=36, fps=12, duration=2,
                source_type="video", source_path=source, video_source="procedural_3d_2d",
            )

            def create_motion_clip(_worker, payload, _work, _cancel):
                Path(payload["output"]).write_bytes(b"motion clip")
                return {"ok": True}

            with patch.object(render, "_run_motion_worker", side_effect=create_motion_clip), \
                    patch.object(render, "video_is_decodable", return_value=True), \
                    patch.object(render, "_render_scene_base", return_value=output) as finalize:
                result = render.render_scene(spec)

        self.assertEqual(result, output)
        self.assertEqual(finalize.call_args.args[0].source_type, "video")
        self.assertIn("scene_01_proc.mp4", str(finalize.call_args.args[0].source_path))


if __name__ == "__main__":
    unittest.main()
