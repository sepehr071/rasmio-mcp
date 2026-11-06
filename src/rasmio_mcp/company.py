"""Company profile (server-rendered page) and official-gazette ad tools."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from bs4 import Tag
from pydantic import Field

from .common import clean, jalali, link_ids, number, soup, table
from .http import ApiError, fetch
from .registry import tool

# Page section id -> output key. Only sections anonymous visitors can see; the rest show a subscribe wall.
TABLES = {
    "managers": "board",
    "other-members": "other_officers",
    "inspectors": "auditors",
    "shareholders": "shareholders",
    "stock-portfolio": "stock_portfolio",
    "certificates": "licenses",
    "capital-changes": "capital_changes",
    "contracts": "contracts",
    "financial-statements": "financial_statements",
    "industrial-design-and-patent-declarations": "patents_and_designs",
}
SECTIONS = Literal["people", "ownership", "licenses", "finance", "ads"]
GROUPS = {
    "people": ("board", "other_officers", "auditors", "signature_rule"),
    "ownership": ("shareholders", "stock_portfolio"),
    "licenses": ("licenses", "brands", "patents_and_designs"),
    "finance": ("capital_changes", "contracts", "financial_statements"),
    "ads": ("recent_ads",),
}
# Labels of the basic-information block, in page order -> output key.
BASIC = {
    "تاریخ تاسیس": "founded",
    "وضعیت شرکت": "status",
    "نوع شرکت": "legal_form",
    "آخرین آگهی روزنامه رسمی": "last_gazette_ad",
    "شماره ثبت": "registration_no",
    "کد اقتصادی": "economic_code",
    "شناسه ملی": "company_id",
    "سرمایه ثبتی": "capital",
    "موضوع فعالیت": "activity",
}


def _id(company_id: str) -> str:
    cid = re.sub(r"\D", "", clean(company_id))
    if not 10 <= len(cid) <= 12:
        raise ApiError(
            "company_id is the 11-digit national id (شناسه ملی), e.g. 10100360794; find it with rs_search_companies."
        )
    return cid


def _basic(el: Tag | None) -> dict[str, Any]:
    """Label/value pairs of the basic-information block, plus the registered and factory addresses."""
    if el is None:
        return {}
    tokens = [clean(t) for t in el.stripped_strings]
    tokens = [t for t in tokens if t and t != ":"]
    out: dict[str, Any] = {}
    for i, tok in enumerate(tokens[:-1]):
        if tok in BASIC and BASIC[tok] not in out:
            out[BASIC[tok]] = tokens[i + 1]
        elif tok in ("نشانی ثبتی شرکت", "نشانی ثبتی کارخانه"):
            key = "address" if tok.endswith("شرکت") else "factory_address"
            # province, city, street address follow the label
            out[key] = " ، ".join(t for t in tokens[i + 1 : i + 4] if t not in ("کد پستی", "مشاهده موقعیت شرکت"))
        elif tok == "کد پستی":
            out["postal_code"] = tokens[i + 1]
        elif tok == "شرکت در این دسته" and i >= 2:
            out["industry"] = tokens[i - 2]
    if "capital" in out:
        out["capital_rial"] = number(out.pop("capital"))
    if "activity" in out:
        out["activity"] = out["activity"].removesuffix("... مشاهده بیشتر").strip()[:1500]
    return out


def _ads(el: Tag | None, max_rows: int) -> list[dict]:
    """Ad cards of the advertisements section: source, dates, title and the start of the text."""
    if el is None:
        return []
    out = []
    for a in el.find_all("a", href=re.compile(r"^/(News|codal)/\d+")):
        card = a
        while card.parent is not None and card.parent is not el and "تاریخ چاپ" not in card.get_text():
            card = card.parent
        tokens = [clean(t) for t in card.stripped_strings]
        tokens = [t for t in tokens if t and t != ":"]
        ad: dict[str, Any] = {
            **link_ids(a),
            "source": tokens[0] if tokens else None,
            "title": clean(a.get_text(" ", strip=True)),
        }
        for label, key in (("تاریخ چاپ:", "published"), ("تاریخ نامه ثبت:", "registered")):
            if label in tokens and tokens.index(label) + 1 < len(tokens):
                ad[key] = tokens[tokens.index(label) + 1]
        rest = tokens[tokens.index(ad["title"]) + 1 :] if ad["title"] in tokens else []
        ad["text"] = " ".join(rest)[:600]
        out.append(ad)
    return out[:max_rows]


def _brands(el: Tag | None, max_rows: int) -> list[dict]:
    if el is None:
        return []
    out = []
    for a in el.find_all("a", href=re.compile(r"^/brand/\d+")):
        tokens = [clean(t) for t in a.stripped_strings if clean(t) not in (":", "تاریخ ثبت")]
        out.append(
            {
                "ip_id": link_ids(a).get("brand_id"),
                "name": tokens[0] if tokens else None,
                "registered": tokens[-1] if len(tokens) > 1 else None,
            }
        )
    return out[:max_rows]


def parse_company(html: str, max_rows: int = 10) -> dict[str, Any]:
    page = soup(html)
    h1 = page.find("h1")
    sig = page.find(id="signature-holders")
    sig_text = [clean(t) for t in sig.stripped_strings] if sig else []
    sig_text = [t for t in sig_text if t not in ("دارندگان حق امضا", "منابع", ":", "بیشتر ...")]
    out: dict[str, Any] = {
        "name": clean(h1.get_text(" ", strip=True)) if h1 else None,
        **_basic(page.find(id="basic-information")),
        "signature_rule": sig_text[0] if sig_text else None,
        "recent_ads": _ads(page.find(id="advertisements"), max_rows),
        "brands": _brands(page.find(id="trademarks"), max_rows),
    }
    for sid, key in TABLES.items():
        out[key] = table(page.find(id=sid), max_rows)
    if not out.get("company_id"):
        raise ApiError("Rasmio returned a page without company data. Check the company_id.")
    return out


@tool("Company profile")
async def rs_company(
    company_id: Annotated[
        str,
        Field(description="11-digit national id (شناسه ملی) from rs_search_companies, e.g. '10100360794'."),
    ],
    sections: Annotated[
        list[SECTIONS] | None,
        Field(description="Parts besides the basics; empty = all."),
    ] = None,
    max_rows: Annotated[int, Field(description="Rows per table.", ge=1, le=30)] = 10,
) -> dict[str, Any]:
    """Registry profile of one Iranian company: basics, board and CEO, shareholders, licenses, capital history, ads.

    sections: people=board/officers/auditors, ownership=shareholders/stock, licenses=licenses/brands/patents,
    finance=capital changes/contracts/statements, ads. Board rows give person_id (rs_person) and ad_id (rs_ad). Anonymous quota is a few profiles per IP every few
    days; the error says when it is reached.
    """
    html = await fetch(f"/company/{_id(company_id)}/direct/")
    data = parse_company(html, max_rows)
    if sections:
        keep = {k for s in sections for k in GROUPS[s]}
        drop = {k for g in GROUPS.values() for k in g} - keep
        data = {k: v for k, v in data.items() if k not in drop}
    return data


@tool("Official gazette ad")
async def rs_ad(
    ad_id: Annotated[
        str,
        Field(description="Ad id from rs_company or rs_person, e.g. '129676674'."),
    ],
) -> dict[str, Any]:
    """Full text of one official gazette (روزنامه رسمی) ad: board changes, capital changes, founding, liquidation.

    National codes in the text are shortened to 6 digits.
    """
    aid = re.sub(r"\D", "", clean(ad_id))
    if not aid:
        raise ApiError("ad_id is a number, e.g. '129676674'.")
    d = await fetch(f"/api/v4/news/{aid}", api=True, referer=f"https://rasmio.com/News/{aid}/")
    v = d.get("value") or {}
    if not v:
        raise ApiError(f"No ad with id {aid}.")
    return {
        "ad_id": v.get("id"),
        "title": clean(v.get("fixedTitle") or v.get("title")),
        "text": clean(v.get("description")),
        "published": jalali(v.get("newspaperDate")),
        "registered": jalali(v.get("newsLetterDate")),
        "newspaper_no": v.get("newsPaperNumber"),
        "page": v.get("pageNumber"),
        "edition": clean(v.get("newsPaperCityType")),
        "indicator_no": v.get("indicatorNumber"),
        "kind": clean(v.get("newsType")),
        "capital_from_rial": v.get("capitalFrom"),
        "capital_to_rial": v.get("capitalTo"),
        "signature_rule": clean(v.get("signatureHolder")),
    }
