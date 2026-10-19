"""Period expressions → concrete date ranges on XYZ Ltd's fiscal calendar (Indian FY, April–March).

Supported: FY2022 · FY22 · fiscal (year) 2022 · FY 2021-22 · 2021-22 · Q3 FY2022 · third quarter of FY2022 · H1 FY2023 ·
March 2021 · Mar-2021 · 15 August 2019 · August 15, 2019 · 2019-08-15 · 15/08/2019 · calendar year 2019 · CY2019 ·
between <date> and <date> · from FY2018 to FY2020 · FY2017–FY2019 (expanded for averages) · last N (fiscal) years ·
latest / most recent fiscal year · bare "2019" (interpreted as FY2019, stated as an assumption).
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

from finguard.xyz.coa import DATA_END, DATA_START, FIRST_FY, LAST_FY, MONTHS, fy_bounds, quarter_bounds

MONTH_IX = {m.lower(): i for i, m in enumerate(MONTHS, start=1)}
MONTH_IX.update({m[:3].lower(): i for m, i in list(MONTH_IX.items())})
MONTH_IX["sept"] = 9
ORD = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}
_M = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)"


@dataclass(frozen=True)
class Period:
    label: str
    start: date
    end: date
    kind: str              # FY | QUARTER | HALF | MONTH | DAY | RANGE | CY
    fy: int | None = None
    assumption: str | None = None

    @property
    def pretty(self) -> str:
        return f"{self.label} ({self.start.day} {self.start:%b %Y} – {self.end.day} {self.end:%b %Y})" \
            if self.kind != "DAY" else f"{self.start.day} {self.start:%B %Y}"


def _num_word(tok: str) -> int:
    words = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12}
    return words.get(tok) or int(tok)


def fy_period(fy: int, assumption: str | None = None) -> Period:
    a, b = fy_bounds(fy)
    return Period(f"FY{fy}", a, b, "FY", fy, assumption)


def _fy_num(tok: str) -> int:
    n = int(tok)
    return 2000 + n if n < 100 else n


def find_periods(text: str) -> list[Period]:
    """All period mentions in reading order (spans consumed so 'Q3 FY2022' is not also read as 'FY2022')."""
    t = text.lower().replace("–", "-").replace("—", "-")
    found: list[tuple[int, int, Period | list[Period]]] = []

    def take(m: re.Match, p) -> None:
        if not any(s < m.end() and m.start() < e for s, e, _ in found):
            found.append((m.start(), m.end(), p))

    # ranges first
    for m in re.finditer(rf"(?:between|from)\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+{_M}\s+(\d{{4}})\s+(?:and|to|till|until)\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+{_M}\s+(\d{{4}})", t):
        try:
            a = date(int(m[3]), MONTH_IX[m[2]], int(m[1]))
            b = date(int(m[6]), MONTH_IX[m[5]], int(m[4]))
            take(m, Period(f"{a:%d %b %Y} – {b:%d %b %Y}", a, b, "RANGE"))
        except ValueError:
            pass
    for m in re.finditer(r"(?:between|from)\s+(\d{4}-\d{2}-\d{2})\s+(?:and|to|till|until)\s+(\d{4}-\d{2}-\d{2})", t):
        a, b = date.fromisoformat(m[1]), date.fromisoformat(m[2])
        take(m, Period(f"{a:%d %b %Y} – {b:%d %b %Y}", a, b, "RANGE"))
    for m in re.finditer(r"\bfy\s?'?(\d{2,4})\s*(?:-|to|through|until)\s*fy\s?'?(\d{2,4})\b(?!\s*-\s*\d)", t):
        a, b = _fy_num(m[1]), _fy_num(m[2])
        if a < b and b - a <= 12:
            take(m, [fy_period(a), fy_period(b)])   # endpoints; AVERAGE/SUM expand the range
    # relative windows are anchored to the latest data in the ledger (31 Mar 2026), stated as an assumption
    rel = f"relative to the latest data available ({DATA_END.day} {DATA_END:%b %Y})"
    for m in re.finditer(r"\b(?:last|past|previous|trailing|recent)\s+(\d{1,3}|two|three|four|five|six|seven|eight|nine|ten|twelve)?\s*(months?|quarters?|weeks?|days?)\b", t):
        n = _num_word(m[1]) if m[1] else 1
        unit = m[2].rstrip("s")
        if unit == "month":
            out_p, y, mo = [], DATA_END.year, DATA_END.month
            for _ in range(min(n, 120)):
                a = date(y, mo, 1)
                out_p.insert(0, Period(f"{a:%B %Y}", a, date(y, mo, calendar.monthrange(y, mo)[1]), "MONTH", assumption=rel))
                y, mo = (y, mo - 1) if mo > 1 else (y - 1, 12)
            take(m, out_p)
        elif unit == "quarter":
            out_p, fy, q = [], LAST_FY, 4
            for _ in range(min(n, 40)):
                a, b = quarter_bounds(fy, q)
                out_p.insert(0, Period(f"Q{q} FY{fy}", a, b, "QUARTER", fy, assumption=rel))
                fy, q = (fy, q - 1) if q > 1 else (fy - 1, 4)
            take(m, out_p)
        elif unit == "week":
            b = DATA_END
            take(m, [Period(f"last {n} week{'s' if n > 1 else ''}", b - timedelta(days=7 * n - 1), b, "RANGE", assumption=rel)])
        else:
            take(m, [Period(f"{(DATA_END - timedelta(days=i)):%d %b %Y}", DATA_END - timedelta(days=i), DATA_END - timedelta(days=i), "DAY",
                            assumption=rel) for i in range(min(n, 92) - 1, -1, -1)])
    for m in re.finditer(r"\blast\s+(\d{1,2}|two|three|four|five)\s+(?:fiscal\s+|financial\s+)?years?\b", t):
        n = {"two": 2, "three": 3, "four": 4, "five": 5}.get(m[1]) or int(m[1])
        take(m, [fy_period(y) for y in range(LAST_FY - n + 1, LAST_FY + 1)])
    for m in re.finditer(r"\b(?:latest|most recent|current|last)\s+(?:fiscal|financial)\s+year\b|\blatest year\b", t):
        take(m, fy_period(LAST_FY, "latest fiscal year in the data"))
    # quarters / halves
    for m in re.finditer(r"\b(?:q([1-4])|(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter(?:\s+of)?)\s*(?:of\s+)?(?:fy\s?'?|fiscal\s+(?:year\s+)?)?(\d{4}-\d{2}|\d{2,4})\b", t):
        q = int(m[1]) if m[1] else ORD[m[2]]
        tok = m[3]
        fy = _fy_num(tok.split("-")[0]) + 1 if "-" in tok else _fy_num(tok)
        a, b = quarter_bounds(fy, q)
        take(m, Period(f"Q{q} FY{fy}", a, b, "QUARTER", fy))
    for m in re.finditer(r"\b(?:h([12])|(first|second)\s+half(?:\s+of)?)\s*(?:fy\s?'?)?(\d{2,4})\b", t):
        h = int(m[1]) if m[1] else (1 if m[2] == "first" else 2)
        fy = _fy_num(m[3])
        a = quarter_bounds(fy, 1 if h == 1 else 3)[0]
        b = quarter_bounds(fy, 2 if h == 1 else 4)[1]
        take(m, Period(f"H{h} FY{fy}", a, b, "HALF", fy))
    # days
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_M},?\s+(\d{{4}})\b", t):
        try:
            d = date(int(m[3]), MONTH_IX[m[2]], int(m[1]))
            take(m, Period(f"{d:%d %b %Y}", d, d, "DAY"))
        except ValueError:
            pass
    for m in re.finditer(rf"\b{_M}\s+(\d{{1,2}})(?:st|nd|rd|th)?,\s*(\d{{4}})\b", t):
        try:
            d = date(int(m[3]), MONTH_IX[m[1]], int(m[2]))
            take(m, Period(f"{d:%d %b %Y}", d, d, "DAY"))
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        try:
            d = date(int(m[1]), int(m[2]), int(m[3]))
            take(m, Period(f"{d:%d %b %Y}", d, d, "DAY"))
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", t):
        try:
            d = date(int(m[3]), int(m[2]), int(m[1]))  # day-first (Indian convention)
            take(m, Period(f"{d:%d %b %Y}", d, d, "DAY", assumption="dd/mm/yyyy"))
        except ValueError:
            pass
    # months
    for m in re.finditer(rf"\b{_M}[\s\-',]*(\d{{4}})\b", t):
        y, mo = int(m[2]), MONTH_IX[m[1]]
        a, b = date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])
        take(m, Period(f"{a:%B %Y}", a, b, "MONTH"))
    # fiscal years
    for m in re.finditer(r"\b(?:fy|financial year|fiscal year|fiscal)\s?'?(\d{4}|\d{2})\s*-\s*(\d{2}|\d{4})\b", t):
        fy = _fy_num(m[1]) + 1
        take(m, fy_period(fy))
    for m in re.finditer(r"\b(?:fy|financial year|fiscal year|fiscal)\s?'?(\d{4}|\d{2})\b", t):
        take(m, fy_period(_fy_num(m[1])))
    for m in re.finditer(r"\b(20\d{2})\s*-\s*(\d{2})\b", t):
        if int(m[2]) == (int(m[1]) + 1) % 100:
            take(m, fy_period(int(m[1]) + 1))
    for m in re.finditer(r"\b(?:calendar year|cy)\s?(\d{4})\b", t):
        y = int(m[1])
        take(m, Period(f"CY{y}", date(y, 1, 1), date(y, 12, 31), "CY"))
    for m in re.finditer(r"\b(20[12]\d)\b", t):
        y = int(m[1])
        take(m, fy_period(y, f"'{y}' interpreted as fiscal year FY{y} (1 Apr {y - 1} – 31 Mar {y})"))
    found.sort(key=lambda x: x[0])
    out: list[Period] = []
    for _, _, p in found:
        out.extend(p if isinstance(p, list) else [p])
    return out


RANGE_HINT = re.compile(r"\bfy\s?'?\d{2,4}\s*(?:-|–|to|through|till|until)\s*(?:fy\s?'?)?\d{2,4}\b|\b\d+[- ]year\b|"
                        r"\b(?:three|two|four|five)[- ]year\b|\bthrough\b|\bover the (?:years|period)\b", re.I)


def expand_fy_range(periods: list[Period]) -> list[Period]:
    if len(periods) == 2 and all(p.kind == "FY" for p in periods) and periods[1].fy > periods[0].fy + 1:
        return [fy_period(y) for y in range(periods[0].fy, periods[1].fy + 1)]
    return periods


def in_range(p: Period) -> bool:
    return p.start >= DATA_START and p.end <= DATA_END


def coverage_note() -> str:
    return f"XYZ Ltd data covers FY{FIRST_FY}–FY{LAST_FY} (1 Apr 2016 – 31 Mar 2026)."
