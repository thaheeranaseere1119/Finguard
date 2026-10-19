"""XYZ Ltd chart of accounts, dimensions and fiscal calendar (imaginary company; all data synthetic).

Fiscal year = Indian FY, 1 April – 31 March. "FY2024" means 1 Apr 2023 – 31 Mar 2024.
Data covers FY2017 … FY2026 (1 Apr 2016 – 31 Mar 2026): 10 years, 120 months, 3,652 days.
"""

from __future__ import annotations

from datetime import date

COMPANY = "XYZ Ltd"
COMPANY_ALIASES = ("xyz ltd", "xyz limited", "xyz", "the company", "our company", "we", "us")
DATA_START = date(2016, 4, 1)
DATA_END = date(2026, 3, 31)
FIRST_FY, LAST_FY = 2017, 2026
CURRENCY = "INR"

# code: (name, type, statement)
ACCOUNTS: dict[str, tuple[str, str, str]] = {
    "1000": ("Cash and bank", "ASSET", "BS"),
    "1100": ("Trade receivables", "ASSET", "BS"),
    "1200": ("Inventories", "ASSET", "BS"),
    "1500": ("Property, plant and equipment (gross)", "ASSET", "BS"),
    "1510": ("Accumulated depreciation", "ASSET", "BS"),
    "2000": ("Trade payables", "LIABILITY", "BS"),
    "2300": ("Income tax payable", "LIABILITY", "BS"),
    "2500": ("Long-term borrowings", "LIABILITY", "BS"),
    "3000": ("Share capital", "EQUITY", "BS"),
    "3100": ("Retained earnings (opening)", "EQUITY", "BS"),
    "3200": ("Dividends declared", "EQUITY", "BS"),
    "4000": ("Product revenue", "REVENUE", "IS"),
    "4100": ("Service revenue", "REVENUE", "IS"),
    "4200": ("Other income (interest)", "REVENUE", "IS"),
    "5000": ("Cost of goods sold", "EXPENSE", "IS"),
    "5100": ("Cost of services", "EXPENSE", "IS"),
    "6000": ("Employee benefit expense (salaries)", "EXPENSE", "IS"),
    "6100": ("Rent", "EXPENSE", "IS"),
    "6200": ("Utilities", "EXPENSE", "IS"),
    "6300": ("Marketing and advertising", "EXPENSE", "IS"),
    "6400": ("Travel", "EXPENSE", "IS"),
    "6500": ("IT and software", "EXPENSE", "IS"),
    "6600": ("Professional fees", "EXPENSE", "IS"),
    "6700": ("Depreciation", "EXPENSE", "IS"),
    "6800": ("Repairs and maintenance", "EXPENSE", "IS"),
    "7000": ("Finance costs (interest)", "EXPENSE", "IS"),
    "8000": ("Income tax expense", "EXPENSE", "IS"),
}

SEGMENTS = ("Enterprise Software", "Cloud Services", "Hardware Solutions")
SEGMENT_ALIASES = {
    "enterprise software": "Enterprise Software", "software": "Enterprise Software", "enterprise": "Enterprise Software",
    "cloud services": "Cloud Services", "cloud": "Cloud Services", "services segment": "Cloud Services",
    "hardware solutions": "Hardware Solutions", "hardware": "Hardware Solutions",
}
REGIONS = ("North", "South", "East", "West", "Export")

CUSTOMERS = (
    "Orion Retail", "Bluepeak Logistics", "Saffron Foods", "Kestrel Motors", "Lumen Healthcare", "Vertex Pharma",
    "Coral Bank", "Indus Telecom", "Granite Infra", "Monsoon Textiles", "Pinnacle Insurance", "Zenith Airlines",
    "Aurora Hotels", "Cobalt Mining", "Nimbus Energy", "Quartz Electronics", "Harbor Shipping", "Maple Education",
    "Falcon Agritech", "Delta Steel", "Summit Realty", "Riverbend Dairy", "Opal Cosmetics", "Titan Cement",
    "Lotus Hospitals", "Sapphire Jewels", "Ember Chemicals", "Horizon Media", "Atlas Engineering", "Comet Couriers",
    "Ivory Apparel", "Jade Paints", "Keystone Finance", "Lyra Gaming", "Meridian Ports", "Nova Biotech",
    "Onyx Plastics", "Prism Glass", "Quill Publishing", "Radiant Solar", "Sterling Tyres", "Topaz Beverages",
    "Umber Furniture", "Velvet Fashion", "Willow Organics", "Xenon Labs", "Yarrow Seeds", "Zephyr Fans",
)
VENDORS = (
    "Alpha Components", "Beacon Supplies", "Crest Semiconductors", "Dune Packaging", "Echo Cloud Infra",
    "Fusion Contractors", "Glide Logistics", "Helix Hardware", "Iris Office Mart", "Juno Power Systems",
    "Krypton Data Centres", "Lattice Networks", "Matrix Staffing", "Nexus Facilities", "Omega Consultants",
)
OPEX_COUNTERPARTIES = {
    "6100": "Metro Estates Pvt Ltd", "6200": "State Electricity Board", "6300": "Brightline Advertising",
    "6400": "SkyWays Travel", "6500": "CodeForge Software", "6600": "Rao & Associates LLP", "6800": "FixIt Services",
}

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


def fy_of(d: date) -> int:
    return d.year + 1 if d.month >= 4 else d.year


def fy_bounds(fy: int) -> tuple[date, date]:
    return date(fy - 1, 4, 1), date(fy, 3, 31)


def quarter_bounds(fy: int, q: int) -> tuple[date, date]:
    import calendar

    start_month = 4 + 3 * (q - 1)
    y = fy - 1 if start_month <= 12 else fy
    m = start_month if start_month <= 12 else start_month - 12
    end_m = m + 2
    end_y = y
    if end_m > 12:
        end_m -= 12
        end_y += 1
    return date(y, m, 1), date(end_y, end_m, calendar.monthrange(end_y, end_m)[1])


def fy_label(fy: int) -> str:
    return f"FY{fy} ({fy - 1}-{str(fy)[2:]})"
