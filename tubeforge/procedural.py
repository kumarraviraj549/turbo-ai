"""Strict, backwards-compatible props preparation for the optional Remotion scene engine."""
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable

TEMPLATES = (
    "3d_bar_chart", "3d_stat_callout", "comparison_split", "timeline_journey", "process_flow",
    "quote_evidence_card", "2d_character_explainer", "kinetic_hook_title", "map_route_animator",
    "3d_model_showcase",
)
MODEL_PUBLIC_DIR = Path(__file__).resolve().parents[1] / "remotion-engine" / "public"


def _text(value: Any, default: str, limit: int = 200) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("Procedural text fields must be strings.")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"Procedural text fields are limited to {limit} characters.")
    return value or default


def _check_keys(value: Dict[str, Any], allowed: set, name: str) -> None:
    extra = set(value) - allowed
    if extra:
        raise ValueError(f"Unexpected {name} field(s): {', '.join(sorted(extra))}.")


def _metric_rows(value: Any) -> list:
    rows = []
    if not isinstance(value, list) or len(value) > 5:
        raise ValueError("Comparison metrics must be a list of at most five items.")
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each comparison metric must be a JSON object.")
        _check_keys(item, {"label", "value"}, "metric")
        metric_value = item.get("value", "")
        if not isinstance(metric_value, (str, int, float)) or isinstance(metric_value, bool) \
                or isinstance(metric_value, float) and not math.isfinite(metric_value):
            raise ValueError("Comparison metric values must be strings or numbers.")
        rows.append({"label": _text(item.get("label"), "Metric", 80), "value": metric_value})
    return rows


def scene_props(scene, settings: Dict[str, Any], duration: float, research_sources: Iterable[str],
                model_public_dir: Path = MODEL_PUBLIC_DIR) -> Dict[str, Any]:
    """Build the exact shape validated by the isolated Zod schema before invoking Remotion."""
    data = scene.procedural_props
    if not isinstance(data, dict):
        raise ValueError("Procedural template props must be a JSON object.")
    if scene.procedural_template not in TEMPLATES:
        raise ValueError(f"Unsupported procedural template: {scene.procedural_template!r}.")
    template = scene.procedural_template
    allowed_fields = {
        "3d_bar_chart": {"subtitle", "unit", "bars"},
        "3d_stat_callout": {"value", "prefix", "suffix", "subtitle"},
        "comparison_split": {"left", "right", "winner"},
        "timeline_journey": {"milestones"},
        "process_flow": {"steps"},
        "quote_evidence_card": {"quote", "source", "context"},
        "2d_character_explainer": {"characterName", "characterSvg", "emotion", "mouthStateTimeline"},
        "kinetic_hook_title": {"hookText", "kicker"},
        "map_route_animator": {"cities", "routes", "highlightCountries", "mapCenter", "zoom"},
        "3d_model_showcase": {"modelUrl", "calloutTitle"},
    }
    _check_keys(data, allowed_fields[template], "template props")
    props: Dict[str, Any] = {
        "template": template,
        "durationInSeconds": max(1.0, min(30.0, float(duration))),
        "aspect": "9:16" if settings.get("aspect", "").startswith("9:16") else "16:9",
        "channelName": _text(settings.get("channel_name"), "TubeForge", 100),
        "title": _text(scene.title, "Key insight", 160),
        "brand": {
            "primary": _text(settings.get("brand_primary_color"), "#8B5CF6", 7),
            "secondary": _text(settings.get("brand_secondary_color"), "#22D3EE", 7),
            "accent": _text(settings.get("brand_accent_color"), "#F472B6", 7),
        },
    }
    for key, fallback in (("primary", "#8B5CF6"), ("secondary", "#22D3EE"), ("accent", "#F472B6")):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", props["brand"][key]):
            props["brand"][key] = fallback

    if template == "3d_bar_chart":
        bars = []
        if "bars" in data:
            if not isinstance(data["bars"], list) or len(data["bars"]) > 8:
                raise ValueError("Chart bars must be a list of at most eight items.")
            for item in data["bars"]:
                if not isinstance(item, dict):
                    raise ValueError("Each chart bar must be a JSON object.")
                _check_keys(item, {"label", "value"}, "chart bar")
                if not isinstance(item.get("value"), (int, float)) or isinstance(item.get("value"), bool) \
                        or isinstance(item.get("value"), float) and not math.isfinite(item["value"]):
                    raise ValueError("Each chart bar needs a numeric value.")
                bars.append({"label": _text(item.get("label"), f"Point {len(bars) + 1}", 40),
                             "value": float(item["value"])})
        if not bars:
            numbers = re.findall(r"(?<!\w)-?\d+(?:,\d{3})*(?:\.\d+)?", scene.narration or "")
            bars = [{"label": f"Point {i + 1}", "value": float(value.replace(",", ""))}
                    for i, value in enumerate(numbers[:4])]
        props.update({
            "subtitle": _text(data.get("subtitle"), "A measurable shift over time"),
            "unit": _text(data.get("unit"), "", 20),
            "bars": bars or [{"label": "Insight", "value": 1}],
        })
    elif template == "3d_stat_callout":
        value = data.get("value", 0)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            if "value" in data:
                raise ValueError("Stat callout value must be numeric.")
            match = re.search(r"(?<!\w)-?\d+(?:,\d{3})*(?:\.\d+)?", scene.narration or "")
            value = float(match.group(0).replace(",", "")) if match else 0
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Stat callout value must be finite.")
        props.update({
            "value": float(value),
            "prefix": _text(data.get("prefix"), "", 12),
            "suffix": _text(data.get("suffix"), "", 12),
            "subtitle": _text(data.get("subtitle"), scene.narration or "A key figure"),
        })
    elif template == "comparison_split":
        left = data.get("left") if isinstance(data.get("left"), dict) else {}
        right = data.get("right") if isinstance(data.get("right"), dict) else {}
        if data.get("left") is not None and not isinstance(data.get("left"), dict):
            raise ValueError("The left comparison panel must be a JSON object.")
        if data.get("right") is not None and not isinstance(data.get("right"), dict):
            raise ValueError("The right comparison panel must be a JSON object.")
        _check_keys(left, {"label", "metrics"}, "left comparison panel")
        _check_keys(right, {"label", "metrics"}, "right comparison panel")
        props.update({
            "left": {"label": _text(left.get("label"), "Option A", 60),
                     "metrics": _metric_rows(left.get("metrics", []))},
            "right": {"label": _text(right.get("label"), "Option B", 60),
                      "metrics": _metric_rows(right.get("metrics", []))},
            "winner": data.get("winner") if data.get("winner") in ("A", "B") else "A",
        })
    elif template == "timeline_journey":
        milestones = []
        if "milestones" in data:
            if not isinstance(data["milestones"], list) or len(data["milestones"]) > 8:
                raise ValueError("Timeline milestones must be a list of at most eight items.")
            for item in data["milestones"]:
                if not isinstance(item, dict):
                    raise ValueError("Each timeline milestone must be a JSON object.")
                _check_keys(item, {"year", "label", "caption"}, "timeline milestone")
                milestones.append({
                    "year": _text(item.get("year"), "Milestone", 20),
                    "label": _text(item.get("label"), "Key event", 60),
                    "caption": _text(item.get("caption"), "", 120),
                })
        if len(milestones) < 2:
            milestones = [{"year": "Then", "label": "Starting point", "caption": ""},
                          {"year": "Now", "label": _text(scene.title, "Current point", 60), "caption": ""}]
        props["milestones"] = milestones
    elif template == "process_flow":
        steps = []
        if "steps" in data:
            if not isinstance(data["steps"], list) or len(data["steps"]) > 8:
                raise ValueError("Process steps must be a list of at most eight items.")
            for item in data["steps"]:
                if not isinstance(item, dict):
                    raise ValueError("Each process step must be a JSON object.")
                _check_keys(item, {"label", "caption"}, "process step")
                steps.append({
                    "label": _text(item.get("label"), f"Step {len(steps) + 1}", 60),
                    "caption": _text(item.get("caption"), "", 120),
                })
        props["steps"] = steps if len(steps) >= 2 else [
            {"label": "Discover", "caption": ""},
            {"label": _text(scene.title, "Outcome", 60), "caption": ""},
        ]
    elif template == "quote_evidence_card":
        source = _text(data.get("source"), "", 300)
        known_sources = [str(item) for item in research_sources if item]
        if source not in known_sources:
            raise ValueError("Evidence card citations must match a source in the Story research sources.")
        props.update({
            "quote": _text(data.get("quote"), scene.narration or "Verified story evidence", 500),
            "source": source,
            "context": _text(data.get("context"), "", 200),
        })
    elif template == "2d_character_explainer":
        emotion = data.get("emotion", "explaining")
        if emotion not in ("neutral", "happy", "shocked", "explaining", "serious"):
            raise ValueError("Unsupported 2D character emotion.")
        timeline = data.get("mouthStateTimeline", [])
        if not isinstance(timeline, list):
            raise ValueError("mouthStateTimeline must be a list.")
        clean_timeline = []
        for state in timeline:
            if not isinstance(state, dict):
                raise ValueError("Each mouth state must be an object.")
            mouth = state.get("mouth")
            if mouth not in ("closed", "half", "open", "wide"):
                raise ValueError("Unsupported mouth state.")
            start = int(state.get("startFrame", 0))
            end = int(state.get("endFrame", start))
            if start < 0 or end < start:
                raise ValueError("Invalid mouth-state frame range.")
            clean_timeline.append({"startFrame": start, "endFrame": end, "mouth": mouth})
        props.update({
            "characterName": _text(data.get("characterName"), scene.title or "Explainer", 80),
            "emotion": emotion,
            "mouthStateTimeline": clean_timeline,
        })
        if isinstance(data.get("characterSvg"), str):
            props["characterSvg"] = data["characterSvg"][:3000]
    elif template == "kinetic_hook_title":
        props.update({
            "hookText": _text(data.get("hookText"), scene.title or "The Hidden Truth", 160),
            "kicker": _text(data.get("kicker"), "WATCH THIS", 80),
        })
    elif template == "map_route_animator":
        cities = data.get("cities", [
            {"name": "Origin", "longitude": -73.9, "latitude": 40.7},
            {"name": "Destination", "longitude": 2.3, "latitude": 48.9},
        ])
        if not isinstance(cities, list) or not 2 <= len(cities) <= 12:
            raise ValueError("A route map needs between two and twelve cities.")
        clean_cities = []
        city_names = set()
        for city in cities:
            if not isinstance(city, dict):
                raise ValueError("Each map city must be a JSON object.")
            _check_keys(city, {"name", "longitude", "latitude"}, "map city")
            name = _text(city.get("name"), "City", 60)
            longitude, latitude = city.get("longitude"), city.get("latitude")
            if (not isinstance(longitude, (int, float)) or isinstance(longitude, bool)
                    or not math.isfinite(float(longitude)) or not -180 <= longitude <= 180):
                raise ValueError(f"Longitude for {name} must be between -180 and 180.")
            if (not isinstance(latitude, (int, float)) or isinstance(latitude, bool)
                    or not math.isfinite(float(latitude)) or not -90 <= latitude <= 90):
                raise ValueError(f"Latitude for {name} must be between -90 and 90.")
            if name in city_names:
                raise ValueError("Map city names must be unique.")
            city_names.add(name)
            clean_cities.append({"name": name, "longitude": float(longitude), "latitude": float(latitude)})
        routes = data.get("routes", [{"from": clean_cities[0]["name"], "to": clean_cities[1]["name"], "label": ""}])
        if not isinstance(routes, list) or not 1 <= len(routes) <= 12:
            raise ValueError("A route map needs between one and twelve routes.")
        clean_routes = []
        for route in routes:
            if not isinstance(route, dict):
                raise ValueError("Each map route must be a JSON object.")
            _check_keys(route, {"from", "to", "label"}, "map route")
            origin = _text(route.get("from"), "", 60)
            destination = _text(route.get("to"), "", 60)
            if origin not in city_names or destination not in city_names or origin == destination:
                raise ValueError("Each map route must connect two different listed cities.")
            clean_routes.append({"from": origin, "to": destination, "label": _text(route.get("label"), "", 80)})
        highlights = data.get("highlightCountries", [])
        if (not isinstance(highlights, list) or len(highlights) > 20
                or any(not isinstance(code, (str, int)) or not re.fullmatch(r"\d{1,3}", str(code))
                       for code in highlights)):
            raise ValueError("Country highlights must use up to 20 numeric ISO-3166 country codes.")
        center = data.get("mapCenter", {"longitude": 0.0, "latitude": 20.0})
        if not isinstance(center, dict):
            raise ValueError("mapCenter must contain longitude and latitude.")
        _check_keys(center, {"longitude", "latitude"}, "map center")
        center_lon, center_lat = center.get("longitude", 0), center.get("latitude", 20)
        if (not isinstance(center_lon, (int, float)) or isinstance(center_lon, bool)
                or not math.isfinite(float(center_lon)) or not -180 <= center_lon <= 180
                or not isinstance(center_lat, (int, float)) or isinstance(center_lat, bool)
                or not math.isfinite(float(center_lat)) or not -90 <= center_lat <= 90):
            raise ValueError("mapCenter coordinates are outside valid longitude/latitude ranges.")
        zoom = data.get("zoom", 1.0)
        if (not isinstance(zoom, (int, float)) or isinstance(zoom, bool)
                or not math.isfinite(float(zoom)) or not 1 <= zoom <= 4):
            raise ValueError("Map zoom must be between 1 and 4.")
        props.update({
            "cities": clean_cities,
            "routes": clean_routes,
            "highlightCountries": [str(code) for code in highlights],
            "mapCenter": {"longitude": float(center_lon), "latitude": float(center_lat)},
            "zoom": float(zoom),
        })
    elif template == "3d_model_showcase":
        model_url = _text(data.get("modelUrl"), "", 240).replace("\\", "/")
        if model_url:
            model_path = Path(model_url)
            public_dir = Path(model_public_dir).resolve()
            target = (public_dir / model_path).resolve()
            try:
                target.relative_to(public_dir)
            except ValueError as exc:
                raise ValueError("Model paths must stay inside the Remotion public/models directory.") from exc
            if (model_path.is_absolute() or not model_url.startswith("models/")
                    or model_path.suffix.lower() not in (".glb", ".gltf")):
                raise ValueError("Model URL must be a relative .glb or .gltf path under public/models.")
            if not target.is_file():
                raise ValueError(f"3D model file was not found in the Remotion public directory: {model_url}")
        props.update({"modelUrl": model_url,
                      "calloutTitle": _text(data.get("calloutTitle"), scene.title or "Featured model", 120)})
    return props
