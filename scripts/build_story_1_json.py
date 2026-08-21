#!/usr/bin/env python3
"""Собрать static/stories/story-1.json из «Сюжет 1.md»."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MD_PATH = ROOT / "Сюжет 1.md"
OUT_PATH = ROOT / "static" / "stories" / "story-1.json"

ALL_RESOURCES = [
    "камень", "дерево", "железо", "скот", "овощи", "рабы", "золото", "зерно", "рыба",
]
ALL_BUILDINGS = [
    "Лесоповал", "Каменоломня", "Теплицы", "Трактир", "Посевные поля", "Рыболовня",
    "Кузнечная", "Ферма", "Постоялый двор", "Куртизанские палатки", "Золотой рудник",
]


def _parse_coef(raw: str) -> float:
    raw = raw.strip().replace(",", ".")
    return float(raw)


BUILDING_ALIASES = {
    "Кузничная": "Кузнечная",
}


def _default_round() -> dict:
    return {
        "resources": {r: {"coef": 1.0, "comment": ""} for r in ALL_RESOURCES},
        "buildings": {b: {"coef": 1.0, "comment": ""} for b in ALL_BUILDINGS},
        "highlight_resources": [],
        "highlight_buildings": [],
    }


def _parse_highlight_csv(raw: str, canonical: list[str], *, lowercase: bool = False) -> list[str]:
    raw = (raw or "").strip()
    if not raw or raw in ("—", "-", "–"):
        return []
    result: list[str] = []
    canonical_set = set(canonical)
    for part in re.split(r",\s*", raw):
        name = part.strip()
        if not name:
            continue
        if lowercase:
            key = name.lower()
            if key in canonical_set:
                result.append(key)
        else:
            key = BUILDING_ALIASES.get(name, name)
            if key in canonical_set:
                result.append(key)
    return result


def _parse_highlights(body: str) -> tuple[list[str], list[str]]:
    highlight_resources: list[str] = []
    highlight_buildings: list[str] = []
    in_highlights = False
    for line in body.splitlines():
        if line.startswith("### Подсветка"):
            in_highlights = True
            continue
        if in_highlights:
            if line.startswith("###") or line.startswith("##"):
                break
            res_match = re.match(r"^\*\*Ресурсы:\*\*\s*(.+)$", line.strip())
            bld_match = re.match(r"^\*\*Объекты:\*\*\s*(.+)$", line.strip())
            if res_match:
                highlight_resources = _parse_highlight_csv(
                    res_match.group(1), ALL_RESOURCES, lowercase=True
                )
            if bld_match:
                highlight_buildings = _parse_highlight_csv(
                    bld_match.group(1), ALL_BUILDINGS
                )
    return highlight_resources, highlight_buildings


def _parse_overview_events(text: str) -> dict[str, str]:
    events: dict[str, str] = {}
    in_table = False
    for line in text.splitlines():
        if line.strip().startswith("| Раунд |"):
            in_table = True
            continue
        if not in_table:
            continue
        if line.strip().startswith("|---"):
            continue
        if not line.strip().startswith("|"):
            if events:
                break
            continue
        parts = [p.strip() for p in line.strip().strip("|").split("|")]
        if len(parts) < 2:
            continue
        m = re.match(r"^(\d+)$", parts[0])
        if m:
            events[m.group(1)] = parts[1]
    return events


def _parse_round_sections(text: str) -> dict[str, dict]:
    rounds: dict[str, dict] = {}
    parts = re.split(r"\n## Раунд (\d+)", text)
    # parts[0] = header, then pairs (num, body)
    i = 1
    while i + 1 < len(parts):
        num = parts[i]
        body = parts[i + 1]
        i += 2
        rd = _default_round()
        section = None
        for line in body.splitlines():
            if line.startswith("### Ресурсы"):
                section = "resources"
                continue
            if line.startswith("### Объекты"):
                section = "buildings"
                continue
            if not line.strip().startswith("|"):
                continue
            if "Ресурс" in line or "Объект" in line or line.strip().startswith("|---"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2:
                continue
            name, coef_raw = cells[0], cells[1]
            comment = cells[3] if section == "resources" and len(cells) > 3 else (
                cells[2] if section == "buildings" and len(cells) > 2 else ""
            )
            try:
                coef = _parse_coef(coef_raw)
            except ValueError:
                continue
            if section == "resources" and name in rd["resources"]:
                rd["resources"][name] = {"coef": coef, "comment": comment}
            elif section == "buildings" and name in rd["buildings"]:
                rd["buildings"][name] = {"coef": coef, "comment": comment}
        highlight_resources, highlight_buildings = _parse_highlights(body)
        rd["highlight_resources"] = highlight_resources
        rd["highlight_buildings"] = highlight_buildings
        rounds[num] = rd
    return rounds


def main() -> None:
    text = MD_PATH.read_text(encoding="utf-8")
    overview = _parse_overview_events(text)
    rounds = _parse_round_sections(text)
    for n in range(1, 11):
        key = str(n)
        if key not in rounds:
            rounds[key] = _default_round()
        if key in overview:
            rounds[key]["event_label"] = overview[key]

    payload = {
        "id": "story-1",
        "title": "Сюжет 1",
        "rounds": rounds,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
