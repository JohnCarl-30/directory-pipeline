"""Synthetic directory data.

Deliberately dirty. Clean fixtures prove nothing -- the point of this dataset is
that it contains the failure modes the pipeline claims to handle:

  * near-duplicate records (same company, different listing, name drift)
  * an exact duplicate under a second listing id
  * a franchise: same brand, different city -- NOT a duplicate
  * inconsistent phone/URL/state formats
  * pages with a drifted template (no microdata) so the DOM extractor misses
  * one stub page with almost nothing on it
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Company:
    slug: str
    name: str
    category: str
    description: str
    street: str
    city: str
    region: str
    postal: str
    phone: str
    email: str
    website: str
    employees: str
    founded: str
    tags: list[str] = field(default_factory=list)
    template: str = "microdata"  # microdata | drifted | stub


COMPANIES: list[Company] = [
    Company(
        "northwind-analytics",
        "Northwind Analytics, Inc.",
        "software",
        "Northwind Analytics builds real-time customer data platforms for mid-market retailers.",
        "412 Congress Ave, Suite 800",
        "Austin",
        "Texas",
        "78701",
        "(512) 555-0142",
        "hello@northwindanalytics.com",
        "https://www.northwindanalytics.com",
        "51-200 employees",
        "Founded 2014",
        ["Analytics", "Data Platform", "Retail"],
    ),
    Company(
        # Near-duplicate of the above: name drift + same domain. Score ~0.9.
        "northwind-analytics-llc",
        "Northwind Analytics LLC",
        "software",
        "Real-time customer data platform vendor serving retail.",
        "412 Congress Avenue #800",
        "Austin",
        "TX",
        "78701",
        "512-555-0142",
        "sales@northwindanalytics.com",
        "northwindanalytics.com",
        "~120",
        "2014",
        ["Analytics", "Retail"],
        template="drifted",
    ),
    Company(
        "harbor-point-labs",
        "Harbor Point Labs",
        "software",
        "Harbor Point Labs develops computer-vision tooling for industrial inspection.",
        "88 Seaport Blvd",
        "Boston",
        "Massachusetts",
        "02210",
        "+1 617 555 0199",
        "contact@harborpointlabs.io",
        "https://harborpointlabs.io",
        "11-50 employees",
        "Founded 2019",
        ["Computer Vision", "Manufacturing"],
    ),
    Company(
        "cascade-freight",
        "Cascade Freight Systems Corp.",
        "logistics",
        "Cascade Freight Systems operates regional LTL trucking across the Pacific Northwest.",
        "2200 NW 21st Ave",
        "Portland",
        "Oregon",
        "97210",
        "(503) 555-0110 ext. 4",
        "dispatch@cascadefreight.com",
        "www.cascadefreight.com",
        "201-1000 employees",
        "Founded 1998",
        ["Freight", "LTL", "Logistics"],
    ),
    Company(
        "cascade-freight-seattle",
        "Cascade Freight Systems",
        "logistics",
        "Seattle terminal of Cascade Freight Systems, serving Puget Sound.",
        "5100 4th Ave S",
        "Seattle",
        "Washington",
        "98134",
        "(206) 555-0188",
        "seattle@cascadefreight.com",
        "www.cascadefreight.com",
        "51-200 employees",
        "Founded 2006",
        ["Freight", "Logistics"],
    ),
    Company(
        "meridian-health-partners",
        "Meridian Health Partners",
        "healthcare",
        "Meridian Health Partners runs outpatient specialty clinics across the Southeast.",
        "1500 Peachtree St NE",
        "Atlanta",
        "Georgia",
        "30309",
        "404.555.0123",
        "info@meridianhealthpartners.org",
        "https://meridianhealthpartners.org",
        "1,200 staff",
        "Founded 2003",
        ["Healthcare", "Clinics"],
    ),
    Company(
        "brightline-capital",
        "Brightline Capital Advisors",
        "finance",
        "Brightline Capital Advisors provides M&A advisory to founder-owned industrials.",
        "1 Rockefeller Plaza",
        "New York",
        "NY",
        "10020",
        "(212) 555-0177",
        "ir@brightlinecap.com",
        "https://brightlinecap.com",
        "11-50 employees",
        "Founded 2011",
        ["M&A", "Advisory", "Finance"],
    ),
    Company(
        "atlas-robotics",
        "Atlas Robotics",
        "software",
        "Atlas Robotics builds warehouse automation and fleet-orchestration software.",
        "701 N 34th St",
        "Seattle",
        "Washington",
        "98103",
        "206-555-0164",
        "hello@atlasrobotics.ai",
        "https://atlasrobotics.ai",
        "201-1000 employees",
        "Founded 2016",
        ["Robotics", "Automation", "Warehouse"],
    ),
    Company(
        # Exact duplicate content under a different listing id -- the easy case.
        "atlas-robotics-inc",
        "Atlas Robotics, Inc.",
        "software",
        "Atlas Robotics builds warehouse automation and fleet-orchestration software.",
        "701 North 34th Street",
        "Seattle",
        "WA",
        "98103",
        "+12065550164",
        "hello@atlasrobotics.ai",
        "atlasrobotics.ai",
        "500",
        "2016",
        ["Robotics", "Automation"],
        template="drifted",
    ),
    Company(
        "quarry-lane-foods",
        "Quarry Lane Foods",
        "manufacturing",
        "Quarry Lane Foods is a co-packer for specialty sauces and shelf-stable condiments.",
        "3400 Industrial Pkwy",
        "Columbus",
        "Ohio",
        "43204",
        "(614) 555-0155",
        "orders@quarrylanefoods.com",
        "https://quarrylanefoods.com",
        "51-200 employees",
        "Founded 1987",
        ["Food", "Co-packing"],
    ),
    Company(
        "vantage-grid",
        "Vantage Grid Energy",
        "energy",
        "Vantage Grid Energy develops utility-scale battery storage projects.",
        "600 17th St",
        "Denver",
        "Colorado",
        "80202",
        "720-555-0133",
        "projects@vantagegrid.com",
        "https://vantagegrid.com",
        "11-50 employees",
        "Founded 2020",
        ["Energy Storage", "Renewables"],
    ),
    Company(
        # Stub page: barely any content. Tests the low-confidence path.
        "silverpine-holdings",
        "Silverpine Holdings",
        "finance",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        [],
        template="stub",
    ),
]

BY_CATEGORY: dict[str, list[Company]] = {}
for _company in COMPANIES:
    BY_CATEGORY.setdefault(_company.category, []).append(_company)

BY_SLUG: dict[str, Company] = {c.slug: c for c in COMPANIES}

# Enrichment records, keyed by domain. Intentionally incomplete: some companies
# have no enrichment record at all, which is the normal case in production.
ENRICHMENT: dict[str, dict] = {
    "northwindanalytics.com": {
        "id": "ent_8812",
        "industry": "Software",
        "naics": "541511",
        "revenue_usd": 24_000_000,
        "employee_count": 138,
        "linkedin_url": "https://linkedin.com/company/northwind-analytics",
        "technologies": ["Snowflake", "dbt", "Kafka", "React"],
        "confidence": 0.93,
    },
    "harborpointlabs.io": {
        "id": "ent_4410",
        "industry": "Software",
        "naics": "541511",
        "revenue_usd": 6_500_000,
        "employee_count": 34,
        "linkedin_url": "https://linkedin.com/company/harbor-point-labs",
        "technologies": ["PyTorch", "ROS", "AWS"],
        "confidence": 0.88,
    },
    "cascadefreight.com": {
        "id": "ent_2201",
        "industry": "Transportation",
        "naics": "484121",
        "revenue_usd": 145_000_000,
        "employee_count": 820,
        "linkedin_url": "https://linkedin.com/company/cascade-freight",
        "technologies": ["SAP", "Samsara"],
        "confidence": 0.91,
    },
    "atlasrobotics.ai": {
        "id": "ent_9007",
        "industry": "Robotics",
        "naics": "333922",
        "revenue_usd": 58_000_000,
        "employee_count": 410,
        "linkedin_url": "https://linkedin.com/company/atlas-robotics",
        "technologies": ["ROS2", "Kubernetes", "Go", "Rust"],
        "confidence": 0.95,
    },
    "meridianhealthpartners.org": {
        "id": "ent_6613",
        "industry": "Healthcare",
        "naics": "621111",
        "revenue_usd": 310_000_000,
        "employee_count": 1240,
        "linkedin_url": "https://linkedin.com/company/meridian-health",
        "technologies": ["Epic", "Azure"],
        "confidence": 0.89,
    },
    "brightlinecap.com": {
        "id": "ent_3345",
        "industry": "Financial Services",
        "naics": "523930",
        "revenue_usd": 19_000_000,
        "employee_count": 28,
        "linkedin_url": "https://linkedin.com/company/brightline-capital",
        "technologies": ["Salesforce", "DealCloud"],
        "confidence": 0.84,
    },
    "vantagegrid.com": {
        "id": "ent_7788",
        "industry": "Energy",
        "naics": "221118",
        "revenue_usd": 41_000_000,
        "employee_count": 47,
        "linkedin_url": "https://linkedin.com/company/vantage-grid",
        "technologies": ["Python", "GIS", "Terraform"],
        "confidence": 0.86,
    },
}
