"""Person profile: the companies someone manages, sits on the board of or audits."""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import Field

from .common import clean, jalali, query_data, soup
from .http import ApiError, fetch
from .registry import tool

PUBLIC_ADS = 5  # the person page shows the 5 latest ads to anonymous visitors


def parse_person(html: str) -> dict[str, Any]:
    soup(html)  # raises on the anonymous-quota page
    data = query_data(html, "person") or {}
    p = data.get("person")
    if not p:
        raise ApiError("Rasmio returned a page without this person. Check the person_id.")
    rc = p.get("relatedCompanies") or {}
    bio = p.get("biography") or {}
    # Contact details, national code and social accounts in the page data are left out on purpose.
    return {
        "name": clean(p.get("title")),
        "verified": p.get("isVerified") or None,
        "id_issued_in": clean(bio.get("city")),
        "company_count": rc.get("companiesCount"),
        "roles_count": {clean(r.get("role")): r.get("count") for r in rc.get("rolesCount") or []},
        "positions": [
            {
                "company_id": c.get("companyId"),
                "company": clean(c.get("companyTitle")),
                "position": clean(c.get("positionTitle")),
                "roles": [clean(r) for r in c.get("allRoles") or []],
                "representing": clean(c.get("representingTitle")),
                "start": jalali(c.get("startDate")),
                "end": jalali(c.get("endDate")),
                "current": not c.get("isFinished"),
                "ad_id": c.get("byNewsId"),
            }
            for c in rc.get("companies") or []
        ],
        "recent_ads": [
            {
                "ad_id": n.get("id"),
                "title": clean(n.get("title")),
                "published": jalali(n.get("newsPaperDate")),
                "text": clean(n.get("description"))[:400],
            }
            for n in (p.get("news") or [])[:PUBLIC_ADS]
        ],
    }


@tool("Person profile")
async def rs_person(
    person_id: Annotated[
        str,
        Field(description="Hex id from rs_search(kind='people') or rs_company, e.g. '3706E5AB'."),
    ],
) -> dict[str, Any]:
    """Companies one person manages, sits on the board of or audits, with role dates and their latest gazette ads.

    current=false is a finished term; ad_id opens with rs_ad.
    """
    pid = clean(person_id).strip().upper()
    if not re.fullmatch(r"[0-9A-F]{4,12}", pid):
        raise ApiError("person_id is the short hex id from rs_search(kind='people'), e.g. '3706E5AB'.")
    return parse_person(await fetch(f"/person/{pid}/x/"))
