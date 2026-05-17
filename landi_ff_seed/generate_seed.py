from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests


ROOT = Path(__file__).resolve().parent
PRIMARY_SOURCE_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.ics",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.ics",
    "https://nfs.faireconomy.media/ff_calendar_week3.ics",
]
FALLBACK_SOURCE_URLS = [
    "https://raw.githubusercontent.com/tashton13/forex-factory-high-impact/main/src/enhanced_economic_calendar.ics",
    "https://raw.githubusercontent.com/tashton13/forex-factory-high-impact/main/src/high_impact_only.ics",
]
FOREX_FACTORY_CALENDAR_URL = "https://www.forexfactory.com/calendar?range={start}-{end}"
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SLOT_COUNT = 9
BASE_ROW_DATE = "20200101T000000"
ALPHABET = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-/:"
BASE = len(ALPHABET)
CHARS_PER_FIELD = 9
SYMBOLS = [f"LANDI_FF_SLOT_{index}" for index in range(1, SLOT_COUNT + 1)]
CURRENCIES = ("AUD", "CAD", "CHF", "CNY", "EUR", "GBP", "JPY", "NZD", "USD")
COUNTRY_TO_CURRENCY = {
    "AU": "AUD",
    "CA": "CAD",
    "CH": "CNY",
    "CN": "CNY",
    "EU": "EUR",
    "EZ": "EUR",
    "FR": "EUR",
    "GE": "EUR",
    "IT": "EUR",
    "SP": "EUR",
    "UK": "GBP",
    "GB": "GBP",
    "JP": "JPY",
    "NZ": "NZD",
    "SW": "CHF",
    "SZ": "CHF",
    "US": "USD",
}
MONTH_ABBR = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MONTH_TITLE = {month.title(): index + 1 for index, month in enumerate(MONTH_ABBR)}


@dataclass(frozen=True)
class Event:
    timestamp: int
    currency: str
    impact: str
    title: str


def fetch_text(url: str) -> str:
    response = requests.get(url, headers=HTTP_HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def to_forex_factory_date(value: date) -> str:
    return f"{MONTH_ABBR[value.month - 1]}{value.day}.{value.year}"


def html_to_text(fragment: str) -> str:
    fragment = re.sub(r"<script\b.*?</script>", " ", fragment, flags=re.IGNORECASE | re.DOTALL)
    fragment = re.sub(r"<style\b.*?</style>", " ", fragment, flags=re.IGNORECASE | re.DOTALL)
    fragment = re.sub(r"<br\s*/?>", " ", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", unescape(fragment)).strip()


def extract_calendar_cell(row_html: str, class_name: str) -> str:
    pattern = rf"<td\b[^>]*class=['\"][^'\"]*{re.escape(class_name)}[^'\"]*['\"][^>]*>(.*?)</td>"
    match = re.search(pattern, row_html, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1) if match else ""


def extract_class_fragment(row_html: str, class_name: str) -> str:
    pattern = rf"<(?P<tag>[a-z0-9]+)\b[^>]*class=['\"][^'\"]*{re.escape(class_name)}[^'\"]*['\"][^>]*>(?P<body>.*?)</(?P=tag)>"
    match = re.search(pattern, row_html, flags=re.IGNORECASE | re.DOTALL)
    return match.group("body") if match else ""


def extract_calendar_impact(row_html: str) -> str:
    impact_cell = extract_calendar_cell(row_html, "calendar__impact")
    class_text = " ".join(re.findall(r"class=['\"]([^'\"]+)['\"]", impact_cell, flags=re.IGNORECASE))
    if "icon--ff-impact-red" in class_text:
        return "H"
    if "icon--ff-impact-ora" in class_text:
        return "M"
    if "icon--ff-impact-yel" in class_text:
        return "L"
    return "N"


def parse_forex_factory_datetime(date_text: str, time_text: str, range_start: date, tz_name: str) -> int | None:
    date_match = re.search(r"\b(?:Sun|Mon|Tue|Wed|Thu|Fri|Sat)?\s*([A-Z][a-z]{2})\s+(\d{1,2})\b", date_text)
    if not date_match:
        return None

    month = MONTH_TITLE.get(date_match.group(1))
    day = int(date_match.group(2))
    if not month:
        return None

    year = range_start.year
    if range_start.month >= 11 and month <= 2:
        year += 1

    time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", time_text, flags=re.IGNORECASE)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or "0")
        period = time_match.group(3).lower()
        if period == "pm" and hour != 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0
        event_time = time(hour, minute)
    else:
        event_time = time(12, 0)

    try:
        event_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        event_tz = ZoneInfo("America/New_York")

    local_dt = datetime.combine(date(year, month, day), event_time, tzinfo=event_tz)
    return int(local_dt.astimezone(timezone.utc).timestamp())


def parse_calendar_html(html: str, range_start: date) -> list[Event]:
    timezone_match = re.search(r"timezone_name\s*:\s*['\"]([^'\"]+)['\"]", html)
    server_tz = timezone_match.group(1) if timezone_match else "America/New_York"
    row_pattern = re.compile(r"<tr\b(?=[^>]*\bdata-event-id=)[^>]*>(.*?)</tr>", flags=re.IGNORECASE | re.DOTALL)
    events: list[Event] = []
    last_date = ""
    last_time = ""

    for row_match in row_pattern.finditer(html):
        row_html = row_match.group(0)
        date_text = html_to_text(extract_calendar_cell(row_html, "calendar__date"))
        time_text = html_to_text(extract_calendar_cell(row_html, "calendar__time"))
        currency = html_to_text(extract_calendar_cell(row_html, "calendar__currency")).upper()
        title = html_to_text(extract_class_fragment(row_html, "calendar__event-title"))
        impact = extract_calendar_impact(row_html)

        if date_text:
            last_date = date_text
        if time_text:
            last_time = time_text
        if not title or not last_date:
            continue

        timestamp = parse_forex_factory_datetime(last_date, last_time, range_start, server_tz)
        if not timestamp:
            continue
        if currency not in CURRENCIES:
            currency = extract_currency(f"{currency} {title}")
        events.append(Event(timestamp, currency, impact, title))

    return events


def fetch_forex_factory_calendar_events(days_forward: int = 21) -> list[Event]:
    start = datetime.now(timezone.utc).date()
    end = start + timedelta(days=days_forward)
    url = FOREX_FACTORY_CALENDAR_URL.format(start=to_forex_factory_date(start), end=to_forex_factory_date(end))
    text = fetch_text(url)
    events = parse_calendar_html(text, start)
    print(f"Fetched {len(events)} events from ForexFactory calendar HTML {url}")
    return events


def unfold_ics(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.startswith((" ", "\t")) and lines:
            lines[-1] += raw_line[1:]
        elif raw_line:
            lines.append(raw_line)
    return lines


def parse_ics_text(value: str) -> str:
    return (
        value.replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def parse_dtstart(value_or_line: str, tz_name: str = "UTC") -> int | None:
    raw_value = value_or_line.split(":", 1)[1].strip() if ":" in value_or_line else value_or_line.strip()
    if not raw_value:
        return None

    is_utc = raw_value.endswith("Z")
    value = raw_value.rstrip("Z")
    formats = ["%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"]
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
            if is_utc:
                event_tz = timezone.utc
            else:
                try:
                    event_tz = ZoneInfo(tz_name)
                except ZoneInfoNotFoundError:
                    event_tz = timezone.utc
            return int(parsed.replace(tzinfo=event_tz).astimezone(timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def extract_field(component: dict[str, str], key: str) -> str:
    return parse_ics_text(component.get(key, ""))


def extract_currency(text: str) -> str:
    for currency in CURRENCIES:
        if re.search(rf"\b{currency}\b", text, flags=re.IGNORECASE):
            return currency
    for country, currency in COUNTRY_TO_CURRENCY.items():
        if re.search(rf"\b{country}\b", text, flags=re.IGNORECASE):
            return currency
    return "---"


def extract_impact(text: str) -> str:
    lowered = text.lower()
    if "impact: high" in lowered or "high impact" in lowered or "red folder" in lowered:
        return "H"
    if "impact: medium" in lowered or "medium impact" in lowered or "orange folder" in lowered:
        return "M"
    if "impact: low" in lowered or "low impact" in lowered or "yellow folder" in lowered:
        return "L"
    if "holiday" in lowered or "non-economic" in lowered:
        return "N"
    return "N"


def clean_title(summary: str, currency: str) -> str:
    title = re.sub(r"\s+", " ", summary).strip()
    title = re.sub(r"^[^A-Za-z0-9]+", "", title)
    title = re.sub(r"^\[?[A-Z]{2,3}\]?\s*[-–:]?\s*", "", title)
    title = re.sub(rf"^\b{re.escape(currency)}\b\s*[-–:]?\s*", "", title, flags=re.IGNORECASE)
    return title or summary or "Economic Event"


def parse_events(ics_text: str) -> list[Event]:
    events: list[Event] = []
    component: dict[str, str] | None = None

    for line in unfold_ics(ics_text):
        if line == "BEGIN:VEVENT":
            component = {}
            continue
        if line == "END:VEVENT":
            if component:
                summary = extract_field(component, "SUMMARY")
                description = extract_field(component, "DESCRIPTION")
                timestamp = parse_dtstart(component.get("DTSTART", ""), component.get("DTSTART_TZ", "UTC"))
                if timestamp and summary:
                    combined = f"{summary} {description}"
                    currency = extract_currency(combined)
                    impact = extract_impact(combined)
                    title = clean_title(summary, currency)
                    events.append(Event(timestamp, currency, impact, title))
            component = None
            continue
        if component is None or ":" not in line:
            continue

        raw_key, value = line.split(":", 1)
        key_parts = raw_key.split(";")
        key = key_parts[0].upper()
        if key == "DTSTART":
            for param in key_parts[1:]:
                if param.upper().startswith("TZID="):
                    component["DTSTART_TZ"] = param.split("=", 1)[1]
        component[key] = value

    return events


def normalize_text(text: str) -> str:
    text = text.upper()
    text = text.replace("&", "AND")
    text = re.sub(r"[^A-Z0-9\-/: ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def pack_text(text: str) -> list[int]:
    normalized = normalize_text(text)[: CHARS_PER_FIELD * 4].ljust(CHARS_PER_FIELD * 4)
    packed: list[int] = []
    for offset in range(0, CHARS_PER_FIELD * 4, CHARS_PER_FIELD):
        chunk = normalized[offset : offset + CHARS_PER_FIELD]
        value = 0
        for char in chunk:
            value = value * BASE + ALPHABET.index(char if char in ALPHABET else " ")
        packed.append(value)
    return packed


def encode_event(event: Event) -> list[int]:
    text = f"{event.currency[:3].ljust(3)}{event.impact[:1]}{event.title}"
    high, low, close, volume = pack_text(text)
    return [event.timestamp, high, low, close, volume]


def write_symbol_meta(symbol: str, index: int) -> None:
    symbol_dir = ROOT / symbol
    symbol_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "description": f"LANDI Forex Factory upcoming economic event slot {index}",
        "currency": "USD",
        "base_currency": "USD",
        "exchange": "CUSTOM",
        "type": "index",
        "session": "24x7",
        "timezone": "Etc/UTC",
        "pricescale": 1,
        "has_intraday": True,
        "supported_resolutions": ["1", "5", "15", "30", "60", "240", "1D"],
    }
    (symbol_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def write_slot(symbol: str, values: list[int]) -> None:
    symbol_dir = ROOT / symbol
    symbol_dir.mkdir(parents=True, exist_ok=True)
    row = ",".join(str(value) for value in values)
    (symbol_dir / "data.csv").write_text(f"date,open,high,low,close,volume\n{BASE_ROW_DATE},{row}\n", encoding="utf-8")


def unique_future_events(events: list[Event]) -> list[Event]:
    now = int(datetime.now(timezone.utc).timestamp())
    seen: set[tuple[int, str, str]] = set()
    unique: list[Event] = []
    for event in sorted(events, key=lambda item: item.timestamp):
        if event.timestamp <= now:
            continue
        key = (event.timestamp, event.currency, normalize_text(event.title))
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    return unique


def fetch_events_from_sources(urls: list[str], label: str) -> list[Event]:
    all_events: list[Event] = []
    for url in urls:
        try:
            text = fetch_text(url)
        except (urllib.error.URLError, TimeoutError) as error:
            print(f"WARNING: failed to fetch {label} source {url}: {error}", file=sys.stderr)
            continue
        parsed = parse_events(text)
        print(f"Fetched {len(parsed)} events from {label} source {url}")
        all_events.extend(parsed)
    return all_events


def main() -> int:
    try:
        all_events = fetch_forex_factory_calendar_events()
    except (urllib.error.URLError, TimeoutError) as error:
        print(f"WARNING: failed to fetch ForexFactory calendar HTML: {error}", file=sys.stderr)
        all_events = []

    future_events = unique_future_events(all_events)
    if not future_events:
        print("ForexFactory HTML returned no future events; trying primary ICS sources", file=sys.stderr)
        all_events = fetch_events_from_sources(PRIMARY_SOURCE_URLS, "primary")
        future_events = unique_future_events(all_events)

    if not future_events:
        print("Primary sources returned no future events; trying GitHub ICS fallback", file=sys.stderr)
        all_events = fetch_events_from_sources(FALLBACK_SOURCE_URLS, "fallback")
        future_events = unique_future_events(all_events)

    print(f"Writing {min(len(future_events), SLOT_COUNT)} upcoming events from {len(future_events)} future events")

    (ROOT / "seed_meta.json").write_text(json.dumps({"data": {"symbols": SYMBOLS}}, indent=2) + "\n", encoding="utf-8")

    for index, symbol in enumerate(SYMBOLS, start=1):
        write_symbol_meta(symbol, index)
        if index <= len(future_events):
            event = future_events[index - 1]
            write_slot(symbol, encode_event(event))
            stamp = datetime.fromtimestamp(event.timestamp, timezone.utc).isoformat()
            print(f"{index:02d}. {stamp} {event.currency} {event.impact} {event.title}")
        else:
            write_slot(symbol, [0, 0, 0, 0, 0])

    return 0 if future_events else 1


if __name__ == "__main__":
    raise SystemExit(main())