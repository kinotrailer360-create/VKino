"""Import a dated KinoMir360 content package from a Word attachment."""

from __future__ import annotations

import io
import re
from datetime import date, timedelta
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from .daily import MONTHS

MAX_FILE_SIZE = 2 * 1024 * 1024
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
WEEKDAYS = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")
DAY_HEADING = re.compile(r"^(ПН|ВТ|СР|ЧТ|ПТ|СБ|ВС)\s*•\s*(.+)$")


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def paragraphs(content: bytes) -> list[str]:
    if len(content) > MAX_FILE_SIZE:
        raise ValueError("Файл превышает 2 МБ.")
    try:
        with ZipFile(io.BytesIO(content)) as doc:
            info = doc.getinfo("word/document.xml")
            if info.file_size > 4 * MAX_FILE_SIZE:
                raise ValueError("Текст внутри документа слишком большой.")
            xml = doc.read(info)
        if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
            raise ValueError("Документ содержит неподдерживаемую структуру XML.")
        root = ElementTree.fromstring(xml)
    except (BadZipFile, KeyError, ElementTree.ParseError, RuntimeError) as exc:
        raise ValueError("Не удалось прочитать документ Word.") from exc
    rows = []
    for para in root.iter(WORD + "p"):
        text = "".join((node.text or "") if node.tag == WORD + "t" else "\n" if node.tag == WORD + "br" else ""
                       for node in para.iter()).strip()
        if text:
            rows.append(text)
    return rows


def parse(content: bytes) -> tuple[date, dict[str, dict]]:
    rows = paragraphs(content)
    months = {name.lower(): i for i, name in enumerate(MONTHS, 1)}
    header = next((row for row in rows if row.startswith("Неделя публикаций:")), "")
    period = re.search(r"(\d{1,2})(?:\s+([а-яё]+))?\s*[–—-]\s*(\d{1,2})\s+([а-яё]+)\s+(\d{4})",
                       header.lower())
    if not period or period[4] not in months or (period[2] and period[2] not in months):
        raise ValueError("Не нашёл неделю публикаций. Ожидается строка «Неделя публикаций: 23–29 ноября 2026».")
    first_month, last_month = months[period[2] or period[4]], months[period[4]]
    year = int(period[5])
    start = date(year - int(first_month > last_month), first_month, int(period[1]))
    end = date(year, last_month, int(period[3]))
    if start != week_start(start) or end - start != timedelta(days=6):
        raise ValueError("Укажите одну неделю, с понедельника по воскресенье.")
    headings = [(i, m) for i, row in enumerate(rows) if (m := DAY_HEADING.fullmatch(row))]
    footer = next((i for i, row in enumerate(rows) if row.startswith("Факт-чек и прямые")), len(rows))
    headings = [(i, m) for i, m in headings if i < footer]
    if not headings:
        raise ValueError("Не нашёл разделы с темами. Ожидаются заголовки вида «ПН • Название».")
    days = {}
    for j, (i, heading) in enumerate(headings):
        day = (start + timedelta(days=WEEKDAYS.index(heading[1]))).isoformat()
        if day in days:
            raise ValueError(f"Повторяется раздел за {day}.")
        content_rows = rows[i + 1:headings[j + 1][0] if j + 1 < len(headings) else footer]
        days[day] = _entry(heading[2], content_rows)
    if footer < len(rows):
        days[max(days)]["notes"] = rows[footer:]
    return start, days


def _entry(heading: str, rows: list[str]) -> dict:
    fields = {key: next((row[len(label):].strip() for row in rows if row.startswith(label)), "")
              for key, label in (("description", "Описание:"), ("hashtags", "Хэштеги:"))}
    if heading.startswith("Длинный ролик:"):
        title = next((row.removeprefix("Заголовок:").strip().removeprefix("«").removesuffix("»")
                      for row in rows if row.startswith("Заголовок:")), heading.split(":", 1)[1].strip())
        starts = [i for i, row in enumerate(rows) if row.startswith("ОЗВУЧКА:")]
        if not starts or not starts[0]:
            raise ValueError(f"В разделе «{title}» нет текста озвучки.")
        sections = [{"heading": rows[i - 1], "narration": rows[i].removeprefix("ОЗВУЧКА:").strip(),
                     "visuals": rows[i + 1:starts[j + 1] - 1 if j + 1 < len(starts) else len(rows)]}
                    for j, i in enumerate(starts)]
        return {"title": title, "sections": sections, **fields}
    voice = next((i for i, row in enumerate(rows) if row.startswith("Озвучка")), -1)
    visual = next((i for i, row in enumerate(rows) if row.startswith("Карта видеоряда")), len(rows))
    lines = [row for row in rows[voice + 1:visual] if not row.startswith(("Описание:", "Хэштеги:"))]
    if voice < 0 or not lines:
        raise ValueError(f"В разделе «{heading}» нет текста озвучки.")
    return {"title": heading, "narration": "\n".join(lines), "visuals": rows[visual + 1:], **fields}
