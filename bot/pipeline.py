"""Monthly menu pipeline: photo -> vision LLM -> JSON -> validation -> preview -> save.

Only the admin can trigger it. Nothing reaches students until the admin runs /approve.
Everything except `call_vision_llm` is plain Python and is covered by tests.
"""

from __future__ import annotations

import base64
import calendar
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .menu import MonthMenu, check_menu_structure, item_choices, month_key

logger = logging.getLogger(__name__)

MAX_DISHES_PER_DAY = 8

# The GTU picture also lists a vegan alternative (the "Zeytinyağlı ..." olive-oil dish).
# It is left out of the bot for now; set this to True to include it again.
INCLUDE_VEGAN_OPTION = False
MIN_PLAUSIBLE_KCAL = 20     # lower values are usually a typo in the picture (e.g. 5 instead of 57)
MAX_PLAUSIBLE_KCAL = 1500
TURKISH_CHARS = set("çğıöşüÇĞİÖŞÜ")

# Common cafeteria words that, written without Turkish letters, suggest the AI
# "ASCII-fied" the text (e.g. "corba" instead of "çorba").
ASCII_FIED_WORDS = {
    "corba", "corbasi", "kofte", "koftesi", "borek", "boregi", "sutlac", "yogurt",
    "yogurtlu", "tursu", "tursusu", "kizartma", "sogan", "patlican", "karniyarik",
    "tatlisi", "salatasi", "pilavi", "cacik", "kiymali", "sehriye", "sehriyeli",
    "guvec", "tavugu", "asure", "kasarli", "sutlu", "icli", "sis", "doner",
}

PROMPT_TEMPLATE = """This image is the monthly cafeteria menu of Gebze Technical University (GTU) for {month_name} {year}.

Extract the menu as JSON with exactly this shape and nothing else:
{{
  "{year}-{month:02d}-01": [
    {{"tr": "<dish name in Turkish>", "en": "<English name>", "kcal": <calories as a whole number, or null>}},
    {{"options": [<dish>, <dish>]}}
  ]
}}

Rules:
- Keys are dates in YYYY-MM-DD format, all inside {year}-{month:02d}.
- Include only days that have a menu. Leave out weekends/holidays with no menu.
- List dishes in the order they appear (serving order).
- Keep Turkish names exactly as written, with correct Turkish letters (ç, ğ, ı, ö, ş, ü).
- For "en": give a natural English name. If a dish has no clean English name,
  use the Turkish name plus a short description, e.g. "Karnıyarık (stuffed eggplant with minced meat)".
- Calories are usually printed next to each dish, e.g. "MERCİMEK ÇORBA (209CAL)". Put the number
  in "kcal" and leave it out of the names. If a dish has no calorie value, use null.
- Write names in normal Turkish capitalisation ("Mercimek çorbası", not "MERCİMEK ÇORBASI") and expand
  abbreviations: ZYT/ZEYT. = Zeytinyağlı, ŞEH. = Şehriyeli.
- Students pick ONE of the pilaf (3rd line: pirinç/bulgur pilavı) and the dish after it (4th line: makarna,
  erişte, kuskus, börek...). Put those two in one {{"options": [...]}} entry instead of two separate dishes.
  Don't do this when the 4th line is not a pasta-type dish (e.g. a dessert on a themed day).
{vegan_rule}- Skip day labels that are not dishes (e.g. "EGE GÜNÜ"), and days marked as holidays ("İYİ TATİLLER").
- Do not include prices or notes.
- Return only the JSON object. No markdown, no explanation."""

MONTH_NAMES_EN = list(calendar.month_name)


class PipelineError(Exception):
    """Extraction or parsing failed."""


@dataclass
class ValidationResult:
    menu: MonthMenu = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)      # block /approve
    warnings: list[str] = field(default_factory=list)    # shown, don't block
    missing_weekdays: list[date] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# --- prompt & AI call --------------------------------------------------------


def build_prompt(year: int, month: int) -> str:
    vegan_rule = "" if INCLUDE_VEGAN_OPTION else (
        "- Leave out the olive-oil dishes (\"ZYT\"/\"ZEYT.\" = Zeytinyağlı ...): they are the vegan alternative.\n"
    )
    return PROMPT_TEMPLATE.format(
        year=year, month=month, month_name=MONTH_NAMES_EN[month], vegan_rule=vegan_rule
    )


async def call_vision_llm(image: bytes, media_type: str, prompt: str, api_key: str, model: str) -> str:
    """Send the picture and prompt to a vision-capable LLM and return its text reply.

    Uses the Anthropic Messages API. To use another provider, replace this one function.
    """
    import anthropic  # imported here so the rest of the module works without the SDK

    client = anthropic.AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model=model,
        max_tokens=16000,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.standard_b64encode(image).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )
    return "".join(block.text for block in response.content if block.type == "text")


def parse_response(text: str) -> dict:
    """Turn the model's reply into a dict. Tolerates ```json fences and stray prose."""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    if not cleaned.startswith("{"):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1:
            raise PipelineError("The AI reply contains no JSON object")
        cleaned = cleaned[start : end + 1]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"The AI reply is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PipelineError("The AI reply is not a JSON object")
    return data


async def extract_menu(
    image: bytes, media_type: str, year: int, month: int, api_key: str, model: str
) -> dict:
    """Call the AI and parse its reply. If parsing fails, retry once, then give up."""
    prompt = build_prompt(year, month)
    last_error: Exception | None = None
    for attempt in (1, 2):
        text = await call_vision_llm(image, media_type, prompt, api_key, model)
        try:
            return parse_response(text)
        except PipelineError as exc:
            logger.warning("AI reply could not be parsed (attempt %d): %s", attempt, exc)
            last_error = exc
    raise PipelineError(f"AI extraction failed twice: {last_error}")


# --- validation --------------------------------------------------------------


def _clean_dish(dish: object, where: str, result: ValidationResult) -> dict | None:
    """Trim names and normalise calories. Problems go into result; returns None if unusable."""
    if not isinstance(dish, dict):
        result.errors.append(f"{where} is not an object")
        return None
    tr = str(dish.get("tr") or "").strip()
    en = str(dish.get("en") or "").strip()
    if not tr or not en:
        missing = " and ".join(l for l, v in (("tr", tr), ("en", en)) if not v)
        result.errors.append(f"{where} has an empty '{missing}' name")
        return None
    day = where.split(" ")[0]
    if _looks_ascii_fied(tr):
        result.warnings.append(f"{day}: '{tr}' looks like it is missing Turkish letters")
    clean: dict = {"tr": tr, "en": en}
    kcal = _clean_kcal(dish.get("kcal"))
    if kcal == "invalid":
        result.warnings.append(f"{day}: '{tr}' has an unreadable calorie value {dish.get('kcal')!r} (dropped)")
    elif kcal is not None:
        if kcal < MIN_PLAUSIBLE_KCAL or kcal > MAX_PLAUSIBLE_KCAL:
            result.warnings.append(f"{day}: '{tr}' has {kcal} kcal — check the picture")
        clean["kcal"] = kcal
    return clean


def _clean_kcal(value: object) -> int | None | str:
    """Calories as an int, None if absent, or "invalid" if present but unreadable.
    Accepts 209, "209", "209CAL", "209 kcal"."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return "invalid"
    if isinstance(value, int):
        return value if value > 0 else "invalid"
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str):
        m = re.fullmatch(r"\s*(\d+)\s*(k?cal)?\s*", value, re.IGNORECASE)
        if m and int(m.group(1)) > 0:
            return int(m.group(1))
    return "invalid"


def _looks_ascii_fied(text: str) -> bool:
    words = re.findall(r"[a-z]+", text.lower())
    joined = " ".join(words)
    return any(re.search(rf"\b{re.escape(w)}\b", joined) for w in ASCII_FIED_WORDS)


def validate_menu(data: object, year: int, month: int) -> ValidationResult:
    """Check an extracted (or hand-written) month menu before it can be approved."""
    result = ValidationResult()

    structural = check_menu_structure(data)
    if not isinstance(data, dict):
        result.errors.extend(structural)
        return result

    cleaned: MonthMenu = {}
    for key, dishes in data.items():
        try:
            day = date.fromisoformat(str(key))
        except ValueError:
            result.errors.append(f"'{key}' is not a valid date (YYYY-MM-DD)")
            continue
        if (day.year, day.month) != (year, month):
            result.errors.append(f"{key} is outside {month_key(year, month)}")
            continue
        if not isinstance(dishes, list) or len(dishes) == 0:
            result.errors.append(f"{key} has no dishes")
            continue
        if len(dishes) > MAX_DISHES_PER_DAY:
            result.warnings.append(f"{key} has {len(dishes)} dishes (more than {MAX_DISHES_PER_DAY})")

        clean_dishes = []
        for i, item in enumerate(dishes, 1):
            if isinstance(item, dict) and "options" in item:
                options = item["options"] if isinstance(item["options"], list) else []
                clean_options = [
                    d for j, o in enumerate(options, 1)
                    if (d := _clean_dish(o, f"{key} dish {i} option {j}", result)) is not None
                ]
                if len(clean_options) >= 2:
                    clean_dishes.append({"options": clean_options})
                elif len(clean_options) == 1:
                    result.warnings.append(f"{key} dish {i}: a choice with only one dish, kept as a normal dish")
                    clean_dishes.append(clean_options[0])
                else:
                    result.errors.append(f"{key} dish {i}: choice has no usable dishes")
            else:
                dish = _clean_dish(item, f"{key} dish {i}", result)
                if dish is not None:
                    clean_dishes.append(dish)
        cleaned[day.isoformat()] = clean_dishes

    # Turkish letters should appear somewhere in a month of Turkish dish names.
    all_tr = "".join(d["tr"] for items in cleaned.values() for it in items for d in item_choices(it))
    if all_tr and not (set(all_tr) & TURKISH_CHARS):
        result.warnings.append("No Turkish letters (ç, ğ, ı, ö, ş, ü) anywhere in the Turkish names")

    # Every weekday should be present, or at least listed as missing.
    days_in_month = calendar.monthrange(year, month)[1]
    for d in range(1, days_in_month + 1):
        day = date(year, month, d)
        if day.weekday() < 5 and day.isoformat() not in cleaned:
            result.missing_weekdays.append(day)
    if result.missing_weekdays:
        listed = ", ".join(str(d.day) for d in result.missing_weekdays)
        result.warnings.append(f"Weekdays without a menu: {listed} (holidays?)")

    if not cleaned and not result.errors:
        result.errors.append("The menu is empty")

    result.menu = dict(sorted(cleaned.items()))
    return result


# --- preview & saving --------------------------------------------------------


def format_preview(result: ValidationResult, year: int, month: int) -> str:
    """A readable summary of the whole month for the admin, warnings first."""
    lines = [f"📋 Menu preview for {MONTH_NAMES_EN[month]} {year} ({len(result.menu)} days)", ""]
    if result.errors:
        lines.append(f"❌ {len(result.errors)} error(s) — cannot approve:")
        lines += [f"  • {e}" for e in result.errors]
        lines.append("")
    if result.warnings:
        lines.append(f"⚠️ {len(result.warnings)} warning(s):")
        lines += [f"  • {w}" for w in result.warnings]
        lines.append("")
    if not result.errors and not result.warnings:
        lines += ["✅ No problems found.", ""]

    for key, dishes in result.menu.items():
        day = date.fromisoformat(key)
        lines.append(f"{day.strftime('%a')} {day.day:02d}")
        for item in dishes:
            choices = item_choices(item)
            tr = " OR ".join(c["tr"] for c in choices)
            en = " OR ".join(c["en"] for c in choices)
            kcals = [str(c["kcal"]) if c.get("kcal") else "?" for c in choices]
            kcal = f" — {' / '.join(kcals)} kcal" if any(c.get("kcal") for c in choices) else ""
            lines.append(f"  • {tr}  |  {en}{kcal}")
    lines.append("")
    if result.ok:
        lines.append("Send /approve to publish or /reject to discard.")
    else:
        lines.append("Send /reject, then resend the photo or upload a fixed JSON file.")
    return "\n".join(lines)


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Split long text on line breaks so each part fits in one Telegram message."""
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single huge line
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts or [""]


def save_menu(
    menu_dir: Path, year: int, month: int, menu: MonthMenu, image: bytes | None, image_ext: str = ".jpg"
) -> Path:
    """Write data/menus/YYYY-MM.json (and the original picture, e.g. YYYY-MM.jpg)."""
    menu_dir = Path(menu_dir)
    menu_dir.mkdir(parents=True, exist_ok=True)
    json_path = menu_dir / f"{month_key(year, month)}.json"
    tmp = json_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(json_path)  # atomic: readers never see a half-written file
    if image is not None:
        (menu_dir / f"{month_key(year, month)}{image_ext}").write_bytes(image)
    return json_path


def parse_month_hint(text: str | None) -> tuple[int, int] | None:
    """Find 'YYYY-MM' in a caption or file name, e.g. '2026-10' or '2026-10.json'."""
    if not text:
        return None
    m = re.search(r"(20\d\d)-(0[1-9]|1[0-2])", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def default_target_month(today: date) -> tuple[int, int]:
    """Without a caption, assume a picture sent after the 20th is next month's menu."""
    if today.day >= 20:
        return (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return today.year, today.month
