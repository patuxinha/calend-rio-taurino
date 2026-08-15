#!/usr/bin/env python3
"""
Calendário Taurino — Scraper Automático v5
Versão gratuita: pedidos directos aos sites + Claude Haiku sem web_search.
Corre diariamente via GitHub Actions às 07h00.
- NUNCA toca no CSS, HTML, vídeo de intro ou design
"""

import os, re, time, datetime, sys, subprocess
import requests
from bs4 import BeautifulSoup
import anthropic

API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
HTML_FILE = "index.html"
DADOS_FILE = "dados.js"
TODAY     = datetime.date.today()
HORIZON   = TODAY + datetime.timedelta(days=183)

MES_MAP = {1:'Jan',2:'Feb',3:'Mar',4:'Abr',5:'Mai',6:'Jun',
           7:'Jul',8:'Ago',9:'Set',10:'Out',11:'Nov',12:'Dez'}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-PT,pt;q=0.9,es;q=0.8,fr;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

SITES = [
    {"nome": "touradas.pt",        "url": "https://www.touradas.pt/agenda",                "pais": "pt", "flag": "🇵🇹", "pN": "Portugal"},
    {"nome": "portadossustos.com", "url": "https://www.portadossustos.com/",                "pais": "pt", "flag": "🇵🇹", "pN": "Portugal"},
    {"nome": "touroeouro.com",     "url": "https://touroeouro.com/",                        "pais": "pt", "flag": "🇵🇹", "pN": "Portugal"},
    {"nome": "mundotoro.com",      "url": "https://www.mundotoro.com/agenda-taurina",       "pais": "es", "flag": "🇪🇸", "pN": "Espanha"},
    {"nome": "cultoro.es",         "url": "https://cultoro.es/agenda-taurina",              "pais": "es", "flag": "🇪🇸", "pN": "Espanha"},
    {"nome": "ladivisa.es",        "url": "https://www.ladivisa.es/",                       "pais": "es", "flag": "🇪🇸", "pN": "Espanha"},
    {"nome": "tertulias.fr",       "url": "https://www.tertulias.fr/cartels-2026/",         "pais": "fr", "flag": "🇫🇷", "pN": "França"},
    {"nome": "vueltaalostoros.fr", "url": "https://www.vueltaalostoros.fr/cartels/",        "pais": "fr", "flag": "🇫🇷", "pN": "França"},
    {"nome": "torosenelmundo.com", "url": "https://torosenelmundo.com/calendario/",         "pais": "am", "flag": "🌎", "pN": "América"},
    {"nome": "voyalostoros.com",   "url": "https://www.voyalostoros.com/",                  "pais": "am", "flag": "🌎", "pN": "América"},
]

SITES_TV = [
    {"nome": "elmuletazo.com", "url": "https://elmuletazo.com/agenda-de-toros-en-television/"},
    {"nome": "cultoro.es TV",  "url": "https://cultoro.es/toros-en-television-agenda"},
]

# ── Utilitários ────────────────────────────────────────────────────────────────

def js_escape(s):
    if not s: return ''
    return str(s).replace('\\','\\\\').replace("'","\\'")

def strip_md_fences(text):
    text = re.sub(r'```[a-zA-Z]*\n?', '', text)
    return text.replace('```', '').strip()

def is_in_window(dt_str):
    try:
        d = datetime.date.fromisoformat(dt_str) if isinstance(dt_str, str) else dt_str
        return TODAY <= d <= HORIZON
    except:
        return False

def fetch(url, timeout=25):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  ⚠ Erro {url}: {e}")
        return ""

def clean_text(html, max_chars=20000):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script","style","nav","footer","header","aside","form","button"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text[:max_chars]

def chunk_text(text, chunk_size=15000, overlap=500):
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text): break
        start = end - overlap
    return chunks

def split_top_level_objects(text):
    objects = []
    depth = 0; start = None; in_string = False; string_char = None; escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape: escape = False
            elif ch == '\\': escape = True
            elif ch == string_char: in_string = False
            continue
        if ch in ("'", '"'):
            in_string = True; string_char = ch; continue
        if ch == '{':
            if depth == 0: start = i
            depth += 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    objects.append(text[start:i+1]); start = None
    return objects

def fes_keys(content):
    m = re.search(r'(?:const|var) FES\s*=\s*\[', content)
    if not m: return set()
    arr_open = content.find('[', m.start())
    depth = 0; arr_close = None
    for i in range(arr_open, len(content)):
        if content[i]=='[': depth+=1
        elif content[i]==']':
            depth-=1
            if depth==0: arr_close=i; break
    if arr_close is None: return set()
    body = content[arr_open+1:arr_close]
    dts  = re.findall(r"dt:'(\d{4}-\d{2}-\d{2})'", body)
    locs = re.findall(r"loc:'([^']+)'", body)
    return {f"{dt}|{locs[i][:30]}" for i, dt in enumerate(dts) if i < len(locs)}

def tv_keys(content):
    pat = r"dt:'(\d{4}-\d{2}-\d{2})'[^}]*?chan:'([^']*)'[^}]*?loc:'([^']*)'"
    return {f"{dt}|{ch}|{loc[:20]}" for dt, ch, loc in re.findall(pat, content)}

# ── Claude Haiku (sem web_search) ─────────────────────────────────────────────

def ask_claude(client, prompt, max_tokens=4000):
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}]
        )
        return strip_md_fences(msg.content[0].text.strip())
    except Exception as e:
        print(f"  ⚠ Erro Claude: {e}")
        return ""

# ── Scraping ───────────────────────────────────────────────────────────────────

def scrape_site(client, site, existing):
    new_entries = []
    print(f"\n🌐 {site['nome']}...")
    raw = fetch(site["url"])
    if len(raw) < 200:
        return []

    text = clean_text(raw)
    chunks = chunk_text(text)
    pais, flag, pN = site['pais'], site['flag'], site['pN']

    americas_extra = ""
    if pais == 'am':
        americas_extra = """
Para cada evento, identifica o país correcto:
  Peru → p:'pe', flag:'🇵🇪', pN:'Peru'
  Colômbia → p:'co', flag:'🇨🇴', pN:'Colômbia'
  México → p:'mx', flag:'🇲🇽', pN:'México'
  Venezuela → p:'ve', flag:'🇻🇪', pN:'Venezuela'
  Equador → p:'ec', flag:'🇪🇨', pN:'Equador'
  Outros → p:'am', flag:'🌎', pN:'América'"""

    for chunk_i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            print(f"  chunk {chunk_i+1}/{len(chunks)}...")

        prompt = f"""Analisa este texto do site taurino "{site['nome']}".
Hoje: {TODAY}. Extrai APENAS eventos futuros (data >= {TODAY}).{americas_extra}

Devolve UM objecto JS por linha — TODOS os eventos, sem omitir nenhum:
{{dt:'YYYY-MM-DD',dtE:'YYYY-MM-DD',dia:'D',mes:'Mmm',p:'{pais}',flag:'{flag}',pN:'{pN}',nom:'Nome do evento ou feria',loc:'Praça, Cidade',mod:'corrida',top:0,feria:0,tv:0,lat:0,lon:0,bi:'{site["url"]}',c:{{dh:'D Mmm YYYY',t:'Ganadaria exacta',to:[{{n:'Nome Toureiro',nat:'{flag}',r:'MATADOR'}}],p:'Nome da Praça',cap:'A confirmar'}},no:'',fi:null}}

IMPORTANTE:
- t: ganadaria/vacada exacta (ex: 'Miura', 'Fuente Ymbro')
- to: lista de toureiros/cavaleiros com nome completo
- r: 'MATADOR', 'CAVALEIRO', 'NOVILHEIRO' ou 'REJONEADOR'
- nom: nome da feria ou evento
- mes: Jan Feb Mar Abr Mai Jun Jul Ago Set Out Nov Dez
- mod: corrida / rejones / misto
- SÓ objectos JS válidos, sem texto extra, sem ```

TEXTO:
{chunk}"""

        resp = ask_claude(client, prompt)
        if not resp:
            continue

        for obj in split_top_level_objects(resp):
            m_dt  = re.search(r"dt:'(\d{4}-\d{2}-\d{2})'", obj)
            m_loc = re.search(r"loc:'([^']+)'", obj)
            if not m_dt or not m_loc: continue
            dt  = m_dt.group(1)
            loc = m_loc.group(1)[:30]
            key = f"{dt}|{loc}"
            if not is_in_window(dt) or key in existing: continue
            new_entries.append(obj)
            existing.add(key)
            m_nom = re.search(r"nom:'([^']*)'", obj)
            print(f"  ✓ {dt} | {loc[:30]} | {m_nom.group(1)[:25] if m_nom else ''}")

        time.sleep(2)

    return new_entries

def scrape_tv(client, dados_content):
    existing = tv_keys(dados_content)
    new_tv = []

    for site in SITES_TV:
        print(f"\n📺 {site['nome']}...")
        raw = fetch(site["url"])
        if len(raw) < 200: continue

        chunks = chunk_text(clean_text(raw, 30000))
        for chunk in chunks:
            prompt = f"""Analisa este texto de agenda de toros em televisão.
Hoje: {TODAY}. Lista eventos televisados de {TODAY} a {TODAY + datetime.timedelta(days=60)}.

Uma linha por evento:
DATA|HORA|CANAL|LOCAL|NOME|GANADARIA|TOUREIROS (vírgula)

- DATA: YYYY-MM-DD, HORA: HH:MM
- Sem cabeçalhos, sem texto extra

TEXTO:
{chunk}"""
            resp = ask_claude(client, prompt, max_tokens=3000)
            if not resp: continue

            for line in resp.splitlines():
                parts = line.strip().split('|')
                if len(parts) < 5: continue
                try:
                    dt_str = parts[0].strip()
                    hora   = parts[1].strip()
                    canal  = parts[2].strip()
                    local  = parts[3].strip()
                    desc   = parts[4].strip()
                    ganad  = parts[5].strip() if len(parts)>5 else ''
                    tours  = [t.strip() for t in parts[6].split(',')] if len(parts)>6 else []
                    d = datetime.date.fromisoformat(dt_str)
                    if not (TODAY <= d <= TODAY + datetime.timedelta(days=60)): continue
                    key = f"{dt_str}|{canal}|{local[:20]}"
                    if key in existing: continue
                    mes = MES_MAP[d.month]; dia = str(d.day)
                    pflag = '🇵🇹' if any(p in canal for p in ['RTP','SIC','TVI']) else '🇪🇸'
                    to_arr = ','.join([f"{{n:'{js_escape(t)}',nat:'🇪🇸',r:'MATADOR'}}" for t in tours if t])
                    tv_line = (f"{{dt:'{dt_str}',dia:'{dia}',mes:'{mes}',"
                               f"chan:'{js_escape(canal)}',hora:'{hora}h {pflag}',"
                               f"loc:'{js_escape(local)}',nom:'{js_escape(desc)}',"
                               f"cartel:'{js_escape(desc)}',"
                               f"c:{{dh:'{dia} {mes} {d.year}',t:'{js_escape(ganad)}',to:[{to_arr}],p:'{js_escape(local)}',cap:'A confirmar'}},"
                               f"link:'https://elmuletazo.com/agenda-de-toros-en-television/'}}")
                    new_tv.append(tv_line)
                    existing.add(key)
                    print(f"  ✓ TV {dt_str} {canal[:25]} | {local[:25]}")
                except: continue
            time.sleep(2)

    return new_tv

# ── Remove eventos passados ────────────────────────────────────────────────────

def prune_past_events(content):
    total_removed = 0

    def prune_array(text, array_name, hor):
        nonlocal total_removed
        start_m = re.search(rf'(?:const|var) {array_name}\s*=\s*\[', text)
        if not start_m: return text
        arr_open = text.find('[', start_m.start())
        depth = 0; arr_close = None
        for i in range(arr_open, len(text)):
            c = text[i]
            if c=='[': depth+=1
            elif c==']':
                depth-=1
                if depth==0: arr_close=i; break
        if arr_close is None: return text
        body = text[arr_open+1:arr_close]
        kept = []; removed = 0
        for obj in split_top_level_objects(body):
            m = re.search(r"dt[E]?:'(\d{4}-\d{2}-\d{2})'", obj)
            if not m: kept.append(obj); continue
            try:
                dt = datetime.date.fromisoformat(m.group(1))
                if TODAY <= dt <= hor: kept.append(obj)
                else: removed+=1; total_removed+=1
            except: kept.append(obj)
        print(f"  {array_name}: removidos {removed}, mantidos {len(kept)}")
        return text[:arr_open+1] + '\n' + ',\n'.join(kept) + '\n' + text[arr_close:]

    content = prune_array(content, 'FES', HORIZON)
    content = prune_array(content, 'RUA', HORIZON)
    content = prune_array(content, 'TV_AGENDA', TODAY + datetime.timedelta(days=90))
    return content, total_removed

# ── Insere FES ─────────────────────────────────────────────────────────────────

def insert_fes(content, entries):
    if not entries: return content, 0
    marker = '];\n\nvar RUA=['
    idx = content.find(marker)
    if idx < 0: marker = '];\n\nconst RUA=['
    idx = content.find(marker)
    if idx < 0:
        print("  ⚠ Marcador FES não encontrado!")
        return content, 0
    block = f"\n\n  /* AUTO {TODAY} */\n" + "\n".join(f"  ,{e}" for e in entries) + "\n"
    return content[:idx] + block + content[idx:], len(entries)

# ── Insere TV ──────────────────────────────────────────────────────────────────

def insert_tv(content, tv_entries):
    if not tv_entries: return content, 0
    for marker in ['];\n\nvar MES_TV', '];\n\nconst MES_TV',
                   '];\n\nvar GANADARIAS', '];\n\nconst GANADARIAS']:
        idx = content.find(marker)
        if idx >= 0:
            block = "\n" + "\n".join(f"  ,{e}" for e in tv_entries) + "\n"
            return content[:idx] + block + content[idx:], len(tv_entries)
    print("  ⚠ Marcador TV não encontrado!")
    return content, 0

# ── Valida JS ──────────────────────────────────────────────────────────────────

def validate_js(content):
    js = content
    if '<script' in content:
        scripts = re.findall(r'<script(?:[^>]*)>(.*?)</script>', content, re.DOTALL)
        js = max(scripts, key=len) if scripts else ''
    if not js.strip(): return True
    open('/tmp/_ctb.js','w',encoding='utf-8').write(js)
    r = subprocess.run(['node','--check','/tmp/_ctb.js'], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print("  ❌ JS inválido:", r.stderr[:200]); return False
    print("  ✅ JS válido"); return True

# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    print(f"\n🐂 CTB Scraper v5 — {TODAY}\n{'='*50}")
    if not API_KEY:
        print("❌ ANTHROPIC_API_KEY não definida!"); sys.exit(1)

    try:
        dados = open(DADOS_FILE, 'r', encoding='utf-8').read()
    except FileNotFoundError:
        print(f"❌ {DADOS_FILE} não encontrado!"); sys.exit(1)

    client = anthropic.Anthropic(api_key=API_KEY)
    changed = False

    # Fase 0: Limpeza
    print("\n🧹 FASE 0: Limpeza de eventos passados")
    dados, pruned = prune_past_events(dados)
    if pruned > 0:
        print(f"  → {pruned} removidos"); changed = True
    else:
        print("  → Nada a remover")

    # Fase 1: Festejos
    print("\n📋 FASE 1: Festejos")
    existing = fes_keys(dados)
    new_fes = []
    for site in SITES:
        entries = scrape_site(client, site, existing)
        new_fes.extend(entries)

    if new_fes:
        dados, n = insert_fes(dados, new_fes)
        print(f"\n  → {n} eventos FES inseridos")
        changed = True

    # Fase 2: TV
    print("\n📺 FASE 2: Agenda TV")
    new_tv = scrape_tv(client, dados)
    if new_tv:
        dados, n = insert_tv(dados, new_tv)
        print(f"  → {n} eventos TV inseridos")
        changed = True

    # Fase 3: Validação
    print("\n🔍 FASE 3: Validação JS")
    if not validate_js(dados):
        print("❌ ABORTADO"); sys.exit(1)

    if changed:
        open(DADOS_FILE, 'w', encoding='utf-8').write(dados)
        print(f"\n💾 {DADOS_FILE} actualizado")
    else:
        print("\nℹ Sem alterações")

    print(f"\n✅ Concluído — {TODAY}\n")

if __name__ == "__main__":
    main()
