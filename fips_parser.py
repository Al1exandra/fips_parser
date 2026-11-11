import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

DOC_URL = ("https://www1.fips.ru/registers-doc-view/fips_servlet"
           "?DB={db}&DocNumber={num}&TypeFile=html")
DB_INFO = {
    "RUPAT": {"type": "invention", "range": (2600000, 2699999)},
    "RUPM": {"type": "utility_model", "range": (230000, 239999)},
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
RATE_LIMIT_MARK = "Слишком быстрый просмотр"
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")

# загрузка
class RequestsFetcher:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)

    def get(self, url: str) -> str:
        r = self.s.get(url, timeout=60)
        r.raise_for_status()
        return r.content.decode("cp1251", errors="replace")

    def close(self):
        self.s.close()

class SeleniumFetcher:

    def __init__(self, headless: bool = True):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        opts = Options()
        if headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        self.driver = webdriver.Chrome(options=opts)

    def get(self, url: str) -> str:
        self.driver.get(url)
        return self.driver.page_source

    def close(self):
        self.driver.quit()


def fetch_document(fetcher, db: str, num: int, delay: float, retries: int = 6):
    #HTML документа или None, если документа нет. Ждёт при ограничении частоты.
    url = DOC_URL.format(db=db, num=num)
    for attempt in range(retries):
        try:
            html = fetcher.get(url)
        except Exception as e:  # сеть
            print(f"  [{num}] ошибка сети: {e}; повтор", file=sys.stderr)
            time.sleep(delay * (attempt + 2))
            continue
        if RATE_LIMIT_MARK in html:
            wait = delay * (attempt + 2)
            print(f"  [{num}] лимит частоты, жду {wait:.0f} c", file=sys.stderr)
            time.sleep(wait)
            continue
        if 'id="bib"' not in html and "id='bib'" not in html:
            return None  # документ отсутствует / аннулирован без полной карточки
        return html
    raise RuntimeError(f"не удалось получить документ {num}")


# разбор
def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def to_iso(text: str) -> str:
    m = DATE_RE.search(text or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def find_p(soup, label: str):
    return soup.find("p", string=lambda s: s and label in s) or next(
        (p for p in soup.find_all("p") if label in p.get_text()), None)


def b_text(p) -> str:
    b = p.find("b") if p else None
    return clean(b.get_text(" ")) if b else ""


def list_after_br(p) -> list:
#Авторы / патентообладатели: значения разделены <br> внутри <b>
    b = p.find("b") if p else None
    if not b:
        return []
    for br in b.find_all("br"):
        br.replace_with("\n")
    return [clean(x).rstrip(",;").strip() for x in b.get_text().split("\n") if clean(x)]


def between(soup, start_href: str, end_href: str):
 #теги между якорями-метками <a href="XxStart"> ... <a href="XxEnd">
    start = soup.find("a", href=start_href)
    end = soup.find("a", href=end_href)
    if not start:
        return []
    out = []
    for el in start.find_all_next():
        if el is end:
            break
        out.append(el)
    return out


def parse_patent(html: str, db: str, num: int) -> dict:
    soup = BeautifulSoup(html, "lxml")
    info = DB_INFO[db]

    # вид документа (C1, C2, U1 ...)
    kind = clean(soup.find(id="top6").get_text()) if soup.find(id="top6") else ""
    number_full = clean(soup.find(id="top4").get_text()) if soup.find(id="top4") else str(num)

    # статус
    status, last_change = "", ""
    st = soup.select_one("table.Status td[class^=StatusR]")
    if st:
        t = clean(st.get_text())
        status = clean(t.split("(")[0])
        last_change = to_iso(t)

    # МПК: ul.ipc > li (в шапке)
    ipc = []
    for li in soup.select("ul.ipc li"):
        span = li.find("span", class_="i")
        code = clean(span.get_text()) if span else ""
        ver = re.search(r"\((\d{4}\.\d{2})\)", li.get_text())
        if code:
            ipc.append({"code": code, "version": ver.group(1) if ver else ""})

    # СПК/CPC: <td class="spk"> <i><b>код</b> (версия)</i>
    cpc = []
    spk = soup.find("td", class_="spk")
    if spk:
        for i in spk.find_all("i"):
            b = i.find("b")
            if b:
                ver = re.search(r"\((\d{4}\.\d{2})\)", i.get_text())
                cpc.append({"code": clean(b.get_text()), "version": ver.group(1) if ver else ""})

    # заявка, даты
    app_number = app_date = ""
    p_app = find_p(soup, "Заявка")
    if p_app:
        a = p_app.find("a")
        btxt = b_text(p_app)  # "2017108123, 13.03.2017" (ссылки у ПМ может не быть)
        app_number = clean(a.get_text()) if a else clean(btxt.split(",")[0])
        app_date = to_iso(btxt)
    reg_date = to_iso(b_text(find_p(soup, "Дата регистрации")))
    if not app_date:
        app_date = to_iso(b_text(find_p(soup, "Дата подачи заявки")))

    pub_date, bulletin = "", ""
    p_pub = find_p(soup, "Опубликовано")
    if p_pub:
        bs = p_pub.find_all("b")
        if bs:
            pub_date = to_iso(bs[0].get_text())
        if len(bs) > 1:
            bulletin = clean(bs[1].get_text())

    address = b_text(find_p(soup, "Адрес для переписки"))
    search_report = b_text(soup.find("p", class_="B560"))

    authors = list_after_br(find_p(soup, "Автор"))
    owners = list_after_br(find_p(soup, "Патентообладатель"))
    authors_struct = []
    for a in authors:
        m = re.match(r"(.*?)\s*\(([A-Z]{2})\)\s*$", a)
        authors_struct.append({"name": m.group(1) if m else a,
                               "country": m.group(2) if m else ""})

    title_p = soup.find(id="B542")
    title = clean(title_p.find("b").get_text()) if title_p and title_p.find("b") else ""
    abs_div = soup.find(id="Abs")
    abstract = ""
    if abs_div:
        abstract = clean(" ".join(p.get_text(" ") for p in abs_div.find_all("p")
                                  if "TitAbs" not in (p.get("class") or [])))

    # описание — абзацы <p num="N"> между DeStart и DeEnd
    sections = []
    for el in between(soup, "DeStart", "DeEnd"):
        if el.name == "p":
            sections.append({"num": el.get("num", ""), "title": "", "text": clean(el.get_text(" "))})

    # формула — абзацы между ClStart и ClEnd (без заголовка)
    claims = clean(" ".join(
        el.get_text(" ") for el in between(soup, "ClStart", "ClEnd")
        if el.name == "p" and "TitCla" not in (el.get("class") or [])))

    prefix = "RU" + str(num)
    return {
        "patent_id": f"{prefix}{kind}",
        "patent_type": info["type"],
        "patent_number_full": f"RU {number_full} {kind}".strip(),
        "title": title,
        "status_info": {"status": status, "last_change": last_change},
        "application_details": {
            "application_number": app_number,
            "application_date": app_date,
            "registration_date": reg_date,
            "publication_date": pub_date,
            "publication_bulletin_number": bulletin,
        },
        "ipc": ipc,
        "cpc": cpc,
        "inventors": authors,
        "authors": authors_struct,
        "owners": owners,
        "correspondence_address": address,
        "abstract": abstract,
        "claims": claims,
        "description_sections": sections,
        "search_report": search_report,
        "url": DOC_URL.format(db=db, num=num),
    }


# main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--db", choices=DB_INFO, required=True, help="RUPAT - изобретения, RUPM - полезные модели")
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--end", type=int, required=True)
    ap.add_argument("--delay", type=float, default=3.5,
                    help="пауза между документами, c (сайт банит чаще 1 раза в 3 c)")
    ap.add_argument("--out", help="выходной файл (по умолчанию <start>-<end>.json)")
    ap.add_argument("--selenium", action="store_true", help="грузить страницы через Chrome/Selenium")
    ap.add_argument("--no-headless", action="store_true")
    ap.add_argument("--csv", action="store_true", help="дополнительно сохранить плоскую таблицу .csv (pandas)")
    args = ap.parse_args()

    lo, hi = DB_INFO[args.db]["range"]
    if not (lo <= args.start <= args.end <= hi):
        print(f"Внимание: диапазон вне варианта 3 ({lo}-{hi})", file=sys.stderr)

    out = Path(args.out or f"{args.start}-{args.end}.json")
    cache = out.with_suffix(".jsonl")  # докачка: уже обработанные номера
    done = {}
    if cache.exists():
        for line in cache.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                done[rec["num"]] = rec["patent"]
        print(f"Из кэша: {len(done)} документов")

    fetcher = SeleniumFetcher(not args.no_headless) if args.selenium else RequestsFetcher()
    total = args.end - args.start + 1
    try:
        with cache.open("a", encoding="utf-8") as cf:
            for i, num in enumerate(range(args.start, args.end + 1), 1):
                if num in done:
                    continue
                html = fetch_document(fetcher, args.db, num, args.delay)
                patent = parse_patent(html, args.db, num) if html else None
                done[num] = patent
                cf.write(json.dumps({"num": num, "patent": patent}, ensure_ascii=False) + "\n")
                cf.flush()
                print(f"[{i}/{total}] {num}: {patent['title'][:60] if patent else '— нет документа'}")
                time.sleep(args.delay)
    except KeyboardInterrupt:
        print("Прервано, сохраняю собранное (повторный запуск продолжит с места остановки)")
    finally:
        fetcher.close()

    patents = [done[n] for n in sorted(done) if done[n]]
    out.write_text(json.dumps(patents, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Сохранено {len(patents)} патентов -> {out}")

    if args.csv:
        import pandas as pd
        df = pd.json_normalize(patents)
        for c in ("ipc", "cpc", "authors", "description_sections"):
            if c in df:
                df[c] = df[c].apply(lambda v: json.dumps(v, ensure_ascii=False))
        for c in ("inventors", "owners"):
            df[c] = df[c].apply(lambda v: "; ".join(v))
        df.to_csv(out.with_suffix(".csv"), index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
