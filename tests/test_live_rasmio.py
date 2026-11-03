"""Calls the real rasmio.com. Run with: uv run python -m pytest -m live -q

Gentle on purpose: anonymous visitors may open only about 5 company profiles per IP every 4 days, so only one
test opens a profile (and it accepts the quota message as a pass).
"""

import asyncio

import pytest

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.fixture(autouse=True)
async def spacing():
    yield
    await asyncio.sleep(2)


async def test_search_companies(client):
    r = await client.call_tool(
        "rs_search_companies", {"query": "نرم افزار", "province": "اصفهان", "status": "active", "page_size": 5}
    )
    d = r.structured_content
    assert d["total"] > 10 and len(d["results"]) == 5
    assert all(h["company_id"].isdigit() for h in d["results"])


async def test_search_people_and_person(client):
    r = await client.call_tool("rs_search", {"query": "ملک نژاد یزدی", "kind": "people", "page_size": 5})
    people = r.structured_content["results"]
    assert people and people[0]["person_id"]
    r = await client.call_tool("rs_person", {"person_id": "3706E5AB"})
    p = r.structured_content
    assert p["company_count"] >= 1 and any(x["company_id"] == "14009396050" for x in p["positions"])


async def test_products_and_product(client):
    r = await client.call_tool("rs_search", {"query": "دمنوش گل محمدی", "kind": "products", "page_size": 3})
    hit = r.structured_content["results"][0]
    r = await client.call_tool("rs_product", {"product_id": hit["product_id"]})
    p = r.structured_content
    assert p["title"] and p["specs"] and p["category_path"]


async def test_ad_and_ip(client):
    r = await client.call_tool("rs_ad", {"ad_id": "129676674"})
    assert "14009396050" in r.structured_content["title"]
    r = await client.call_tool("rs_ip", {"ip_id": "812386"})
    assert r.structured_content["registration_no"] == "395756"
    r = await client.call_tool("rs_ip", {"ip_id": "45546", "kind": "patent"})
    assert r.structured_content["title"]


async def test_options(client):
    r = await client.call_tool("rs_options", {"what": "licenses", "query": "دانش بنیان"})
    assert r.structured_content["items"]
    r = await client.call_tool("rs_options", {"what": "categories"})
    assert any(i["slug"] == "metals-chemicals" for i in r.structured_content["items"])


async def test_company_profile(client):
    r = await client.call_tool("rs_company", {"company_id": "14009396050"})
    if r.is_error:
        assert "limit" in r.content[0].text  # the anonymous quota for this IP is used up
        pytest.skip("anonymous profile quota used up on this IP")
    c = r.structured_content
    assert c["company_id"] == "14009396050" and c["board"]["rows"]
