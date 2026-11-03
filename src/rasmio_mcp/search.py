"""Search tools: companies with the site's filters, the other record kinds, and the lookups that give filter ids."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from pydantic import Field

from .common import clean, jalali, to_iso
from .http import ApiError, fetch
from .registry import tool

SORTS = {"relevance": 0, "most_viewed": 1, "newest": 2, "oldest": 3, "lowest_capital": 4, "highest_capital": 5}
STATUSES = {
    "active": 1,
    "tax_inactive": 2,
    "liquidated": 3,
    "incomplete_data": 4,
    "revoked": 5,
    "license_ended": 6,
    "inactive": 7,
}
KINDS = {"people": 1, "products": 2, "brands": 4, "patents": 5}
MARK_TYPES = {"image": 1, "text": 2, "combined": 3}


def _person_id(link: str) -> str | None:
    """The hex id from a person link (/person/3706E5AB/<name>/). The hit's own `id` is a national code: never shown."""
    m = re.match(r"/person/([0-9A-Fa-f]+)/", link or "")
    return m.group(1) if m else None


def _company_hit(h: dict) -> dict:
    c = h.get("companySearchHit") or {}
    return {
        "company_id": h.get("id"),
        "name": clean(h.get("title")),
        "summary": clean(h.get("subTitle")),
        "info": clean(h.get("information")),
        "ceo": clean(c.get("ceo")),
        "verified_by_company": c.get("isVerified") or None,
        "licenses": [clean(x.get("title")) for x in (c.get("certificates") or [])[:5]],
        "license_count": len(c.get("certificates") or []) or None,
        "top_products": [clean(p.get("title"))[:120] for p in (c.get("topProducts") or [])[:3]],
        "top_categories": [
            clean(x.get("title") if isinstance(x, dict) else x) for x in (c.get("topCategories") or [])[:3]
        ],
    }


def _hit(h: dict) -> dict:
    """One search hit of any kind, trimmed to what helps pick the next call."""
    kind = h.get("type")
    if kind == 0:
        return {"kind": "company", **_company_hit(h)}
    if kind == 1:
        p = h.get("personSearchHit") or {}
        return {
            "kind": "person",
            "person_id": _person_id(h.get("link", "")),
            "name": clean(h.get("title")),
            "summary": clean(h.get("subTitle")),
            "company_count": p.get("totalCompanyCount"),
            "companies": [
                {
                    "name": clean(c.get("title")),
                    "company_id": (re.findall(r"/company/(\d+)", c.get("url", "")) or [None])[0],
                }
                for c in (p.get("topCompanies") or [])[:5]
            ],
        }
    if kind == 2:
        p = h.get("productSearchHit") or {}
        return {
            "kind": "product",
            "product_id": h.get("id"),
            "title": clean(h.get("title")),
            "brand": clean(p.get("brand")),
            "country": clean(p.get("countryTitle")),
            "supplier": clean(p.get("companyTitle")),
            "supplier_company_id": str(p["companyId"]) if p.get("companyId") else None,
            "category": clean((p.get("lastCategory") or {}).get("title")),
            "category_slug": (p.get("lastCategory") or {}).get("slug"),
            "min_order_price": clean(p.get("minimumOrderPrice")),
            "specs": {clean(x.get("title")): clean(x.get("value")) for x in (p.get("characteristics") or [])[:8]},
        }
    ip = h.get("ipSearchHit") or {}
    link = unquote(h.get("link", ""))
    return {
        "kind": "brand" if kind == 4 else "industrial_design" if "/industrial-design/" in link else "patent",
        "ip_id": h.get("id"),
        "title": clean(h.get("title")),
        "owner": clean(ip.get("ownerText") or h.get("subTitle")),
        "owner_company_id": str(ip["ownerCompanyId"]) if ip.get("ownerCompanyId") else None,
        "registration_no": ip.get("registrationNo"),
        "registered": jalali(ip.get("registrationDate")),
        "inventors": clean(ip.get("inventorsText")),
        "classes": clean(ip.get("categoriesText"))[:200],
    }


def _page(d: dict, page: int, size: int, hits: list) -> dict:
    total = d.get("totalCount") or 0
    return {
        "total": total,
        "page": page,
        "pages": -(-total // size) if total else 0,
        "results": [_hit(h) for h in hits],
    }


async def _search(params: dict, page: int, size: int) -> dict:
    d = await fetch("/api/search/Search", {**params, "page": page - 1, "pageSize": size}, api=True)
    return _page(d, page, size, (d.get("data") or {}).get("hits") or [])


@tool("Search companies")
async def rs_search_companies(
    query: Annotated[
        str,
        Field(
            description="Name, brand or activity words in Persian, e.g. 'نرم افزار' or 'دیجی کالا'. Empty = filters only."
        ),
    ] = "",
    province: Annotated[
        str | None,
        Field(description="Registered province, Persian name as the site writes it, e.g. 'تهران', 'اصفهان'."),
    ] = None,
    city: Annotated[str | None, Field(description="Registered city, Persian name, e.g. 'مشهد'.")] = None,
    status: Annotated[
        Literal["active", "tax_inactive", "liquidated", "incomplete_data", "revoked", "license_ended", "inactive"]
        | None,
        Field(description="Company status."),
    ] = None,
    company_type: Annotated[
        int | None,
        Field(
            description="Legal form id from rs_options('company_types'); 5 = سهامی خاص, 6 = با مسئولیت محدود, 3 = تعاونی."
        ),
    ] = None,
    size: Annotated[
        Literal["xs", "s", "m", "l", "xl"] | None, Field(description="Headcount class: xs very small .. xl very large.")
    ] = None,
    min_capital: Annotated[
        int | None, Field(description="Minimum registered capital in Rial, e.g. 10000000000.", ge=0)
    ] = None,
    max_capital: Annotated[int | None, Field(description="Maximum registered capital in Rial.", ge=0)] = None,
    founded_after: Annotated[
        str | None, Field(description="Registration date from, Persian '1400/01/01' or Gregorian '2021-03-21'.")
    ] = None,
    founded_before: Annotated[str | None, Field(description="Registration date to, same formats.")] = None,
    license_id: Annotated[
        str | None, Field(description="Only companies holding this license; id from rs_options('licenses', query).")
    ] = None,
    product_category: Annotated[
        str | None,
        Field(
            description="Only suppliers in this product category slug, from rs_options('categories') or a product's category_slug."
        ),
    ] = None,
    has_brand: Annotated[bool, Field(description="Only companies that own a registered trademark.")] = False,
    has_subsidiary: Annotated[bool, Field(description="Only companies with subsidiaries.")] = False,
    has_contract: Annotated[bool, Field(description="Only companies with recorded government contracts.")] = False,
    sort: Annotated[
        Literal["relevance", "most_viewed", "newest", "oldest", "lowest_capital", "highest_capital"],
        Field(description="Order of results."),
    ] = "relevance",
    page: Annotated[int, Field(description="Result page, from 1.", ge=1, le=200)] = 1,
    page_size: Annotated[int, Field(description="Results per page.", ge=1, le=60)] = 20,
) -> dict[str, Any]:
    """Search Iranian companies by name or activity with filters; returns national ids for rs_company.

    Each result has the company's national id (company_id, شناسه ملی), name, age/capital summary, CEO and up to 5
    licenses. Combine filters freely, e.g. active software companies in Isfahan with capital over 10 billion Rial.
    Next: rs_company(company_id) for the full registry profile (board, shareholders, gazette ads).
    """
    extra = {
        "companystatus": STATUSES.get(status) if status else None,
        "companyregistrationtype": company_type,
        "companysize": size,
        "capital-min": min_capital,
        "capital-max": max_capital,
        "registrationdate-start": to_iso(founded_after),
        "registrationdate-end": to_iso(founded_before),
        "certificate": license_id,
        "has-brand": "true" if has_brand else None,
        "has-subsidiary": "true" if has_subsidiary else None,
        "has-contract": "true" if has_contract else None,
    }
    params = {
        "q": query.strip(),
        "type": 0,
        "provinces": province,
        "cities": city,
        "slug": product_category,
        "sort": SORTS[sort],
        **{f"additionalFilters[{k}]": v for k, v in extra.items() if v is not None},
    }
    return await _search(params, page, page_size)


@tool("Search people, products, brands or patents")
async def rs_search(
    query: Annotated[
        str, Field(description="Words in Persian (or Latin for brands), e.g. 'ملک نژاد', 'روغن موتور', 'Samsung'.")
    ],
    kind: Annotated[
        Literal["people", "products", "brands", "patents", "everything"],
        Field(description="What to search. 'everything' mixes companies, people, brands and products."),
    ] = "people",
    product_category: Annotated[
        str | None,
        Field(description="products only: category slug from rs_options('categories'), e.g. 'metals-chemicals'."),
    ] = None,
    brand_class: Annotated[
        int | None, Field(description="brands only: Nice class 1-45, e.g. 9 = electronics, 30 = foods.", ge=1, le=45)
    ] = None,
    mark_type: Annotated[
        Literal["image", "text", "combined"] | None, Field(description="brands only: kind of mark.")
    ] = None,
    patent_kind: Annotated[Literal["invention", "industrial_design"] | None, Field(description="patents only.")] = None,
    page: Annotated[int, Field(description="Result page, from 1.", ge=1, le=200)] = 1,
    page_size: Annotated[int, Field(description="Results per page.", ge=1, le=60)] = 20,
) -> dict[str, Any]:
    """Search people (board members, managers), products, registered brands or patents and industrial designs.

    People results give person_id for rs_person and the companies they sit in. Products give product_id for
    rs_product and the supplier's company_id. Brands and patents give ip_id for rs_ip. For companies use
    rs_search_companies (it has filters).
    """
    if not query.strip():
        raise ApiError("Give a search word.")
    if kind == "everything":
        d = await fetch(
            "/api/search/search/all", {"q": query.strip(), "page": page - 1, "pageSize": page_size}, api=True
        )
        return _page(d, page, page_size, d.get("hits") or [])
    extra = {
        "ip-class": brand_class if kind == "brands" else None,
        "ip-mark-type": MARK_TYPES[mark_type] if kind == "brands" and mark_type else None,
        "ip-kind": {"invention": 2, "industrial_design": 3}[patent_kind] if kind == "patents" and patent_kind else None,
    }
    params = {
        "q": query.strip(),
        "type": KINDS[kind],
        "slug": product_category if kind == "products" else None,
        "sort": 0,
        **{f"additionalFilters[{k}]": v for k, v in extra.items() if v is not None},
    }
    return await _search(params, page, page_size)


@tool("Filter values and categories")
async def rs_options(
    what: Annotated[
        Literal["company_types", "licenses", "categories", "brand_classes", "provinces"],
        Field(
            description="Which list: company legal forms, license names (needs query), product categories, trademark classes, provinces with their cities."
        ),
    ],
    query: Annotated[
        str | None,
        Field(
            description="licenses: words of the license name, e.g. 'دانش بنیان', 'ایزو'. company_types/provinces: optional text filter."
        ),
    ] = None,
    category: Annotated[
        str | None,
        Field(
            description="categories: a category slug to list its sub-categories, e.g. 'metals-chemicals'. Empty = top level."
        ),
    ] = None,
) -> dict[str, Any]:
    """List the ids and names the search filters take: legal forms, licenses, product categories, brand classes.

    Use the returned id/slug in rs_search_companies (company_type, license_id, product_category, province, city)
    or rs_search (product_category, brand_class). counts are how many companies or products carry each value.
    """
    q = (query or "").strip()
    if what == "licenses":
        if not q:
            raise ApiError("Give query: words from the license name, e.g. 'دانش بنیان'.")
        rows = await fetch("/api/search/Search/company-certificates", {"q": q}, api=True)
        items = [
            {
                "license_id": r.get("value"),
                "name": clean(r.get("fa")),
                "issuer": clean(r.get("issuer")),
                "companies": r.get("companyCount"),
            }
            for r in rows or []
        ]
        return {"items": items}
    if what == "categories":
        d = await fetch("/api/search/Search/get-specific-category", {"type": 2, "slug": category}, api=True)
        roots = d.get("rootCategories") or []
        if category:
            root = next((r for r in roots if r.get("slug") == category), None)
            cat = d.get("category") or root or {}
            subs = cat.get("subCategories") or (root or {}).get("subCategories") or []
        else:
            cat, subs = {}, roots
        return {
            "category": clean(cat.get("title")) or None,
            "path": [clean(b.get("title") or b.get("name")) for b in d.get("breadCrumbs") or [] if isinstance(b, dict)],
            "items": [
                {
                    "slug": s.get("slug"),
                    "name": clean(s.get("title")),
                    "products": s.get("docCount"),
                    "has_children": not s.get("lastLevel"),
                }
                for s in subs
            ],
        }
    if what == "provinces":
        d = await fetch("/api/search/Search", {"q": "", "type": 0, "page": 0, "pageSize": 1}, api=True)
        provinces = (d.get("data") or {}).get("provinces") or []
        return {
            "items": [
                {"province": p.get("title"), "cities": p.get("cities")}
                for p in provinces
                if q in (p.get("title") or "")
            ]
        }
    d = await fetch("/api/search/Filter/get-filters", {"Type": 4 if what == "brand_classes" else 0}, api=True)
    filters = {f.get("id"): f for f in (d.get("value") or {}).get("filters") or []}
    fid = "ip-class" if what == "brand_classes" else "companyregistrationtype"
    values = (filters.get(fid) or {}).get("values") or []
    return {"items": [{"id": int(v["value"]), "name": clean(v.get("fa"))} for v in values if q in (v.get("fa") or "")]}
