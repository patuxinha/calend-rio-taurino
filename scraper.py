#!/usr/bin/env python3
"""
Calendário Taurino — Scraper Automático v2
Corre diariamente via GitHub Actions às 07h00.
- Adiciona eventos novos
- Corrige canais TV errados
- NUNCA toca no CSS, HTML, vídeo de intro ou design
"""

import os, re, json, time, datetime, sys
import requests
from bs4 import BeautifulSoup
import anthropic

API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
HTML_FILE = "index.html"
TODAY     = datetime.date.today()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "pt-PT,pt;q=0.9,es;q=0.8",
}

# ── Sites a consultar ──────────────────────────────────────────────────────────
SITES_AGENDA = [
    {"nome": "touradas.pt",        "url": "https://www.touradas.pt/agenda",                          "pais": "pt"},
    {"nome": "portadossustos.com", "url": "https://www.portadossustos.com/",                          "pais": "pt"},
    {"nome": "touroeouro.com",     "url": "https://touroeouro.com/",                                  "pais": "pt"},
    {"nome": "mundotoro.com",      "url": "https://www.mundotoro.com/agenda-taurina",                  "pais": "es"},
    {"nome": "cultoro.es",         "url": "https://cultoro.es/agenda-taurina",                        "pais": "es"},
    {"nome": "ladivisa.es",        "url": "https://www.ladivisa.es/",                                 "pais": "es"},
    {"nome": "tauromaquia.com.pt", "url": "https://www.tauromaquia.com.pt/",                          "pais": "pt"},
]

# ── Site TV — corrige canais errados E adiciona novos ─────────────────────────
SITES_TV = [
    {"nome": "elmuletazo.com",  "url": "https://elmuletazo.com/agenda-de-toros-en-television/"},
    {"nome": "cultoro.es TV",   "url": "https://cultoro.es/toros-en-television-agenda"},
    {"nome": "vadetoros.es TV", "url": "https://vadetoros.es/tv/"},
]

def fetch(url, timeout=20):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  ⚠ Erro ao aceder {url}: {e}")
        return ""

def clean_text(html, max_chars=14000):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script","style","nav","footer","header","aside","form","button"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text[:max_chars]

def ask_claude(client, prompt, max_tokens=2500):
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"  ⚠ Erro Claude API: {e}")
        return ""

# ── Extrai eventos existentes ──────────────────────────────────────────────────
def get_existing_keys(html):
    """Devolve set de chaves 'YYYY-MM-DD|loc30' dos eventos existentes."""
    dts  = re.findall(r"dt:'(\d{4}-\d{2}-\d{2})'", html)
    locs = re.findall(r"loc:'([^']+)'", html)
    keys = set()
    for i, dt in enumerate(dts):
        loc = locs[i][:30] if i < len(locs) else ""
        keys.add(f"{dt}|{loc}")
    return keys

def get_tv_keys(html):
    """Devolve set de chaves 'YYYY-MM-DD|chan|loc20' da agenda TV existente."""
    pattern = r"dt:'(\d{4}-\d{2}-\d{2})',dia:'[^']*',mes:'[^']*',chan:'([^']*)',hora:'[^']*',loc:'([^']*)'"
    matches = re.findall(pattern, html)
    return {f"{dt}|{chan}|{loc[:20]}" for dt, chan, loc in matches}

# ── Scraper de agenda ──────────────────────────────────────────────────────────
def scrape_agenda(client, html_content):
    """Scrapes agenda sites, returns new FES entries to insert."""
    existing = get_existing_keys(html_content)
    new_entries = []

    for site in SITES_AGENDA:
        print(f"\n🌐 Agenda: {site['nome']}...")
        raw = fetch(site["url"])
        if not raw or len(raw) < 200:
            continue

        text = clean_text(raw)
        print(f"  → {len(text)} chars")

        prompt = f"""Analisa este texto do site taurino "{site['nome']}" (país: {site['pais']}).
Hoje é {TODAY}. Extrai APENAS eventos futuros (data >= {TODAY}).

Para cada evento, devolve UM objecto JS por linha, exactamente neste formato:
{{dt:'YYYY-MM-DD',dtE:'YYYY-MM-DD',dia:'D',mes:'Mmm',p:'{site['pais']}',flag:'{"🇵🇹" if site["pais"]=="pt" else "🇪🇸"}',pN:'{"Portugal" if site["pais"]=="pt" else "Espanha"}',nom:'Nome',loc:'Praça, Cidade',mod:'rejones',top:0,feria:0,tv:0,lat:0,lon:0,bi:'url',c:{{dh:'D Mmm YYYY',t:'Ganadaria',to:[{{n:'Nome',nat:'🇵🇹',r:'CAVALEIRO'}}],p:'Praça',cap:'A confirmar'}},no:'nota',fi:null}}

Regras:
- mes: Jan Feb Mar Abr Mai Jun Jul Ago Set Out Nov Dez
- mod: rejones / corrida / misto
- tv:1 se transmitido em TV
- top:1 se evento de grande destaque
- feria:1 se parte de uma feria/festa popular
- flag/pN conforme país
- SÓ objectos JS, sem texto adicional, sem ```

TEXTO:
{text}"""

        resp = ask_claude(client, prompt)
        if not resp:
            continue

        # Parse individual objects
        for obj in re.split(r'\},\s*\{', resp):
            obj = obj.strip().lstrip(',').strip()
            if not obj.startswith('{'):
                obj = '{' + obj
            if not obj.endswith('}'):
                obj = obj + '}'

            m_dt  = re.search(r"dt:'(\d{4}-\d{2}-\d{2})'", obj)
            m_loc = re.search(r"loc:'([^']+)'", obj)
            if not m_dt or not m_loc:
                continue

            dt  = m_dt.group(1)
            loc = m_loc.group(1)[:30]
            key = f"{dt}|{loc}"

            try:
                if datetime.date.fromisoformat(dt) < TODAY:
                    continue
            except:
                continue

            if key in existing:
                continue

            new_entries.append(obj)
            existing.add(key)
            print(f"  ✓ Novo: {dt} | {loc[:50]}")

        time.sleep(2)

    return new_entries

# ── Scraper TV — corrige E adiciona ───────────────────────────────────────────
def scrape_tv(client, html_content):
    """
    Scrapes TV agenda sites.
    Returns (corrections, new_entries).
    corrections: list of (old_chan, old_loc, old_dt, new_chan, new_loc, new_cartel)
    new_entries: list of TV JS objects to insert
    """
    existing_tv = get_tv_keys(html_content)
    corrections  = []
    new_tv       = []

    # Get all TV entries from the HTML to check for errors
    tv_pattern = r"\{dt:'(\d{4}-\d{2}-\d{2})',dia:'[^']*',mes:'[^']*',chan:'([^']*)',hora:'([^']*)',loc:'([^']*)',nom:'([^']*)',cartel:'([^']*)'"
    existing_tv_list = re.findall(tv_pattern, html_content)

    combined_text = ""
    for site in SITES_TV:
        print(f"\n📺 TV: {site['nome']}...")
        raw = fetch(site["url"])
        if not raw or len(raw) < 200:
            continue
        combined_text += f"\n\n--- {site['nome']} ---\n" + clean_text(raw, max_chars=8000)
        time.sleep(1)

    if not combined_text:
        return [], []

    # Ask Claude to extract ALL TV events from today onwards
    prompt = f"""Analisa este texto de sites de agenda de toros em televisão.
Hoje é {TODAY}. Extrai TODOS os eventos televisados a partir de hoje.

Para cada evento devolve uma linha com este formato EXACTO (separado por |):
DATA|HORA|CANAL|LOCAL|NOME|CARTEL

Exemplo:
2026-06-13|19:00|Canal Sur|Marbella, Málaga|Feria San Bernabé|El Freixo p/ Morante, Talavante, Miranda

Regras:
- DATA no formato YYYY-MM-DD
- CANAL: nome exacto do canal (Telemadrid, Canal Sur, CMM, Aragón TV, À Punt, Canal Extremadura, OneToro, etc.)
- Inclui TODOS os eventos que encontrares
- Uma linha por evento
- Sem cabeçalhos, sem texto extra

TEXTO DOS SITES:
{combined_text[:20000]}"""

    resp = ask_claude(client, prompt, max_tokens=3000)
    if not resp:
        return [], []

    print(f"\n  → Claude identificou {len(resp.splitlines())} eventos TV")

    for line in resp.splitlines():
        parts = line.strip().split('|')
        if len(parts) < 6:
            continue

        dt, hora, canal, local, nome, cartel = parts[0], parts[1], parts[2], parts[3], parts[4], '|'.join(parts[5:])

        try:
            if datetime.date.fromisoformat(dt) < TODAY:
                continue
        except:
            continue

        # Check if this event already exists with WRONG canal
        for ex_dt, ex_chan, ex_hora, ex_loc, ex_nom, ex_cartel in existing_tv_list:
            if ex_dt == dt and ex_loc[:20] == local[:20] and ex_chan != canal:
                # Same date, same location, different channel → correction needed
                corrections.append({
                    'dt': dt, 'old_chan': ex_chan, 'new_chan': canal,
                    'loc': local, 'nome': nome, 'cartel': cartel
                })
                print(f"  ✏ Correcção: {dt} {local[:30]} | {ex_chan} → {canal}")

        # Check if new (not in existing TV keys)
        key = f"{dt}|{canal}|{local[:20]}"
        if key not in existing_tv:
            # Build TV entry object
            mes_map = {1:'Jan',2:'Feb',3:'Mar',4:'Abr',5:'Mai',6:'Jun',7:'Jul',8:'Ago',9:'Set',10:'Out',11:'Nov',12:'Dez'}
            try:
                d = datetime.date.fromisoformat(dt)
                mes = mes_map[d.month]
                dia = str(d.day)
            except:
                continue

            # Detect country from canal
            pt_canals = ['RTP','RTP1','RTP2','SIC','TVI','Canal 11','CMTV']
            pais = '🇵🇹' if any(p in canal for p in pt_canals) else '🇪🇸'

            tv_obj = f"  {{dt:'{dt}',dia:'{dia}',mes:'{mes}',chan:'{canal}',hora:'{hora}h {pais}',loc:'{local}',nom:'{nome}',cartel:'{cartel}',link:'https://www.elmuletazo.com/agenda-de-toros-en-television/'}}"
            new_tv.append(tv_obj)
            existing_tv.add(key)
            print(f"  ✓ Novo TV: {dt} {canal} | {local[:30]}")

    return corrections, new_tv

# ── Aplica correcções TV ───────────────────────────────────────────────────────
def apply_tv_corrections(html, corrections):
    for corr in corrections:
        old = f"chan:'{corr['old_chan']}',hora:"
        # Find the specific entry by dt + old_chan + loc
        pattern = rf"(dt:'{re.escape(corr['dt'])}',dia:'[^']*',mes:'[^']*',)chan:'{re.escape(corr['old_chan'])}'(,hora:'[^']*',loc:'{re.escape(corr['loc'][:20])}"
        new_pattern = rf"\1chan:'{corr['new_chan']}'\2"
        html_new = re.sub(pattern, new_pattern, html)
        if html_new != html:
            print(f"  ✅ Corrigido: {corr['dt']} {corr['old_chan']} → {corr['new_chan']}")
            html = html_new
    return html

# ── Insere entradas no FES ─────────────────────────────────────────────────────
def insert_fes_entries(html, entries):
    if not entries:
        return html, 0
    marker = '];\n\nconst RUA=['
    block = f"\n\n  /* AUTO {TODAY} */\n" + "\n".join(f"  ,{e}" for e in entries)
    idx = html.find(marker)
    if idx < 0:
        print("  ⚠ Marcador FES não encontrado!")
        return html, 0
    return html[:idx] + block + html[idx:], len(entries)

# ── Insere entradas no TV_AGENDA ───────────────────────────────────────────────
def insert_tv_entries(html, tv_entries):
    if not tv_entries:
        return html, 0
    # Find closing ]; of TV_AGENDA array
    tv_end_pattern = r'(\];\s*\n\s*const MES_TV)'
    match = re.search(tv_end_pattern, html)
    if not match:
        print("  ⚠ Marcador TV_AGENDA não encontrado!")
        return html, 0
    pos = match.start()
    block = "\n" + "\n".join(tv_entries) + "\n"
    return html[:pos] + block + html[pos:], len(tv_entries)

# ── Valida sintaxe JS ──────────────────────────────────────────────────────────
def validate_js(html):
    """Verifica se o array FES tem chaves balanceadas."""
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
    for s in scripts:
        if 'const FES=' in s:
            opens  = s.count('{')
            closes = s.count('}')
            if opens != closes:
                print(f"  ❌ ERRO SINTAXE: {{ = {opens}, }} = {closes}, diff = {opens-closes}")
                return False
            print(f"  ✅ Sintaxe JS OK: {{ = {opens}, }} = {closes}")
            return True
    return True

# ── Actualiza versão ───────────────────────────────────────────────────────────
def update_version(html):
    hoje = TODAY.strftime("%Y%m%d")
    return re.sub(
        r'<!DOCTYPE html><!-- CTB-v\d{8}[^>]* -->',
        f'<!DOCTYPE html><!-- CTB-v{hoje}-AUTO -->',
        html
    )

# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    print(f"\n🐂 Calendário Taurino — Scraper v2 — {TODAY}\n{'='*50}")

    if not API_KEY:
        print("❌ ANTHROPIC_API_KEY não definida!")
        sys.exit(1)

    with open(HTML_FILE, 'r', encoding='utf-8') as f:
        html = f.read()

    client = anthropic.Anthropic(api_key=API_KEY)
    changed = False

    # 1. Agenda sites → novos FES
    print("\n📋 FASE 1: Agenda de festejos")
    new_fes = scrape_agenda(client, html)
    if new_fes:
        html, n = insert_fes_entries(html, new_fes)
        print(f"\n  → {n} novos eventos FES inseridos")
        changed = True

    # 2. TV sites → correcções + novos TV
    print("\n📺 FASE 2: Agenda TV")
    corrections, new_tv = scrape_tv(client, html)

    if corrections:
        html = apply_tv_corrections(html, corrections)
        changed = True

    if new_tv:
        html, n = insert_tv_entries(html, new_tv)
        print(f"\n  → {n} novos eventos TV inseridos")
        changed = True

    # 3. Validar sintaxe
    print("\n🔍 FASE 3: Validação")
    if not validate_js(html):
        print("❌ ABORTANDO — erro de sintaxe detectado, ficheiro NÃO gravado")
        sys.exit(1)

    if changed:
        html = update_version(html)
        with open(HTML_FILE, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"\n💾 {HTML_FILE} actualizado.")
    else:
        print("\nℹ Sem alterações — ficheiro não modificado.")

    print(f"\n✅ Concluído — {TODAY}\n")

if __name__ == "__main__":
    main()
