"""Understanding the *conditions* in a transaction question.

"gimme all transactions related to hardware and products above 5 lakh in FY2025" carries four conditions — a business
domain, an amount floor, a period and "all". Each is turned into an explicit filter that the ledger query applies, and
every filter is reported back to the user ("Understood as …"), so nothing in the question is silently ignored. Words
that match no filter are kept as `leftover` and resolved later against the ledger itself (counterparty names,
descriptions) — or, if that fails, reported as not understood instead of being answered with an unrelated list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from finguard.xyz.coa import ACCOUNTS

HARDWARE_VENDORS = ("Alpha Components", "Beacon Supplies", "Crest Semiconductors", "Dune Packaging", "Helix Hardware")
CLOUD_VENDORS = ("Echo Cloud Infra", "Krypton Data Centres")

# business domains: transactions of the segment (sales, receipts) OR with the domain's suppliers (purchases, payments)
DOMAINS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "hardware": ("Hardware & products", "Hardware Solutions", HARDWARE_VENDORS),
    "cloud": ("Cloud services", "Cloud Services", CLOUD_VENDORS),
    "software": ("Enterprise software", "Enterprise Software", ()),
}

# topic key → (pattern, label, filter fields). Order matters: specific phrases first.
TOPICS: list[tuple[str, str, str, dict]] = [
    ("it_software", r"\bit (?:and |& )?software\b|\bsoftware (?:expenses?|subscriptions?|licen[cs]e fees?|bills?|costs?|spend\w*)\b|"
                    r"\bit (?:expenses?|costs?|spend\w*|bills?)\b|\bcodeforge\b", "IT & software expenses", {"accounts": ("6500",)}),
    ("hardware", r"\bhardware\b|\bproducts?\b|\bcomponents?\b|\bdevices?\b|\bchips?\b|\binventory\b|\bgoods\b",
     "Hardware & products", {"domain": "hardware"}),
    ("cloud", r"\bcloud\b|\bhosting\b|\bdata ?cent(?:re|er)s?\b", "Cloud services", {"domain": "cloud"}),
    ("software", r"\b(?:enterprise )?software\b|\blicen[cs](?:e|es|ing)\b", "Enterprise software", {"domain": "software"}),
    ("rent", r"\brent(?:al|s)?\b|\blease\b|\boffice space\b|\bmetro estates\b", "Rent", {"accounts": ("6100",)}),
    ("utilities", r"\butilit(?:y|ies)\b|\belectricity\b|\bpower bills?\b", "Utilities", {"accounts": ("6200",)}),
    ("marketing", r"\bmarketing\b|\badvertis\w*\b|\bads\b|\bpromotions?\b|\bbrightline\b", "Marketing & advertising",
     {"accounts": ("6300",)}),
    ("travel", r"\btravel\w*\b|\bflights?\b|\bhotels?\b|\btrips?\b|\bskyways\b", "Travel", {"accounts": ("6400",)}),
    ("professional", r"\bprofessional (?:fees?|services?)\b|\blegal\b|\baudit (?:fees?)?\b|\bconsult\w*\b",
     "Professional & legal fees", {"accounts": ("6600",)}),
    ("repairs", r"\brepairs?\b|\bmaintenance\b|\bfixit\b", "Repairs & maintenance", {"accounts": ("6800",)}),
]
TOPIC_KEYS = {k: (label, spec) for k, _, label, spec in TOPICS}

_UNITS = {"k": 10**3, "thousand": 10**3, "thousands": 10**3, "lakh": 10**5, "lakhs": 10**5, "lac": 10**5, "lacs": 10**5,
          "l": 10**5, "crore": 10**7, "crores": 10**7, "cr": 10**7, "million": 10**6, "mn": 10**6, "m": 10**6}
_AMT = r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|thousands?|lakhs?|lacs?|l|crores?|cr|million|mn|m)?\b(?!\s*(?:days?|weeks?|months?|years?|quarters?|transactions?|entries))"
_MIN = r"(?:above|over|more than|greater than|exceeding|exceeds?|at least|minimum(?: of)?|bigger than|larger than|higher than|>=?|from)"
_MAX = r"(?:below|under|less than|lower than|smaller than|at most|up ?to|not more than|maximum(?: of)?|<=?)"
_TIME = (r"\b(?:fy\s?'?\d{2,4}|q[1-4]|h[12]|(?:19|20)\d{2}|jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|"
         r"nov\w*|dec\w*|years?|yearly|annual\w*|quarters?|quarterly|months?|monthly|weeks?|weekly|days?|daily|today|yesterday|"
         r"ago|since|till|until|through|ytd|mtd|fiscal|financial|calendar|this|last|previous|past|next|current|first|second|third|"
         r"fourth|half|\d{1,2}(?:st|nd|rd|th)?)\b")
STOP = set("""gimme give me show list display get find fetch pull bring all every everything entire complete full whole the a an of to
for in on at by with and or related relating relate regarding about concerning connected linked associated pertaining
involving involve made done any some our we us xyz company ltd limited please can could would you i want need see view tell
what which were was are is be been have has did do does there their its it that this these those my kindly just only also plus
other others etc details detail info information records record data transaction transactions entries entry journal ledger
postings posting vouchers voucher activity activities dealings how many count number total sum value values amount amounts
much recent latest newest oldest earliest largest biggest smallest highest lowest top first last wise between from domain
domains category categories segment segments area areas type types kind kinds department departments business line lines
side stuff things thing item items so far ever till now yet more most less least big large small huge major significant
order sorted sort descending ascending chronological date dates when where who whom why happened happen occurred took place
recorded posted ones one under over above below than greater exceeding rs inr lakh lakhs crore crores cr thousand million
same such each sir pls plz okay ok hi hello hey bro dear just quickly kind regards rupees worth paid pay payments payment
money received company's companies wanna want lemme let containing contains mentioning mentions word text description
named called""".split())


@dataclass
class TxnFilter:
    kinds: tuple[str, ...] = ()
    parties: tuple[str, ...] = ()
    segment: str | None = None
    region: str | None = None
    accounts: tuple[str, ...] = ()            # the entry debits one of these accounts
    domain: str | None = None                 # DOMAINS key: segment OR the domain's suppliers
    min_amt: int | None = None                # paise, inclusive
    max_amt: int | None = None
    text: str | None = None                   # substring of description / counterparty / reference

    def empty(self) -> bool:
        return not (self.kinds or self.parties or self.segment or self.region or self.accounts or self.domain
                    or self.min_amt is not None or self.max_amt is not None or self.text)

    def matches(self, r, debit_accounts: set[str]) -> bool:
        if self.kinds and r.kind not in self.kinds:
            return False
        if self.parties and r.counterparty not in self.parties:
            return False
        if self.segment and r.segment != self.segment:
            return False
        if self.region and r.region != self.region:
            return False
        if self.accounts and not (debit_accounts & set(self.accounts)):
            return False
        if self.domain:
            _, seg, vendors = DOMAINS[self.domain]
            if r.segment != seg and r.counterparty not in vendors:
                return False
        if self.min_amt is not None and r.amount < self.min_amt:
            return False
        if self.max_amt is not None and r.amount > self.max_amt:
            return False
        if self.text:
            t = self.text.lower()
            if t not in r.description.lower() and t not in (r.counterparty or "").lower() and t not in r.doc_ref.lower():
                return False
        return True

    def sql(self) -> tuple[str, dict]:
        """WHERE fragment over entries `e` (entry-level conditions) — amounts and accounts are applied on the aggregate."""
        w, p = "", {}
        if self.kinds:
            w += " AND e.kind = ANY(:f_k)"
            p["f_k"] = list(self.kinds)
        if self.parties:
            w += " AND e.counterparty = ANY(:f_cp)"
            p["f_cp"] = list(self.parties)
        if self.segment:
            w += " AND e.segment = :f_seg"
            p["f_seg"] = self.segment
        if self.region:
            w += " AND e.region = :f_reg"
            p["f_reg"] = self.region
        if self.domain:
            _, seg, vendors = DOMAINS[self.domain]
            w += " AND (e.segment = :f_dseg OR e.counterparty = ANY(:f_dv))"
            p["f_dseg"], p["f_dv"] = seg, list(vendors)
        if self.text:
            w += " AND (e.description ILIKE :f_t OR e.counterparty ILIKE :f_t OR e.doc_ref ILIKE :f_t)"
            p["f_t"] = "%" + self.text.replace("\\", "").replace("%", "").replace("_", "") + "%"
        return w, p


@dataclass
class Parsed:
    topics: list[str] = field(default_factory=list)        # TOPICS keys
    min_amt: int | None = None
    max_amt: int | None = None
    text: str | None = None
    want_all: bool = False
    leftover: list[str] = field(default_factory=list)      # content words no filter explained


def _paise(num: str, unit: str | None) -> int:
    v = float(num.replace(",", ""))
    return int(round(v * _UNITS.get((unit or "").lower(), 1) * 100))


def amount_range(low: str) -> tuple[int | None, int | None, str]:
    """(min paise, max paise, low with the amount phrase removed)."""
    if m := re.search(rf"\bbetween\s+{_AMT}\s+(?:and|to|-)\s+{_AMT}", low):
        a_num, a_unit, b_num, b_unit = m[1], m[2], m[3], m[4]
        if not (re.fullmatch(r"(?:19|20)\d{2}", a_num) and not a_unit and not b_unit):   # "between 2020 and 2022" is a period
            lo, hi = _paise(a_num, a_unit or b_unit), _paise(b_num, b_unit or a_unit)
            return min(lo, hi), max(lo, hi), low[:m.start()] + " " + low[m.end():]
    lo = hi = None
    for which, pat in (("min", _MIN), ("max", _MAX)):
        if (m := re.search(rf"(?<![\w-]){pat}\s+{_AMT}", low)) and not (re.fullmatch(r"(?:19|20)\d{2}", m[1]) and not m[2]):
            if which == "min" and pat == _MIN and m.group(0).startswith("from") and not (m[2] or re.search(r"₹|rs|inr", m.group(0))):
                continue                                   # "from 15 August" is a date
            v = _paise(m[1], m[2])
            if which == "min":
                lo = v
            else:
                hi = v
            low = low[:m.start()] + " " + low[m.end():]
    return lo, hi, low


def parse(low: str, consumed: list[str], names: tuple[str, ...] = ()) -> Parsed:
    """`consumed` — regexes already understood by the caller (kinds, region, order …), removed before looking for
    leftover words. `names` — counterparty names found, removed first so "Alpha Components" is not read as a topic."""
    p = Parsed()
    rest = low
    for n in names:
        rest = re.sub(rf"\b{re.escape(n.lower())}\b", " ", rest)
    if m := re.search(r"[\"“'‘]([^\"”'’]{2,40})[\"”'’]", low):
        p.text = m[1].strip()
        rest = rest.replace(m.group(0), " ")
    elif m := re.search(r"\b(?:containing|contains|mentioning|mentions|with (?:the )?(?:word|text|description)|"
                        r"description (?:contains|has|like)|named|called)\s+([a-z0-9&.\- ]{2,40}?)(?=\s+(?:in|on|during|for|from|"
                        r"above|below|over|under|between|since|before|after)\b|[?.!,]|$)", low):
        p.text = m[1].strip()
        rest = rest.replace(m.group(0), " ")
    p.min_amt, p.max_amt, rest = amount_range(rest)
    for key, pat, _, _ in TOPICS:
        if re.search(pat, rest):
            if not (key == "software" and "it_software" in p.topics):
                p.topics.append(key)
            rest = re.sub(pat, " ", rest)
    p.want_all = bool(re.search(r"\b(?:all|every|everything|entire|complete|full list|whole)\b", low))
    for pat in consumed:
        rest = re.sub(pat, " ", rest)
    rest = re.sub(_TIME, " ", rest)
    words = re.findall(r"[a-z][a-z&\-]+", rest)
    p.leftover = list(dict.fromkeys(w for w in words if len(w) >= 3 and w not in STOP))
    return p


def topic_label(key: str) -> str:
    return TOPIC_KEYS[key][0]


def account_names(accs) -> str:
    return ", ".join(f"{ACCOUNTS[a][0]} ({a})" for a in accs)
