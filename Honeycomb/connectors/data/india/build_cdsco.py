"""Rebuild the bundled India drug datasets the medicines connector searches.

There is no public API for Indian drug regulation data. CDSCO and NPPA publish
PDFs, so this script downloads them and extracts the tables into the three JSON
files beside it. It is a maintenance tool, run by hand when CDSCO adds a year's
list -- never at runtime, and its one extra dependency is not in
requirements.txt:

    python -m venv /tmp/pdfenv && /tmp/pdfenv/bin/pip install pdfplumber httpx
    /tmp/pdfenv/bin/python Honeycomb/connectors/data/india/build_cdsco.py

nppa.gov.in serves an incomplete TLS certificate chain, so its two public PDFs
are fetched with verification off; nothing secret is sent and the files are
only parsed.

Known gaps, stated in the data files too: the 2006 CDSCO list is a scanned
image with no text, and the 2001-2003 lists are flowing text, so their drug
name and class arrive as one field.
"""
import html
import json
import re
from pathlib import Path
from urllib.parse import quote

import httpx
import pdfplumber

HERE = Path(__file__).resolve().parent
WORK = Path('/tmp/honeycomb-india-build')
CDSCO = 'https://cdsco.gov.in'
LIST_PAGE = CDSCO + '/opencms/opencms/en/Approval_new/Approved-New-Drugs/'
NPPA_CEILING = ('https://nppa.gov.in/storage/uploads/pdf/'
                'Ceiling-Price-List-Fpdf-2a92c4b9585909e865b5ae90f099f58c.pdf')
NLEM = 'https://nppa.gov.in/storage/uploads/pdf/nlem-2022pdf-0cd1d2b28855bf30128875ab19fc5304.pdf'
UA = {'User-Agent': 'Mozilla/5.0 (compatible; Honeycomb-data-build)'}
MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']


def clean(s):
    return re.sub(r'\s+', ' ', (s or '').replace('', '•')).strip()


def fetch(url, path, verify=True):
    with httpx.Client(headers=UA, follow_redirects=True, timeout=120, verify=verify) as c:
        path.write_bytes(c.get(url).content)
    return path


def iso(raw):
    s = raw.strip().lower()
    m = re.match(r'^(\d{1,2})[\s.\-/]+(\d{1,2})[\s.\-/]+(\d{2,4})', s)
    if m:
        d, mo, y = map(int, m.groups())
        y = y + 2000 if y < 100 else y
        if 1 <= mo <= 12 and 1 <= d <= 31 and 1950 < y < 2100:
            return '{0:04d}-{1:02d}-{2:02d}'.format(y, mo, d)
    m = re.match(r'^([a-z]+)[\s.\-,]+(\d{4})', s)
    if m and m.group(1)[:3] in MONTHS:
        return '{0}-{1:02d}'.format(m.group(2), MONTHS.index(m.group(1)[:3]) + 1)
    m = re.search(r'(19|20)\d{2}', s)
    return m.group(0) if m else ''


def colmap(header):
    m = {}
    for i, c in enumerate(clean(x).lower() for x in header):
        if 'name' in c or c in ('drug', 'drugs', 'product'):
            m.setdefault('drug', i)
        elif 'strength' in c:
            m.setdefault('strength', i)
        elif 'indication' in c or 'pharmacological' in c or 'action' in c:
            m.setdefault('indication', i)
        elif 'date' in c or 'approval' in c:
            m.setdefault('date', i)
    return m if 'drug' in m and ('indication' in m or 'date' in m) else None


def cdsco_lists():
    page = httpx.get(LIST_PAGE, headers=UA, timeout=60).text
    rows = re.findall(r"<td>(\d+)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td><a href='([^']+)'",
                      page, re.S)
    for n, title, _date, link in rows:
        title = clean(html.unescape(title))
        if 'Reference Pro' in title:  # a reference-product list, not approvals
            continue
        # The link answers with an <iframe> pointing at the real PDF.
        frame = httpx.get(CDSCO + link, headers=UA, timeout=60).text
        src = re.search(r"src='([^']+)'", frame)
        if src:
            yield title, CDSCO + quote(src.group(1))


def parse_cdsco(path, title, url):
    out = []
    line_re = re.compile(r'^(\d{1,4})\s+(.+?)\s+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})\s*$')
    with pdfplumber.open(path) as pdf:
        cols = None
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    found = colmap(row)
                    if found:
                        cols = found
                        continue
                    if not cols:
                        continue

                    def get(key):
                        i = cols.get(key)
                        return clean(row[i]) if i is not None and i < len(row) else ''
                    drug, indication, date = get('drug'), get('indication'), get('date')
                    if not drug and out and (indication or date):
                        out[-1]['indication'] = clean(out[-1]['indication'] + ' ' + indication)
                        out[-1]['approved'] = out[-1]['approved'] or date
                        continue
                    if drug and drug.lower() not in ('approval', 'name of drug'):
                        out.append({'drug': drug, 'strength': get('strength'),
                                    'indication': indication, 'approved': date})
        if not out:
            for page in pdf.pages:
                for line in (page.extract_text() or '').splitlines():
                    m = line_re.match(line.strip())
                    if m:
                        out.append({'drug': clean(m.group(2)), 'strength': '', 'indication': '',
                                    'approved': m.group(3)})
    for r in out:
        r['date'] = iso(r['approved'])
        r['list'] = title
    return out


def build_cdsco():
    lists, rows, seen = {}, [], set()
    for i, (title, url) in enumerate(cdsco_lists()):
        lists[title] = url
        for r in parse_cdsco(fetch(url, WORK / 'cdsco_{0:02d}.pdf'.format(i)), title, url):
            key = (r['drug'].lower(), r['approved'], r['indication'][:60].lower())
            if key not in seen:
                seen.add(key)
                rows.append(r)
    return {'source': 'CDSCO, Approved New Drugs (cdsco.gov.in)',
            'note': ('Extracted from CDSCO PDF lists 1961-2026; the 2006 list is a scanned image and is '
                     'missing. Where a PDF row breaks across pages an indication can be cut short -- '
                     'quote from source_pdf.'),
            'lists': lists, 'rows': rows}


def build_ceiling():
    rows, last = [], None
    with pdfplumber.open(fetch(NPPA_CEILING, WORK / 'ceiling.pdf', verify=False)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for r in table:
                    r = [clean(c) for c in r]
                    if len(r) < 5 or r[0].lower().startswith('sl') or r[0] == '(1)':
                        continue
                    _sl, med, form, unit, price = r[:5]
                    if not med:
                        if last and form:
                            last['dosage_form'] = clean(last['dosage_form'] + ' ' + form)
                        continue
                    try:
                        value = float(price.replace(',', ''))
                    except ValueError:
                        value = None
                    last = {'medicine': med, 'dosage_form': form, 'unit': unit,
                            'ceiling_price_inr': value}
                    rows.append(last)
    return {'source': 'NPPA, Ceiling prices of scheduled formulations under DPCO 2013 (NLEM 2015)',
            'as_of': '2020-09-30', 'url': NPPA_CEILING, 'rows': rows}


def build_nlem():
    rows, heads = [], {}
    with pdfplumber.open(fetch(NLEM, WORK / 'nlem.pdf', verify=False)) as pdf:
        pages = [p for p in pdf.pages
                 if 'Level of' in (p.extract_text() or '') or 'Dosage form' in (p.extract_text() or '')]
        for page in pages:
            for line in (page.extract_text() or '').splitlines():
                m = re.match(r'^(?:Section\s+)?(\d{1,2}(?:\.\d{1,2}){0,2})\s*[-–:]\s*([A-Za-z].+)$',
                             line.strip())
                if m and m.group(1) not in heads:
                    heads[m.group(1)] = clean(m.group(2))
            for table in page.extract_tables():
                last = None
                for r in table:
                    r = [clean(c) for c in r]
                    if len(r) >= 4 and re.match(r'^\d+(\.\d+){2,}$', r[0] or ''):
                        last = {'code': r[0], 'medicine': re.sub(r'[\s*#]+$', '', r[1]),
                                'level': r[2], 'dosage_forms': r[3]}
                        rows.append(last)
                    elif last is not None and len(r) >= 4 and not r[0] and not r[1] and r[3]:
                        last['dosage_forms'] = clean(last['dosage_forms'] + '; ' + r[3])
    for r in rows:
        parts = r['code'].split('.')
        r['section'] = next(('{0} {1}'.format(k, heads[k]) for k in
                             ('.'.join(parts[:n]) for n in (len(parts) - 1, 2, 1)) if k in heads), '')
    return {'source': 'National List of Essential Medicines 2022 (Gazette of India), via NPPA',
            'url': NLEM, 'rows': rows}


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    for name, build in (('cdsco_new_drugs.json', build_cdsco),
                        ('nppa_ceiling_prices.json', build_ceiling),
                        ('nlem_2022.json', build_nlem)):
        data = build()
        (HERE / name).write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
        print(name, len(data['rows']), 'rows')


if __name__ == '__main__':
    main()
