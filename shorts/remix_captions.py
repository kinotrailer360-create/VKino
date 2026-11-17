"""Short, timed captions and editorial ribbons for uploaded video remixes."""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any


def estimated_words(phrases: list[dict[str, Any]], total: float) -> list[dict[str, Any]]:
    """Fallback to the original speech intervals, never grouped scene timings."""
    out: list[dict[str, Any]] = []
    for phrase in phrases:
        tokens = str(phrase.get("text") or phrase.get("ru") or "").split()
        start = max(0.0, float(phrase.get("start") or 0))
        end = min(total, float(phrase.get("end") or start))
        if not tokens or not math.isfinite(start + end) or end <= start:
            continue
        weight = sum(len(word) + 1 for word in tokens)
        at = start
        for index, word in enumerate(tokens):
            until = end if index == len(tokens) - 1 else at + (end - start) * (len(word) + 1) / weight
            out.append({"w": word, "s": at, "e": until})
            at = until
    return out


def caption_events(words: list[dict[str, Any]], total: float) -> list[dict[str, Any]]:
    """One or two words at a time; clear during pauses and cap stale timings."""
    valid = []
    for word in words:
        try:
            start, end = float(word["s"]), float(word["e"])
        except (KeyError, ValueError, TypeError):
            continue
        text = str(word.get("w") or "").strip()
        if text and math.isfinite(start + end) and 0 <= start < total and end > start:
            valid.append({"text": text, "start": start, "end": min(end, total, start + 1.2)})
    valid.sort(key=lambda item: item["start"])
    events: list[dict[str, Any]] = []
    index = 0
    while index < len(valid):
        event = dict(valid[index])
        index += 1
        if index < len(valid):
            following = valid[index]
            if (not re.search(r"[.!?:;]$", event["text"])
                    and 0 <= following["start"] - event["end"] <= 0.18
                    and following["end"] - event["start"] <= 0.85
                    and len((event["text"] + " " + following["text"]).split()) <= 2):
                event["text"] += " " + following["text"]
                event["end"] = following["end"]
                index += 1
        if index < len(valid):
            event["end"] = min(event["end"], valid[index]["start"])
        if event["end"] > event["start"]:
            events.append(event)
    return events


def fallback_headline(text: str) -> str:
    """Use the author's own words when editorial AI is unavailable."""
    first = re.split(r"[.!?]", str(text or ""), maxsplit=1)[0]
    words = first.split()[:6]
    return " ".join(words).strip(" ,:;—-")[:48].upper()


def _time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    return f"{centiseconds // 360000}:{centiseconds // 6000 % 60:02d}:{centiseconds // 100 % 60:02d}.{centiseconds % 100:02d}"


def _escape(text: str) -> str:
    return str(text).replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def write_ass(path: Path, events: list[dict[str, Any]], scenes: list[dict[str, Any]]) -> None:
    """Render all words with one libass input; no per-word image inputs."""
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 720", "PlayResY: 1280",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Words,Oswald,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,5,40,40,0,1",
        "Style: Ribbon,Oswald,34,&H00101010,&H00101010,&H003ACCF8,&H003ACCF8,-1,0,0,0,100,100,0,0,3,9,0,5,30,30,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for scene in scenes:
        headline = _escape(str(scene.get("headline") or "")).upper()
        if not headline:
            continue
        size = 34 if len(headline) <= 34 else 28
        lines.append(
            f"Dialogue: 1,{_time(float(scene['phrase_start']))},{_time(float(scene['phrase_end']))},Ribbon,,0,0,0,,"
            + r"{\pos(360,174)\fs" + str(size) + "}" + headline
        )
    for event in events:
        text = _escape(event["text"]).upper()
        lines.append(
            f"Dialogue: 2,{_time(event['start'])},{_time(event['end'])},Words,,0,0,0,,"
            + r"{\pos(360,790)\fad(35,35)}" + text
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
