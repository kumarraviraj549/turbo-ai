import concurrent.futures as cf
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tubeforge import alignment, audio, jobs, otio_export, planner, render, timeline_qa
from workers import depthflow_worker


class UpgradeVerificationTests(unittest.TestCase):
    def test_cpu_depth_warp_displaces_foreground_more_than_background(self):
        h, w = 220, 320
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        depth = np.where(xx > w * 0.58, 0.90, 0.20).astype(np.float32)
        map_x, _ = depthflow_worker._cpu_depth_warp_maps(
            depth, xx, yy, w, h, "dolly_in", 0.5, 1.0,
        )
        displacement = xx - map_x
        foreground = float(np.median(displacement[:, int(w * 0.62):]))
        background = float(np.median(displacement[:, :int(w * 0.54)]))
        self.assertGreater(abs(foreground - background), 1.0)

    def test_legacy_flat_scene_list_loads_as_chapter_one_at_long_form_scale(self):
        project = jobs.Project({
            "id": "legacy", "settings": {},
            "plan": {"scenes": [{"title": f"Scene {index + 1}"} for index in range(120)]},
        })
        plan = project.plan
        self.assertEqual(len(plan.scenes), 120)
        groups = planner.group_chapters(plan.scenes)
        self.assertEqual([name for name, _ in groups], ["Chapter 1"])
        self.assertEqual(len(groups[0][1]), 120)
        self.assertEqual(planner.MAX_SCENES, 250)

    def test_auto_lipsync_emits_cached_frame_mouth_states(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            samples = np.zeros(audio.SR, dtype=np.float32)
            tone = np.sin(2 * np.pi * 220 * np.arange(int(audio.SR * 0.4)) / audio.SR) * 0.35
            samples[:len(tone)] = tone
            samples[int(audio.SR * 0.6):int(audio.SR)] = tone
            wav_path = root / "voice.wav"
            audio._write_pcm_wav(wav_path, np.stack([samples, samples], axis=1))
            cache_path = root / "mouth_states.json"
            states = alignment.build_mouth_state_timeline([(wav_path, 0.0)], 30, cache_path)
            self.assertTrue(cache_path.is_file())
            self.assertTrue(any(item["mouth"] == "closed" for item in states))
            self.assertTrue(any(item["mouth"] in ("open", "wide") for item in states))
            self.assertTrue(all(item["endFrame"] >= item["startFrame"] for item in states))
            self.assertEqual(states, alignment.build_mouth_state_timeline([(wav_path, 0.0)], 30, cache_path))
            scene = planner.Scene(
                title="Speaker", videoSource="procedural_3d_2d",
                proceduralTemplate="2d_character_explainer",
                proceduralProps={"characterName": "Speaker", "emotion": "explaining",
                                 "mouthStateTimeline": []},
            )
            project_data = {
                "id": "lipsync-test", "settings": dict(jobs.DEFAULT_SETTINGS),
                "plan": planner.CinematicPlan(scenes=[scene]).model_dump(by_alias=True),
                "scene_state": {scene.id: {"seed": 1, "voice_parts": [
                    {"path": str(wav_path), "offset": 0.0},
                ]}},
            }
            with patch.object(jobs, "STUDIO_ROOT", root), \
                    patch.object(render, "detect_video_encoder", return_value={"recommended": "libx264"}):
                project = jobs.Project(project_data)
                spec = jobs.render_spec(project, scene, 0, "image", wav_path, 2.0, [])
                self.assertTrue(spec.procedural_props["mouthStateTimeline"])
                project.data["plan"]["scenes"][0]["proceduralProps"]["mouthStateTimeline"] = [
                    {"startFrame": 0, "endFrame": 4, "mouth": "wide"},
                ]
                manual_scene = project.plan.scenes[0]
                manual_spec = jobs.render_spec(project, manual_scene, 0, "image", wav_path, 2.0, [])
                self.assertEqual(manual_spec.procedural_props["mouthStateTimeline"], [
                    {"startFrame": 0, "endFrame": 4, "mouth": "wide"},
                ])

    def test_hardware_encoder_probe_runs_a_real_smoke_encode(self):
        def fake_run(command, **_kwargs):
            Path(command[-1]).write_bytes(b"encoded frame")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(render.shutil, "which", return_value="ffmpeg"), \
                patch.object(render.subprocess, "run", side_effect=fake_run) as probe:
            self.assertTrue(render._smoke_encoder("h264_nvenc"))
        self.assertIn("h264_nvenc", probe.call_args.args[0])

    def test_scene_render_process_pool_serializes_spec_and_writes_clip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.png"
            Image.new("RGB", (64, 64), (30, 80, 120)).save(source)
            spec = render.SceneRenderSpec(
                out_path=root / "clip.mp4", width=64, height=64, fps=1, duration=1,
                source_type="image", source_path=source, camera="Static (locked off)",
                atmosphere="none", grain=0, vignette=0, bloom=0, dof=0,
                caption_style="None", encoder="libx264", work_dir=root / "work",
            )
            cancel_path = root / "cancel"
            with cf.ProcessPoolExecutor(max_workers=1) as pool:
                output, messages = pool.submit(
                    jobs._render_scene_process, spec, str(cancel_path),
                ).result(timeout=90)
            self.assertEqual(Path(output), spec.out_path)
            self.assertTrue(spec.out_path.is_file())
            self.assertEqual(messages, [])

    def test_mix_exports_content_cached_audio_stems(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            samples = audio.SR * 4
            times = np.arange(samples) / audio.SR
            voice_mono = 0.35 * np.sin(2 * np.pi * 220 * times)
            voice_mono[(times < 0.5) | (times > 3.5)] = 0
            music_mono = 0.2 * np.sin(2 * np.pi * 55 * times)
            voice, music = (np.stack([item, item], axis=1).astype(np.float32)
                            for item in (voice_mono, music_mono))
            voice_path, music_path = root / "voice.wav", root / "music.wav"
            audio._write_pcm_wav(voice_path, voice)
            audio._write_pcm_wav(music_path, music)
            out_dir = root / "final"
            configured_mix = audio.mix_final(
                voice_path, music_path, None, root / "configured_mix.m4a", 4.0,
                voice_vol=1.0, music_vol=0.35, loudness=-20.0, duck_db=-24.0, stem_dir=out_dir,
                master_out=out_dir / "master_mix.wav",
            )
            master = out_dir / "master_mix.wav"
            narration = out_dir / "narration_stem.wav"
            music_sfx = out_dir / "music_sfx_stem.wav"
            self.assertTrue(master.is_file())
            self.assertTrue(configured_mix.is_file())
            self.assertTrue(narration.is_file())
            self.assertTrue(music_sfx.is_file())
            self.assertTrue((out_dir / "master_mix.wav.sha256").is_file())
            measured_lufs = render.measure_loudness(master)["integrated_lufs"]
            self.assertIsNotNone(measured_lufs)
            self.assertGreaterEqual(measured_lufs, -15.0)
            self.assertLessEqual(measured_lufs, -13.0)
            voice_stem = audio.decode(narration)
            bed_stem = audio.decode(music_sfx)
            active = (times > 1.0) & (times < 3.0)
            gap = audio._rms_db(bed_stem[active]) - audio._rms_db(voice_stem[active])
            self.assertGreater(gap, -26.0)
            self.assertLess(gap, -22.0)

    def test_adjacent_distinct_beats_never_repeat_visual_tuple(self):
        beats = alignment.persist_distinct_beats(
            {},
            [{"start": 0.0, "end": 4.0, "text": "First"},
             {"start": 4.0, "end": 8.0, "text": "Second"},
             {"start": 8.0, "end": 12.0, "text": "Third"}],
            "a" * 64,
        )
        for left, right in zip(beats, beats[1:]):
            left_tuple = (
                left["sourceAssetHash"],
                left["visualTreatment"]["motionEngine"],
                left["visualTreatment"]["cameraPreset"],
                tuple(left["visualTreatment"]["cropRect"]),
            )
            right_tuple = (
                right["sourceAssetHash"],
                right["visualTreatment"]["motionEngine"],
                right["visualTreatment"]["cameraPreset"],
                tuple(right["visualTreatment"]["cropRect"]),
            )
            self.assertNotEqual(left_tuple, right_tuple)

    def test_speech_ducking_holds_22_to_26_db_gap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sr = audio.SR
            samples = sr * 8
            t = np.arange(samples) / sr
            voice_mono = np.sin(2 * np.pi * 220 * t) * ((t > 1) & (t < 6)).astype(np.float32)
            music_mono = np.sin(2 * np.pi * 55 * t) * 0.9
            voice = np.stack([voice_mono, voice_mono], axis=1).astype(np.float32)
            music = np.stack([music_mono, music_mono], axis=1).astype(np.float32)
            ducked = audio.apply_speech_ducking(music, voice, target_db=-24.0)
            active = (t > 1.5) & (t < 5.5)
            voice_rms = audio._rms_db(voice[active])
            music_rms = audio._rms_db(ducked[active])
            gap = music_rms - voice_rms
            self.assertGreaterEqual(gap, -26.0)
            self.assertLessEqual(gap, -22.0)

    def test_tts_chunk_join_discontinuity_is_below_threshold(self):
        from tubeforge import tts

        left = np.zeros(24000, dtype=np.float32)
        right = np.ones(24000, dtype=np.float32) * 0.05
        stitched, _ = tts._stitch_chunks([
            (tts.SpeechChunk("first chunk", False), left),
            (tts.SpeechChunk("second chunk", False), right),
        ])
        join = len(left) - int(0.03 * 24000)
        differences = np.abs(np.diff(stitched[join - 12:join + 12]))
        self.assertLess(float(difference := differences.max()), 0.20)

    def test_silence_trim_and_waveform_cache_are_content_based(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            time_axis = np.arange(audio.SR * 3) / audio.SR
            samples = np.zeros_like(time_axis, dtype=np.float32)
            tone = 0.35 * np.sin(2 * np.pi * 220 * time_axis)
            samples[:audio.SR] = tone[:audio.SR]
            samples[2 * audio.SR:] = tone[2 * audio.SR:]
            source = root / "voice.wav"
            audio._write_pcm_wav(source, np.stack([samples, samples], axis=1))
            trimmed = audio.trim_silence(source, root / "trim-cache")
            self.assertTrue(trimmed.is_file())
            from tubeforge import tts

            duration = tts.probe_duration(trimmed)
            self.assertGreater(duration, 2.1)
            self.assertLess(duration, 2.5)
            envelope_path = root / "waveform.json"
            waveform = audio.waveform_envelope(source, envelope_path, buckets=30)
            self.assertEqual(len(waveform), 30)
            self.assertGreater(max(waveform), 0.9)
            self.assertEqual(waveform, audio.waveform_envelope(source, envelope_path, buckets=30))

    def test_beat_detection_cache_and_cut_snapping(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            samples = np.zeros(audio.SR * 4, dtype=np.float32)
            pulse = np.sin(2 * np.pi * 880 * np.arange(int(audio.SR * 0.06)) / audio.SR) * 0.8
            for start in (0.25, 0.75, 1.25, 1.75, 2.25, 2.75, 3.25):
                offset = int(start * audio.SR)
                samples[offset:offset + len(pulse)] = pulse
            source = root / "music.wav"
            audio._write_pcm_wav(source, np.stack([samples, samples], axis=1))
            cache = root / "beats.json"
            beats = audio.detect_beats(source, cache)
            self.assertGreaterEqual(len(beats), 4)
            self.assertEqual(beats, audio.detect_beats(source, cache))
            snapped = audio.snap_beats_to_music([
                {"start": 0.0, "end": 1.05},
                {"start": 1.05, "end": 2.0},
            ], beats, tolerance=0.35)
            self.assertAlmostEqual(snapped[0]["end"], snapped[1]["start"])
            self.assertLessEqual(abs(snapped[0]["end"] - 1.05), 0.35)

    def test_cube_luts_are_valid_and_ffmpeg_applies_the_3d_filter(self):
        lut_dir = Path(__file__).resolve().parents[1] / "assets" / "luts"
        luts = sorted(lut_dir.glob("*.cube"))
        self.assertEqual(len(luts), 6)
        for lut in luts:
            self.assertEqual(render.validate_cube_lut_text(lut.read_text(encoding="utf-8")), 2)
        command = [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=0.1",
            "-vf", f"lut3d=file='{render._escape_filter_path(luts[0])}':interp=tetrahedral",
            "-frames:v", "1", "-f", "null", "-",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        spec = render.SceneRenderSpec(
            out_path=Path("look.mp4"), width=64, height=64, fps=1, duration=1,
            source_type="image", source_path=Path("source.png"), lut_path=luts[0], lut_strength=0.45,
        )
        graph_result = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=0.1",
             "-filter_complex", ";".join(render._look_filters(spec, None)), "-map", "[look]",
             "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(graph_result.returncode, 0, graph_result.stderr)

    def test_otio_export_includes_relative_media_tracks_transitions_and_markers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            final = project / "final"
            scenes_dir = project / "scenes"
            scenes_dir.mkdir(parents=True)
            clip = scenes_dir / "scene.mp4"
            clip.write_bytes(b"placeholder")
            timeline_path = otio_export.write_timeline(
                final / "film.otio",
                name="Film",
                scenes=[{"id": "scene-1", "title": "Opening", "chapter": "Chapter 1", "clip": str(clip)},
                        {"id": "scene-2", "title": "Payoff", "chapter": "Chapter 2", "clip": str(clip)}],
                durations=[5.0, 6.0],
                transitions=[("fade", 0.5)],
                fps=30,
                narration=project / "final" / "narration_stem.wav",
                music_sfx=project / "final" / "music_sfx_stem.wav",
                overlays=[{"text": "Opening title", "kind": "Title"}, {}],
                beat_times=[2.5],
            )
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
            tracks = timeline["tracks"]["children"]
            self.assertEqual([track["name"] for track in tracks], [
                "Video Track 1", "Video Track 2 · Overlays & graphics",
                "Audio Track 1 · Narration", "Audio Track 2 · Music + SFX",
            ])
            video_children = tracks[0]["children"]
            self.assertTrue(any(child["OTIO_SCHEMA"].startswith("Transition.") for child in video_children))
            self.assertTrue(tracks[0]["markers"])
            self.assertEqual(
                video_children[0]["media_references"]["DEFAULT_MEDIA"]["target_url"],
                "../scenes/scene.mp4",
            )
            self.assertEqual(tracks[1]["children"][0]["metadata"]["tubeForge"]["text"], "Opening title")
            self.assertTrue(
                tracks[2]["children"][0]["media_references"]["DEFAULT_MEDIA"]["target_url"]
                .endswith("narration_stem.wav")
            )
            fallback_path = final / "fallback.otio"
            with patch.dict("sys.modules", {"opentimelineio": None}):
                otio_export.write_timeline(
                    fallback_path, name="Fallback", scenes=[], durations=[], transitions=[], fps=30,
                )
            self.assertEqual(json.loads(fallback_path.read_text(encoding="utf-8"))["name"], "Fallback")

    def test_channel_lut_and_procedural_model_uploads_are_validated_and_project_scoped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(jobs, "STUDIO_ROOT", root / "studio"):
                lut = jobs.save_channel_lut(
                    "Test Channel", "look.cube",
                    b"LUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n",
                )
                self.assertTrue(Path(lut["path"]).is_file())
                library = jobs.load_channel_library("Test Channel")
                self.assertEqual(library["luts"][0]["name"], "look")
                project = jobs.Project({"id": "model-upload", "settings": {}})
                model_url = jobs.save_procedural_model(project, [
                    ("sample.gltf", b'{"asset":{"version":"2.0"},"buffers":[{"uri":"mesh.bin"}]}'),
                    ("mesh.bin", b"buffer-data"),
                ])
                model_path = project.dir / "remotion_public" / model_url
                self.assertTrue(model_path.is_file())
                with self.assertRaisesRegex(ValueError, "Upload the referenced"):
                    jobs.save_procedural_model(project, [
                        ("sample.gltf", b'{"asset":{"version":"2.0"},"buffers":[{"uri":"missing.bin"}]}'),
                    ])

    def test_project_package_keeps_otio_relative_media_and_audio_stems(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scene = planner.Scene(id="scene-otio", title="Opening", duration=3)
            plan = planner.CinematicPlan(scenes=[scene])
            with patch.object(jobs, "STUDIO_ROOT", root / "studio"):
                project = jobs.Project({
                    "id": "otio-package",
                    "settings": dict(jobs.DEFAULT_SETTINGS),
                    "plan": plan.model_dump(by_alias=True),
                    "scene_state": {scene.id: {}},
                    "outputs": {},
                })
                clip = project.scene_dir(scene.id) / "clip.mp4"
                clip.write_bytes(b"clip")
                project.state(scene.id)["clip"] = str(clip)
                final = project.dir / "final"
                final.mkdir(parents=True)
                files = {
                    "video": final / "film.mp4",
                    "otio": final / "film.otio",
                    "narration_stem": final / "narration_stem.wav",
                    "music_sfx_stem": final / "music_sfx_stem.wav",
                    "master_mix": final / "master_mix.wav",
                }
                for path in files.values():
                    path.write_bytes(b"asset")
                project.data["outputs"] = {key: str(path) for key, path in files.items()}
                archive = jobs.package_zip(project)
            with zipfile.ZipFile(archive) as package:
                names = set(package.namelist())
            self.assertIn("final/film.otio", names)
            self.assertIn("scenes/scene-otio/clip.mp4", names)
            self.assertIn("final/narration_stem.wav", names)
            self.assertIn("final/music_sfx_stem.wav", names)

    def test_vulkan_postprocess_falls_back_to_ffmpeg_when_optional_tools_are_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "assembled.mkv"
            source.write_bytes(b"source video")
            with patch.object(render, "detect_vulkan_tools", return_value={"rife": "", "realesrgan": ""}):
                result, status = render.vulkan_video_postprocess(
                    source, root / "cache", 30, upscale=True, interpolation="rife-2x",
                )
            self.assertEqual(result, source)
            self.assertFalse(status["applied"])
            self.assertTrue(any("using FFmpeg" in message for message in status["warnings"]))

    def test_retention_telemetry_flags_silence_slow_scenes_and_dense_captions(self):
        scenes = [planner.Scene(
            id="paced", duration=20, narration="A deliberately slow opening line with too few words.",
        )]
        words = [
            {"word": "A", "start": 0.2, "end": 0.3},
            {"word": "very", "start": 0.3, "end": 0.4},
            {"word": "fast", "start": 0.4, "end": 0.5},
            {"word": "caption", "start": 0.5, "end": 0.6},
            {"word": "later", "start": 3.0, "end": 3.2},
        ]
        result = timeline_qa.analyze(scenes, words, {}, fps=30)
        kinds = {item["kind"] for item in result["findings"]}
        self.assertIn("dead_air", kinds)
        self.assertIn("slow_pacing", kinds)
        self.assertIn("caption_speed", kinds)
        self.assertTrue(all(item.get("recommendation") for item in result["findings"]))
        self.assertEqual(len(result["timeline"]), 60)


if __name__ == "__main__":
    unittest.main()
