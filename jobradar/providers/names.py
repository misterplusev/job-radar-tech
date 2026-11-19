"""Canonical company names for ATS slugs.

Lever and Ashby boards mostly do not report the employer name, so providers
fall back to title-casing the slug - which turns `shieldai` into "Shieldai" and
`helm-ai` into "Helm Ai". Greenhouse reports a name for most boards but not all.

One shared map keeps the same employer spelled the same way no matter which ATS
it arrives through, which matters because the relevance gate keys off company.
"""
from __future__ import annotations

from typing import Optional

BOARD_NAMES = {
    # --- greenhouse ---
    "anthropic": "Anthropic",
    "databricks": "Databricks",
    "stripe": "Stripe",
    "coinbase": "Coinbase",
    "figma": "Figma",
    "airbnb": "Airbnb",
    "robinhood": "Robinhood",
    "waymo": "Waymo",
    "nuro": "Nuro",
    "flyzipline": "Zipline",
    "motional": "Motional",
    "kodiak": "Kodiak Robotics",
    "agilityrobotics": "Agility Robotics",
    "figureai": "Figure AI",
    "apptronik": "Apptronik",
    "andurilindustries": "Anduril Industries",
    "locusrobotics": "Locus Robotics",
    "diligentrobotics": "Diligent Robotics",
    "spacex": "SpaceX",
    "rocketlab": "Rocket Lab",
    "relativity": "Relativity Space",
    "vast": "Vast Space",
    "astranis": "Astranis",
    "machindustries": "Mach Industries",
    "planetlabs": "Planet Labs",
    "ursamajor": "Ursa Major",
    "lgelectronics": "LG Electronics",
    "wing": "Wing",
    "torcrobotics": "Torc Robotics",
    "epirus": "Epirus",
    "maymobility": "May Mobility",
    "dexory": "Dexory",
    "pronto": "Pronto.ai",
    "outrider": "Outrider",
    "noahmedical": "Noah Medical",
    # --- lever ---
    "zoox": "Zoox",
    "waabi": "Waabi",
    "shieldai": "Shield AI",
    "anybotics": "ANYbotics",
    "dexterity": "Dexterity",
    "brightmachines": "Bright Machines",
    "weride": "WeRide",
    "tri": "Toyota Research Institute",
    "cobaltrobotics": "Cobalt Robotics",
    "osaro": "Osaro",
    "picklerobot": "Pickle Robot",
    "ambirobotics": "Ambi Robotics",
    # --- ashby ---
    "ramp": "Ramp",
    "openai": "OpenAI",
    "wayve": "Wayve",
    "1x": "1X",
    "physicalintelligence": "Physical Intelligence",
    "standardbots": "Standard Bots",
    "saronic": "Saronic",
    "skydio": "Skydio",
    "serverobotics": "Serve Robotics",
    "brinc": "Brinc Drones",
    "swoop": "Swoop Aero",
    "helm-ai": "Helm.ai",
    "impulse": "Impulse Space",
    "oxa": "Oxa",
    # --- workday ---
    "bostondynamics": "Boston Dynamics",
    "autostore": "AutoStore",
    "rockwellautomation": "Rockwell Automation",
}


def board_company(slug: str, reported: Optional[str] = None) -> str:
    """Prefer the board's own name; fall back to the map, then the slug."""
    name = (reported or "").strip()
    if name:
        return name
    if slug in BOARD_NAMES:
        return BOARD_NAMES[slug]
    return slug.replace("-", " ").title()
