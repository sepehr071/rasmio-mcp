"""Product page and intellectual-property records (trademarks, patents, industrial designs)."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from pydantic import Field

from .common import clean, flight, jalali, soup, table
from .http import ApiError, fetch
from .registry import tool

_SPEC = re.compile(r'\{"key":"[cd]-')
_CRUMBS = re.compile(r'"data":(\[\{"name":.*?\]),"baseUrl":"/search/products"')
IP_PATHS = {
    "brand": "/api/v4/Trademarks/{}",
    "patent": "/api/v4/Trademarks/patent-declarations/{}",
    "industrial_design": "/api/v4/Trademarks/industrial-designs/{}",
}


def parse_product(html: str, max_rows: int = 10) -> dict[str, Any]:
    page = soup(html)
    title = page.find(id="product-title")
    if title is None:
        raise ApiError("Rasmio returned a page without this product. Check the product_id.")
    payload = flight(html)
    specs: dict[str, str] = {}
    supplier_id = None
    decoder = json.JSONDecoder()
    for m in _SPEC.finditer(payload):
        try:
            row, _ = decoder.raw_decode(payload, m.start())
        except ValueError:
            continue
        label, value = clean(row.get("label")), clean(str(row.get("value") or ""))
        if label and value and not row.get("isMissing") and label not in specs:
            specs[label] = value
        if row.get("isCompany") and (cid := re.search(r"/company/(\d+)", row.get("href") or "")):
            supplier_id = supplier_id or cid.group(1)
    crumbs = _CRUMBS.search(payload)
    path = json.loads(crumbs.group(1)) if crumbs else []
    suppliers = None
    for t in page.find_all("table"):
        heads = [clean(th.get_text(strip=True)) for th in t.find_all("th")]
        if heads[:1] == ["شرکت"]:
            suppliers = table(t, max_rows)
            break
    return {
        "title": clean(title.get_text(" ", strip=True)),
        "category_path": [clean(c.get("name")) for c in path],
        "category_slug": (re.findall(r"/search/products/([^/]+)/", path[-1].get("url", "")) or [None])[0]
        if path
        else None,
        "supplier_company_id": supplier_id,
        "specs": specs,
        "similar_suppliers": suppliers,
    }


@tool("Product details")
async def rs_product(
    product_id: Annotated[str, Field(description="Product id from rs_search(kind='products'), e.g. '42218435'.")],
    max_rows: Annotated[int, Field(description="Rows of the similar-suppliers table.", ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """One registered product: full specs (HS/ISIC codes, brand, maker, country), category and comparable suppliers.

    similar_suppliers lists other companies with the same or a close product (company_id for rs_company). The
    category_slug works as product_category in rs_search_companies to find every supplier of the category.
    """
    pid = re.sub(r"\D", "", clean(product_id))
    if not pid:
        raise ApiError("product_id is a number, e.g. '42218435'.")
    return parse_product(await fetch(f"/product/{pid}/x/"), max_rows)


@tool("Trademark, patent or design")
async def rs_ip(
    ip_id: Annotated[
        str,
        Field(description="ip_id from rs_search(kind='brands' or 'patents') or a brand in rs_company, e.g. '812386'."),
    ],
    kind: Annotated[
        Literal["brand", "patent", "industrial_design"],
        Field(description="Record type, as the search result's kind says."),
    ] = "brand",
) -> dict[str, Any]:
    """Registration details of an Iranian trademark, patent or industrial design: numbers, date, owner, description."""
    iid = re.sub(r"\D", "", clean(ip_id))
    if not iid:
        raise ApiError("ip_id is a number, e.g. '812386'.")
    d = await fetch(IP_PATHS[kind].format(iid), api=True)
    v = d.get("value") or {}
    if not v:
        raise ApiError(f"No {kind} with id {iid}.")
    owners = [
        {"name": clean(o.get("title")), "company_id": o.get("companyId")}
        for o in v.get("owners") or []
        if o.get("companyId")
    ]
    return {
        "ip_id": v.get("id"),
        "kind": kind,
        "title": clean(v.get("title") or v.get("titleFa") or v.get("titleEn")),
        "title_en": clean(v.get("titleEn")) if v.get("titleFa") else None,
        "declaration_no": v.get("declarationNo"),
        "registration_no": v.get("registrationNo"),
        "registered": jalali(v.get("registrationDate")),
        "owner": clean(v.get("ownerText")),
        "owner_companies": owners,
        "inventors": clean(v.get("inventorsText")),
        "classes": clean(v.get("categories"))
        or [clean(c.get("title") if isinstance(c, dict) else c) for c in v.get("productCategories") or []],
        "description": clean(v.get("description"))[:2000],
        "image": v.get("pictureUrl") or v.get("logo"),
    }
