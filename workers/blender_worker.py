"""Safe template-only Blender scene flythrough worker; arbitrary scripts are never accepted."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict

_BLENDER_SOURCE = r'''
import bpy
import json
import math
import sys
from mathutils import Vector

args = sys.argv[sys.argv.index("--") + 1:]
cfg = json.loads(open(args[0], encoding="utf-8").read())
scene = bpy.context.scene
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
scene.render.resolution_x = cfg["width"]
scene.render.resolution_y = cfg["height"]
scene.render.resolution_percentage = 100
scene.render.fps = cfg["fps"]
scene.frame_start = 1
scene.frame_end = max(2, round(cfg["duration"] * cfg["fps"]))
scene.render.image_settings.file_format = "FFMPEG"
scene.render.ffmpeg.format = "MPEG4"
scene.render.ffmpeg.codec = "H264"
scene.render.filepath = cfg["output"]
scene.render.film_transparent = False
scene.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items.keys() else "BLENDER_EEVEE"
scene.world.color = (0.008, 0.012, 0.025)
target = Vector((0, 0, 0))
model_path = cfg.get("model_path")
if model_path:
    bpy.ops.import_scene.gltf(filepath=model_path)
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if not meshes:
        raise RuntimeError("The uploaded GLB/GLTF contains no mesh objects.")
    for obj in meshes:
        obj.location = (0, 0, 0)
        obj.scale = (cfg["model_scale"],) * 3
else:
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=1.4, location=target)
    bpy.context.object.name = "TubeForge Showcase Object"
    meshes = [bpy.context.object]
for obj in meshes:
    material = bpy.data.materials.new(name="TubeForge Showcase Material")
    material.diffuse_color = (0.22, 0.42, 0.78, 1)
    obj.data.materials.clear()
    obj.data.materials.append(material)
bpy.ops.mesh.primitive_plane_add(size=200, location=(0, 0, -2))
floor = bpy.context.object
floor_mat = bpy.data.materials.new(name="Studio Floor")
floor_mat.diffuse_color = (0.025, 0.04, 0.07, 1)
floor.data.materials.append(floor_mat)
for name, location, energy, size in [
    ("Key", (5, -5, 7), 1200, 5), ("Fill", (-5, -2, 4), 650, 6), ("Rim", (0, 5, 6), 1000, 4)
]:
    bpy.ops.object.light_add(type="AREA", location=location)
    light = bpy.context.object
    light.name = name
    light.data.energy = energy
    light.data.shape = "DISK"
    light.data.size = size
    light.rotation_euler = (Vector((0, 0, 0)) - light.location).to_track_quat("-Z", "Y").to_euler()
bpy.ops.object.camera_add(location=(6, -8, 4))
camera = bpy.context.object
camera.data.lens = 48
camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()
scene.camera = camera
frames = scene.frame_end
for frame, angle in ((1, 0), (frames, 2 * math.pi)):
    camera.location = (7 * math.cos(angle), 7 * math.sin(angle), 3.2 + 0.4 * math.sin(angle / 2))
    camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera.keyframe_insert(data_path="location", frame=frame)
    camera.keyframe_insert(data_path="rotation_euler", frame=frame)
for curve in camera.animation_data.action.fcurves:
    for key in curve.keyframe_points:
        key.interpolation = "BEZIER"
scene.view_settings.view_transform = "AgX"
scene.render.image_settings.color_mode = "RGB"
scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
scene.render.ffmpeg.ffmpeg_preset = "GOOD"
bpy.ops.render.render(animation=True)
'''


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_config(config: Dict) -> Dict:
    if not isinstance(config, dict):
        raise ValueError("Blender worker configuration must be an object.")
    project_root = Path(config["project_root"]).resolve()
    model_path = Path(config["model_path"]).resolve() if config.get("model_path") else None
    if model_path:
        try:
            model_path.relative_to(project_root)
        except ValueError as exc:
            raise ValueError("Blender GLB/GLTF assets must be inside the current project directory.") from exc
        if not model_path.is_file() or model_path.suffix.lower() not in (".glb", ".gltf"):
            raise ValueError("Blender flythrough model must be an existing .glb or .gltf file.")
    return {
        "project_root": str(project_root),
        "model_path": str(model_path) if model_path else "",
        "output": str(Path(config["output"]).resolve()),
        "duration": max(1.0, min(60.0, float(config.get("duration", 8.0)))),
        "width": max(320, min(3840, int(config.get("width", 1920)))),
        "height": max(320, min(2160, int(config.get("height", 1080)))),
        "fps": max(15, min(60, int(config.get("fps", 30)))),
        "model_scale": max(0.01, min(100.0, float(config.get("model_scale", 1.0)))),
    }


def run(config: Dict) -> Dict:
    cfg = validate_config(config)
    output = Path(cfg["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    model_digest = _sha256(Path(cfg["model_path"])) if cfg["model_path"] else "primitive"
    signature = hashlib.sha256(
        (json.dumps(cfg, sort_keys=True, separators=(",", ":")) + model_digest).encode("utf-8")
    ).hexdigest()
    sidecar = output.with_suffix(".json")
    if output.is_file() and sidecar.is_file():
        try:
            if json.loads(sidecar.read_text(encoding="utf-8")).get("cache_key") == signature:
                return {"ok": True, "cached": True, "output": str(output), "cache_key": signature}
        except (OSError, ValueError):
            pass
    executable = os.getenv("BLENDER_EXECUTABLE", "").strip() or shutil.which("blender")
    if not executable or not Path(executable).is_file():
        raise RuntimeError("Blender executable is unavailable; set BLENDER_EXECUTABLE or add blender to PATH.")
    with tempfile.TemporaryDirectory(prefix="tf-blender-", dir=output.parent) as temp:
        work = Path(temp)
        config_path = work / "config.json"
        config_path.write_text(json.dumps(cfg), encoding="utf-8")
        script = work / "safe_flythrough.py"
        script.write_text(_BLENDER_SOURCE, encoding="utf-8")
        temporary = output.with_name(output.stem + ".part.mp4")
        cfg["output"] = str(temporary)
        config_path.write_text(json.dumps(cfg), encoding="utf-8")
        result = subprocess.run(
            [executable, "--background", "--python", str(script), "--", str(config_path)],
            cwd=work, capture_output=True, text=True, timeout=max(900, cfg["duration"] * 180), check=False,
        )
        if result.returncode or not temporary.is_file() or temporary.stat().st_size < 10_000:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"Blender flythrough failed: {(result.stderr or result.stdout)[-1400:]}")
        temporary.replace(output)
    sidecar.write_text(json.dumps({"cache_key": signature, "config": cfg}, indent=2), encoding="utf-8")
    return {"ok": True, "cached": False, "output": str(output), "cache_key": signature}


def main() -> None:
    try:
        result = run(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:  # noqa: BLE001 - surfaced to the render job
        print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
