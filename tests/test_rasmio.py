import httpx
import pytest
from conftest import fixture, page

from rasmio_mcp.common import clean, jalali, to_iso

pytestmark = pytest.mark.anyio


def test_dates_and_masking():
    assert to_iso("1402/01/15") == "2023-04-04T00:00:00.000Z"
    assert to_iso("۱۴۰۰-۰۱-۰۱") == "2021-03-21T00:00:00.000Z"
    assert to_iso("2024-01-01") == "2024-01-01T00:00:00.000Z"
    assert jalali("2025-10-06T00:00:00") == "1404/07/14"
    assert clean("آقای الف به شماره ملی 1234567890 به سمت") == "آقای الف به شماره ملی 123456xxxx به سمت"
    assert clean("شناسه ملی 14009396050") == "شناسه ملی 14009396050"  # company ids stay


async def test_search_companies_sends_site_filters(api, client):
    api["/api/search/Search"] = fixture("search_companies.json")
    r = await client.call_tool(
        "rs_search_companies",
        {
            "query": "رسمیو",
            "province": "تهران",
            "status": "active",
            "min_capital": 10000000000,
            "founded_after": "1399/01/01",
            "license_id": "38075",
            "has_brand": True,
            "sort": "highest_capital",
            "page": 2,
            "page_size": 3,
        },
    )
    q = api.calls[0].url.params
    assert q["type"] == "0" and q["page"] == "1" and q["pageSize"] == "3" and q["sort"] == "5"
    assert q["provinces"] == "تهران"
    assert q["additionalFilters[companystatus]"] == "1"
    assert q["additionalFilters[capital-min]"] == "10000000000"
    assert q["additionalFilters[registrationdate-start]"] == "2020-03-20T00:00:00.000Z"
    assert q["additionalFilters[certificate]"] == "38075"
    assert q["additionalFilters[has-brand]"] == "true"
    assert "additionalFilters[has-contract]" not in q
    hit = r.structured_content["results"][0]
    assert hit["kind"] == "company" and hit["company_id"] == "14009396050"
    assert hit["name"] and hit["ceo"]


async def test_people_search_hides_national_code(api, client):
    body = fixture("search_people.json")
    api["/api/search/Search"] = body
    r = await client.call_tool("rs_search", {"query": "ملک نژاد", "kind": "people"})
    hits = r.structured_content["results"]
    assert hits and all(h["kind"] == "person" and h["person_id"] for h in hits)
    raw_ids = {h["id"] for h in body["data"]["hits"]}
    assert not any(i in r.content[0].text for i in raw_ids)


async def test_search_kinds(api, client):
    api["/api/search/Search"] = fixture("search_patents.json")
    api["/api/search/search/all"] = fixture("search_all.json")
    r = await client.call_tool("rs_search", {"query": "صندلی", "kind": "patents", "patent_kind": "industrial_design"})
    assert api.calls[0].url.params["additionalFilters[ip-kind]"] == "3"
    assert r.structured_content["results"][0]["kind"] in ("patent", "industrial_design")
    r = await client.call_tool("rs_search", {"query": "رسمیو", "kind": "everything"})
    assert {h["kind"] for h in r.structured_content["results"]} >= {"brand", "company"}


async def test_company_profile(api, client):
    api["/company/10100360794/direct/"] = page("company_ikco.html")
    r = await client.call_tool("rs_company", {"company_id": "۱۰۱۰۰۳۶۰۷۹۴", "max_rows": 3})
    c = r.structured_content
    assert c["company_id"] == "10100360794" and c["registration_no"] == "8352"
    assert c["capital_rial"] == 2563746068000000 and c["postal_code"] == "1389913657"
    board = c["board"]["rows"]
    assert board[0]["position"] == "مدیرعامل" and board[0]["person_id"] and board[0]["ad_id"]
    assert len(board) == 3 and c["board"]["total_shown"] == 6
    assert c["shareholders"]["rows"][0]["company_id"]
    assert c["brands"][0]["ip_id"] and c["recent_ads"][0].get("codal_id")
    r = await client.call_tool("rs_company", {"company_id": "10100360794", "sections": ["people"]})
    assert "board" in r.structured_content and "shareholders" not in r.structured_content


async def test_company_ads_are_masked(api, client):
    api["/company/14009396050/direct/"] = page("company_rasmio.html")
    r = await client.call_tool("rs_company", {"company_id": "14009396050", "sections": ["ads"]})
    ads = r.structured_content["recent_ads"]
    assert ads[0]["ad_id"] and ads[0]["published"]
    assert "شماره ملی 205059xxxx" in r.content[0].text


async def test_company_quota_wall(api, client):
    api["/company/14007650912/direct/"] = page("company_limit.html")
    r = await client.call_tool("rs_company", {"company_id": "14007650912"})
    assert r.is_error and "limit" in r.content[0].text


async def test_bad_company_id(api, client):
    r = await client.call_tool("rs_company", {"company_id": "123"})
    assert r.is_error and "national id" in r.content[0].text and not api.calls


async def test_ad(api, client):
    body = fixture("ad.json")
    body["value"]["description"] = "آقای الف به شماره ملی 1234567890 به سمت مدیرعامل"
    api["/api/v4/news/129676674"] = body
    r = await client.call_tool("rs_ad", {"ad_id": "129676674"})
    a = r.structured_content
    assert a["published"] == "1404/07/14" and a["newspaper_no"] == "23454"
    assert "123456xxxx" in a["text"] and "1234567890" not in a["text"]


async def test_person(api, client):
    api["/person/3706E5AB/x/"] = page("person.html")
    r = await client.call_tool("rs_person", {"person_id": "3706e5ab"})
    p = r.structured_content
    assert p["company_count"] == 7 and len(p["positions"]) == 7
    assert p["positions"][2]["company_id"] == "14009396050" and p["positions"][2]["current"] is True
    assert len(p["recent_ads"]) == 5
    text = r.content[0].text
    assert "@" not in text and "nationalId" not in text and "handle" not in text


async def test_product(api, client):
    api["/product/42218435/x/"] = page("product.html")
    r = await client.call_tool("rs_product", {"product_id": "42218435", "max_rows": 2})
    p = r.structured_content
    assert p["specs"]["HS"] == "12119099" and p["category_slug"] == "herbal-tea"
    assert p["supplier_company_id"] == "10380280920"
    assert len(p["similar_suppliers"]["rows"]) == 2 and p["similar_suppliers"]["rows"][0]["company_id"]


async def test_ip(api, client):
    api["/api/v4/Trademarks/812386"] = fixture("trademark.json")
    api["/api/v4/Trademarks/industrial-designs/35249"] = fixture("design.json")
    r = await client.call_tool("rs_ip", {"ip_id": "812386"})
    assert r.structured_content["owner_companies"][0]["company_id"] == "14009396050"
    assert r.structured_content["registered"] == "1399/10/03"
    r = await client.call_tool("rs_ip", {"ip_id": "35249", "kind": "industrial_design"})
    assert r.structured_content["title"] == "صندلي"


async def test_options(api, client):
    api["/api/search/Search/company-certificates"] = fixture("licenses.json")
    api["/api/search/Search/get-specific-category"] = fixture("categories.json")
    api["/api/search/Filter/get-filters"] = fixture("filters_companies.json")
    r = await client.call_tool("rs_options", {"what": "licenses", "query": "دانش بنیان"})
    assert r.structured_content["items"][0]["license_id"]
    r = await client.call_tool("rs_options", {"what": "categories", "category": "metals-chemicals"})
    assert any(i["slug"] == "metals" for i in r.structured_content["items"])
    r = await client.call_tool("rs_options", {"what": "company_types", "query": "سهامی خاص"})
    assert {"id": 5, "name": "سهامی خاص"} in r.structured_content["items"]


async def test_quota_429_message(api, client):
    msg = "تعداد درخواست شما بیش از حد مجاز است"
    api["/api/v4/news/1"] = lambda request: httpx.Response(429, json={"isSuccess": False, "message": msg})
    r = await client.call_tool("rs_ad", {"ad_id": "1"})
    assert r.is_error and msg in r.content[0].text and len(api.calls) == 1  # 429 is not retried
