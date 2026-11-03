"""Helpers shared by the tool modules: digits, dates, privacy masking and reading the site's HTML pages."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote

from bs4 import BeautifulSoup, Tag

from .http import SITE, ApiError

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫٬", "01234567890123456789.,")
# A person's 10-digit national code after "شماره ملی"/"کد ملی"; the site itself shows only the first 6 digits.
_NATIONAL_CODE = re.compile(r"((?:شماره|کد)\s*ملی\s*:?\s*)(\d{6})\d{4}(?!\d)")
_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
# Shown instead of a company profile once the anonymous per-IP quota is used up.
_LIMIT_MARKERS = ("سقف مجاز", "امکان مشاهده این اطلاعات برای شما وجود ندارد")


def ascii_digits(text: str) -> str:
    """Persian/Arabic digits and separators to ASCII ('۱۴۰۴/۰۶/۲۶' -> '1404/06/26', '۶۰٬۰۰۰' -> '60,000')."""
    return text.translate(_DIGITS)


def clean(text: str | None) -> str:
    """ASCII digits, single spaces, person national codes masked like the site does (0923198891 -> 092319xxxx)."""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", ascii_digits(text)).strip()
    return _NATIONAL_CODE.sub(r"\g<1>\g<2>xxxx", text)


def number(text: str | None) -> int | None:
    """'۶۰٬۰۰۰٬۰۰۰ ریال' -> 60000000; None when there are no digits."""
    digits = re.sub(r"[^\d]", "", ascii_digits(text or ""))
    return int(digits) if digits else None


def g2j(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Gregorian date to Persian (Jalali) date."""
    gdm = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + gdm[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def j2g(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    """Persian (Jalali) date to Gregorian."""
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4 + jd
    days += (jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0
    months = (0, 31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    gm = 0
    while gm < 13 and gd > months[gm]:
        gd -= months[gm]
        gm += 1
    return gy, gm, gd


def jalali(iso: str | None) -> str | None:
    """'2025-10-06T00:00:00' -> '1404/07/14'; None stays None."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    if not m:
        return None
    y, mo, d = g2j(*(int(x) for x in m.groups()))
    return f"{y}/{mo:02d}/{d:02d}"


def to_iso(day: str | None) -> str | None:
    """A user date, Persian ('1402/01/15', '۱۴۰۲-۰۱-۱۵') or Gregorian ('2023-04-04'), to the site's ISO UTC midnight."""
    if not day:
        return None
    m = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", ascii_digits(day.strip()))
    if not m:
        raise ApiError(f"Date {day!r} is not YYYY/MM/DD (Persian like 1402/01/15, or Gregorian like 2023-04-04).")
    y, mo, d = (int(x) for x in m.groups())
    if y < 1700:
        y, mo, d = j2g(y, mo, d)
    return f"{y:04d}-{mo:02d}-{d:02d}T00:00:00.000Z"


def soup(html: str) -> BeautifulSoup:
    """Parse a page and stop with a clear message when the site shows its anonymous-quota wall instead."""
    page = BeautifulSoup(html, "html.parser")
    main = page.find(id="main-content")
    text = main.get_text(" ", strip=True) if main else ""
    if any(m in text for m in _LIMIT_MARKERS):
        quota = re.search(r"سقف مجاز[^.]*\.", text)
        raise ApiError(
            "Rasmio shows this page to anonymous visitors only a few times per IP address and the limit is used up"
            f"{' (' + clean(quota.group(0)) + ')' if quota else ''}. Search results still work; try the profile again "
            "after the waiting time, or from another network."
        )
    return page


def flight(html: str) -> str:
    """The React Server Components payload a Next.js page streams in `self.__next_f.push` calls."""
    return "".join(json.loads(m.group(1)) for m in _PUSH.finditer(html))


def query_data(html: str, key: str) -> Any:
    """The data of the dehydrated React Query whose key starts with `key` (e.g. 'person'), or None."""
    found: list[Any] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            queries = x.get("queries")
            for q in queries if isinstance(queries, list) else []:
                if isinstance(q, dict) and (q.get("queryKey") or [None])[0] == key:
                    found.append((q.get("state") or {}).get("data"))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    for line in flight(html).split("\n"):
        if '"queryKey"' in line and ":" in line:
            try:
                walk(json.loads(line.split(":", 1)[1]))
            except ValueError:
                continue
    return found[0] if found else None


def link_ids(el: Tag) -> dict[str, str]:
    """Ids from the links inside an element: person (hex), company (national id), ad, codal letter, brand."""
    out: dict[str, str] = {}
    for a in [el, *el.find_all("a", href=True)] if el.name == "a" else el.find_all("a", href=True):
        href = unquote(a["href"]).replace(SITE, "")
        for name, pattern in (
            ("person_id", r"^/person/([0-9A-Fa-f]+)/"),
            ("company_id", r"^/company/(\d+)/?(?:[^/]*/?)?$"),
            ("ad_id", r"^/News/(\d+)"),
            ("codal_id", r"^/codal/(\d+)"),
            ("brand_id", r"^/brand/(\d+)"),
            ("certificate_id", r"certificates=(\d+)"),
        ):
            if name not in out and (m := re.search(pattern, href)):
                out[name] = m.group(1)
    return out


# Column headers of the profile tables -> output keys; an unknown header is kept as written.
HEADS = {
    "نام": "name",
    "سمت": "position",
    "تاریخ شروع": "start",
    "تاریخ پایان": "end",
    "درجه": "kind",
    "سال مالی": "fiscal_year",
    "نام سهام‌دار": "shareholder",
    "تعداد سهم": "shares",
    "درصد سهم": "percent",
    "ارزش سهام (ریال)": "value_rial",
    "نام شرکت و سهم": "holding",
    "مجوز": "license",
    "مرجع صادر کننده": "issuer",
    "تاریخ شروع مجوز": "start",
    "مبلغ سرمایه (ریال)": "capital_rial",
    "نوع": "type",
    "محل تغییر سرمایه": "source",
    "تاریخ ثبت": "registered",
    "عنوان قرارداد": "title",
    "کارفرما": "employer",
    "مبلغ (ریال)": "amount_rial",
    "سال مالی (منتهی به)": "fiscal_year_end",
    "وضعیت حسابرسی": "audit",
    "دوره": "period",
    "تاریخ انتشار": "published",
    "عنوان اختراع یا طرح": "title",
    "شرکت": "company",
    "محصول منطبق": "matching_product",
    "سن شرکت": "age",
    "سرمایه ثبتی": "capital",
}


def table(el: Tag | None, max_rows: int) -> dict[str, Any] | None:
    """Rows of the first <table> in a section as {column: cell text} plus ids from the row's links."""
    t = el if el is not None and el.name == "table" else el.find("table") if el else None
    if t is None:
        return None
    heads = [clean(th.get_text(" ", strip=True)) for th in t.find_all("th")]
    heads = [HEADS.get(h, h) for h in heads]
    rows = []
    for tr in t.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        row: dict[str, Any] = {}
        for head, td in zip(heads, cells, strict=False):
            text = clean(td.get_text(" ", strip=True))
            if head and text and text != "-":
                row[head] = text
        row.update(link_ids(tr))
        rows.append(row)
    return {"total_shown": len(rows), "rows": rows[:max_rows]}
