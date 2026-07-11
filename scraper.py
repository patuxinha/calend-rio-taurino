#!/usr/bin/env python3
"""
Calendário Taurino — Scraper Automático v3
Corre diariamente via GitHub Actions às 07h00.
- Adiciona eventos novos (FES + TV)
- Corrige canais TV errados
- Valida sintaxe JS antes de gravar
- NUNCA toca no CSS, HTML, vídeo de intro ou design
"""

import os, re, time, datetime, sys, subprocess
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

SITES_AGENDA = [
    {"nome": "touradas.pt",        "url": "https://www.touradas.pt/agenda",                "pais": "pt"},
    {"nome": "portadossustos.com", "url": "https://www.portadossustos.com/",                "pais": "pt"},
    {"nome": "touroeouro.com",     "url": "https://touroeouro.com/",                        "pais": "pt"},
    {"nome": "mundotoro.com",      "url": "https://www.mundotoro.com/agenda-taurina",       "pais": "es"},
    {"nome": "cultoro.es",         "url": "https://cultoro.es/agenda-taurina",              "pais": "es"},
    {"nome": "ladivisa.es",        "url": "https://www.ladivisa.es/",                       "pais": "es"},
    {"nome": "tauromaquia.com.pt", "url": "https://www.tauromaquia.com.pt/",                "pais": "pt"},
    # Américas — fontes globais com cobertura de Peru, Colombia, México, Venezuela, Equador, etc.
    {"nome": "torosenelmundo.com", "url": "https://torosenelmundo.com/calendario/",         "pais": "am"},
    {"nome": "voyalostoros.com",   "url": "https://www.voyalostoros.com/",                  "pais": "am"},
    # França
    {"nome": "tertulias.fr",       "url": "https://www.tertulias.fr/cartels-2026/",          "pais": "fr"},
    {"nome": "vueltaalostoros.fr", "url": "https://www.vueltaalostoros.fr/cartels/",         "pais": "fr"},
]

SITES_TV = [
    {"nome": "elmuletazo.com", "url": "https://elmuletazo.com/agenda-de-toros-en-television/"},
    {"nome": "cultoro.es TV",  "url": "https://cultoro.es/toros-en-television-agenda"},
    {"nome": "tertulias.fr TV","url": "https://www.tertulias.fr/cartels-2026/"},
    {"nome": "torosenelmundo TV","url": "https://torosenelmundo.com/calendario/"},
]

MES_MAP = {1:'Jan',2:'Feb',3:'Mar',4:'Abr',5:'Mai',6:'Jun',
           7:'Jul',8:'Ago',9:'Set',10:'Out',11:'Nov',12:'Dez'}

HORIZON = TODAY + datetime.timedelta(days=183)  # 6 meses à frente

# ── Utilidades ─────────────────────────────────────────────────────────────────

def fetch(url, timeout=20):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  ⚠ Erro {url}: {e}")
        return ""

def clean_text(html, max_chars=80000):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script","style","nav","footer","header","aside","form","button"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text[:max_chars]

def chunk_text(text, chunk_size=15000, overlap=500):
    """Divide texto em chunks com sobreposição para não perder eventos na fronteira."""
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks

def strip_md_fences(text):
    """Remove blocos de markdown ```json / ```javascript / ``` que a API por vezes
    devolve apesar do prompt pedir o contrário. Sem isto, o texto fica inserido
    literalmente no JS e quebra a sintaxe de todo o ficheiro."""
    text = re.sub(r'```[a-zA-Z]*\n?', '', text)
    text = text.replace('```', '')
    return text.strip()

def js_escape(s):
    """Escapa aspas simples e backslashes para inserção segura em literais JS."""
    if s is None:
        return ''
    return str(s).replace('\\', '\\\\').replace("'", "\\'")

def split_top_level_objects(text):
    """Extrai todos os objectos JS de nível superior de um texto, de forma
    robusta a vírgulas ou novas linhas em falta entre objectos (a API às vezes
    esquece a vírgula separadora). Ignora chavetas dentro de strings."""
    objects = []
    depth = 0
    start = None
    in_string = False
    string_char = None
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == string_char:
                in_string = False
            continue
        if ch in ("'", '"'):
            in_string = True
            string_char = ch
            continue
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    objects.append(text[start:i+1])
                    start = None
    return objects

def ask_claude(client, prompt, max_tokens=2500):
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

def is_future(dt_str):
    try:
        d = datetime.date.fromisoformat(dt_str) if isinstance(dt_str, str) else dt_str
        return TODAY <= d <= HORIZON
    except:
        return False

# ── Chaves de deduplicação ─────────────────────────────────────────────────────

def fes_keys(html):
    dts  = re.findall(r"dt:'(\d{4}-\d{2}-\d{2})'", html)
    locs = re.findall(r"loc:'([^']+)'", html)
    return {f"{dt}|{locs[i][:30]}" for i, dt in enumerate(dts) if i < len(locs)}

def tv_keys(html):
    pat = r"dt:'(\d{4}-\d{2}-\d{2})'[^}]*?chan:'([^']*)'[^}]*?loc:'([^']*)'"
    return {f"{dt}|{ch}|{loc[:20]}" for dt, ch, loc in re.findall(pat, html)}

# ── Fase 1: Agenda ─────────────────────────────────────────────────────────────

def scrape_agenda(client, html):
    existing = fes_keys(html)
    new_entries = []

    for site in SITES_AGENDA:
        print(f"\n🌐 {site['nome']}...")
        raw = fetch(site["url"])
        if len(raw) < 200:
            continue

        text = clean_text(raw)
        chunks = chunk_text(text)
        print(f"  → {len(chunks)} chunk(s) de texto")

        if site['pais'] == 'pt':
            flag, pN = '🇵🇹', 'Portugal'
        elif site['pais'] == 'es':
            flag, pN = '🇪🇸', 'Espanha'
        elif site['pais'] == 'fr':
            flag, pN = '🇫🇷', 'França'
        else:
            flag, pN = '🌎', 'América'

        americas_extra = ""
        if site['pais'] == 'am':
            americas_extra = """
IMPORTANTE: Este site cobre vários países das Américas (Peru, Colômbia, México, Venezuela, Equador, etc.).
Para cada evento, usa a flag e pN do país correcto:
  Peru → flag:'🇵🇪', pN:'Peru', p:'pe'
  Colômbia → flag:'🇨🇴', pN:'Colômbia', p:'co'
  México → flag:'🇲🇽', pN:'México', p:'mx'
  Venezuela → flag:'🇻🇪', pN:'Venezuela', p:'ve'
  Equador → flag:'🇪🇨', pN:'Equador', p:'ec'
  Outros → flag:'🌎', pN:'América', p:'am'"""

        for chunk_i, chunk in enumerate(chunks):
            print(f"  chunk {chunk_i+1}/{len(chunks)}...")
            prompt = f"""Analisa este texto do site taurino "{site['nome']}" (país: {site['pais']}).
Hoje: {TODAY}. Extrai APENAS eventos futuros (data >= {TODAY}).{americas_extra}

Devolve UM objecto JS por linha — TODOS os eventos que encontrares, sem omitir nenhum:
{{dt:'YYYY-MM-DD',dtE:'YYYY-MM-DD',dia:'D',mes:'Mmm',p:'{site['pais']}',flag:'{flag}',pN:'{pN}',nom:'Nome',loc:'Praca, Cidade',mod:'rejones',top:0,feria:0,tv:0,lat:0,lon:0,bi:'{site["url"]}',c:{{dh:'D Mmm YYYY',t:'Ganadaria',to:[{{n:'Nome',nat:'{flag}',r:'TORERO'}}],p:'Praca',cap:'A confirmar'}},no:'',fi:null}}

mes: Jan Feb Mar Abr Mai Jun Jul Ago Set Out Nov Dez
mod: rejones / corrida / misto
SÓ objectos JS, sem texto extra, sem ```.

TEXTO:
{chunk}"""

            resp = ask_claude(client, prompt, max_tokens=4000)
            if not resp:
                continue

            for obj in split_top_level_objects(resp):
                obj = obj.strip()

                m_dt  = re.search(r"dt:'(\d{4}-\d{2}-\d{2})'", obj)
                m_loc = re.search(r"loc:'([^']+)'", obj)
                if not m_dt or not m_loc:
                    continue

                dt  = m_dt.group(1)
                loc = m_loc.group(1)[:30]
                key = f"{dt}|{loc}"

                if not is_future(dt) or key in existing:
                    continue

                new_entries.append(obj)
                existing.add(key)
                print(f"  ✓ {dt} | {loc[:50]}")

            time.sleep(2)

    return new_entries

# ── Fase 2: TV ─────────────────────────────────────────────────────────────────

MESES_ES = {
    'enero':1,'febrero':2,'marzo':3,'abril':4,'mayo':5,'junio':6,
    'julio':7,'agosto':8,'septiembre':9,'octubre':10,'noviembre':11,'diciembre':12
}

def parse_elmuletazo(raw_html):
    """Extracção directa do elmuletazo.com — processa blocos entre eventos."""
    soup = BeautifulSoup(raw_html, "lxml")
    for tag in soup(["script","style","nav","footer","header","aside","img"]):
        tag.decompose()
    # Usar separador especial para preservar estrutura
    text = soup.get_text(separator=" ", strip=True)
    # Normalizar espaços múltiplos
    text = re.sub(r'\s+', ' ', text)

    events = []
    # Dividir por 🗓 — cada bloco é um evento
    blocks = re.split(r'(?=🗓\s+\w+\s+\d+\s+de\s+\w+\s+de\s+\d{4})', text)

    for block in blocks:
        if '🗓' not in block or '⏰' not in block:
            continue

        # Data
        m_date = re.search(r'🗓\s+\w+\s+(\d+)\s+de\s+(\w+)\s+de\s+(\d{4})', block)
        m_hora = re.search(r'⏰[️]?\s*(\d{2}:\d{2})h', block)
        if not m_date or not m_hora:
            continue

        dia_num = int(m_date.group(1))
        mes_str = m_date.group(2).lower()
        ano = int(m_date.group(3))
        hora = m_hora.group(1)
        mes_num = MESES_ES.get(mes_str)
        if not mes_num:
            continue
        try:
            dt = datetime.date(ano, mes_num, dia_num)
        except:
            continue

        # Canal — entre 📺 e 🏟
        m_canal = re.search(r'📺\s*([^🏟🐂📜🔗]+?)(?:\s*🏟)', block)
        canal = re.sub(r'\s+', ' ', m_canal.group(1)).strip().rstrip('.') if m_canal else ''
        canal = canal[:60]

        # Local — entre 🏟 e 🐂
        m_local = re.search(r'🏟\s*([^🐂📜🔗]+?)(?:\s*🐂)', block)
        local = re.sub(r'\s+', ' ', m_local.group(1)).strip().rstrip('.') if m_local else ''
        local = local[:60]

        # Descrição — entre 🐂 e (📜 ou 🔗 ou fim)
        m_desc = re.search(r'🐂\s*(.+?)(?:📜|🔗|$)', block, re.DOTALL)
        desc = re.sub(r'\s+', ' ', m_desc.group(1)).strip().rstrip('.') if m_desc else ''

        # Cartel — depois de 📜 até 🔗
        ganaderia = ''
        toureiros = []
        m_cartel = re.search(r'📜\s*(.+?)(?:🔗|$)', block, re.DOTALL)
        if m_cartel:
            cartel_txt = re.sub(r'\s+', ' ', m_cartel.group(1)).strip()
            # "Toros/Novillos/Erales/Reses de X para Y, Z y W"
            m_gan = re.search(
                r'(?:Toros?|Novillos?|Erales?|Reses?|Becerros?)\s+de\s+(.+?)\s+para\s+(.+?)(?:\.\s*$|$)',
                cartel_txt, re.IGNORECASE
            )
            if m_gan:
                ganaderia = m_gan.group(1).strip().rstrip('.,')[:80]
                toureiros_raw = m_gan.group(2).strip().rstrip('.,')
                # Normaliza separadores: "y", "e", "i" entre toureiros
                toureiros_raw = re.sub(r'\s+(?:y|e|i)\s+', ', ', toureiros_raw)
                toureiros = [t.strip().rstrip('.,') for t in re.split(r',\s*', toureiros_raw) if len(t.strip()) > 2][:8]
            else:
                ganaderia = cartel_txt[:80]

        if local:
            events.append({
                'dt': dt, 'hora': hora,
                'canal': canal, 'local': local,
                'desc': desc[:200], 'ganaderia': ganaderia,
                'toureiros': toureiros,
            })

    print(f"  → parse_elmuletazo: {len(events)} eventos, {sum(1 for e in events if e['toureiros'])} com toureiros")
    return events

def scrape_tv(client, html):
    existing = tv_keys(html)
    corrections = []
    new_tv = []

    tv_existing_list = re.findall(
        r"dt:'(\d{4}-\d{2}-\d{2})'[^}]*?chan:'([^']*)'[^}]*?loc:'([^']*)'",
        html
    )

    def process_tv_entry(dt_obj, hora, canal, local, desc, dt_str=None, ganaderia='', toureiros=None):
        """Processa uma entrada TV e adiciona se for nova."""
        if toureiros is None:
            toureiros = []
        if dt_str is None:
            dt_str = dt_obj.isoformat()
        try:
            d = dt_obj if isinstance(dt_obj, datetime.date) else datetime.date.fromisoformat(dt_str)
        except:
            return
        if not (TODAY <= d <= HORIZON):
            return
        for ex_dt, ex_chan, ex_loc in tv_existing_list:
            if ex_dt == dt_str and local[:20] == ex_loc[:20] and canal != ex_chan:
                corrections.append({'dt': dt_str, 'old_chan': ex_chan, 'new_chan': canal, 'loc': ex_loc})
                print(f"  ✏ {dt_str} {ex_loc[:25]}: {ex_chan} → {canal}")
        key = f"{dt_str}|{canal}|{local[:20]}"
        if key in existing:
            return
        mes = MES_MAP[d.month]
        dia = str(d.day)
        pais_flag = '🇵🇹' if any(p in canal for p in ['RTP','SIC','TVI']) else '🇪🇸'
        # Constrói array de toureiros
        to_arr = ','.join([f"{{n:'{js_escape(t)}',nat:'🇪🇸',r:'MATADOR'}}" for t in toureiros]) if toureiros else ''
        tv_line = (
            f"{{dt:'{dt_str}',dia:'{dia}',mes:'{mes}',"
            f"chan:'{js_escape(canal)}',hora:'{hora}h {pais_flag}',"
            f"loc:'{js_escape(local)}',nom:'{js_escape(desc[:80])}',"
            f"cartel:'{js_escape(desc)}',"
            f"c:{{dh:'{dia} {mes} {d.year}',t:'{js_escape(ganaderia)}',to:[{to_arr}],p:'{js_escape(local)}',cap:'A confirmar'}},"
            f"link:'https://elmuletazo.com/agenda-de-toros-en-television/'}}"
        )
        new_tv.append(tv_line)
        existing.add(key)
        print(f"  ✓ TV {dt_str} {canal[:30]} | {local[:30]}" + (f" | {ganaderia[:30]}" if ganaderia else ''))

    # ── Fonte 1: elmuletazo.com — parsing directo ──────────────────────────────
    print(f"\n📺 elmuletazo.com (parsing directo)...")
    raw = fetch(SITES_TV[0]["url"])
    if len(raw) > 200:
        events = parse_elmuletazo(raw)
        print(f"  → {len(events)} eventos encontrados")
        for ev in events:
            process_tv_entry(ev['dt'], ev['hora'], ev['canal'], ev['local'], ev['desc'],
                           ganaderia=ev.get('ganaderia',''), toureiros=ev.get('toureiros',[]))

    # ── Fontes adicionais: via Claude API ──────────────────────────────────────
    for site in SITES_TV[1:]:
        print(f"\n📺 {site['nome']} (via API)...")
        raw2 = fetch(site["url"])
        if len(raw2) < 200:
            continue
        chunks = chunk_text(clean_text(raw2, 40000), chunk_size=15000)
        for chunk_i, chunk in enumerate(chunks):
            print(f"  chunk {chunk_i+1}/{len(chunks)}...")
            prompt = f"""Analisa este texto de agenda de toros em televisão.
Hoje: {TODAY}. Lista TODOS os eventos televisados a partir de hoje.

Uma linha por evento, formato EXACTO (separado por |):
DATA|HORA|CANAL|LOCAL|NOME DO EVENTO|CARTEL

- DATA: YYYY-MM-DD
- HORA: HH:MM (sem h)
- CANAL: nome exacto
- Sem cabeçalhos, sem texto extra

TEXTO:
{chunk}"""
            resp = ask_claude(client, prompt, max_tokens=4000)
            if not resp:
                continue
            for line in resp.splitlines():
                parts = line.strip().split('|')
                if len(parts) < 5:
                    continue
                try:
                    dt_str = parts[0].strip()
                    hora   = parts[1].strip()
                    canal  = parts[2].strip()
                    local  = parts[3].strip()
                    desc   = '|'.join(parts[4:]).strip()
                    dt_obj = datetime.date.fromisoformat(dt_str)
                    process_tv_entry(dt_obj, hora, canal, local, desc, dt_str)
                except:
                    continue
            time.sleep(2)

    return corrections, new_tv

# ── Aplica correcções TV (sem regex complexo) ──────────────────────────────────

def apply_corrections(html, corrections):
    for corr in corrections:
        # Usa substituição simples de string, sem regex
        old_str = f"chan:'{corr['old_chan']}',hora:"
        new_str = f"chan:'{corr['new_chan']}',hora:"

        # Só substitui se a linha também contiver o dt e loc correctos
        lines = html.split('\n')
        new_lines = []
        for line in lines:
            if (corr['dt'] in line and
                corr['old_chan'] in line and
                corr['loc'][:15] in line):
                line = line.replace(old_str, new_str, 1)
                print(f"  ✅ Corrigido: {corr['dt']} {corr['old_chan']} → {corr['new_chan']}")
            new_lines.append(line)
        html = '\n'.join(new_lines)
    return html

# ── Insere FES ─────────────────────────────────────────────────────────────────

def insert_fes(html, entries):
    if not entries:
        return html, 0
    marker = '];\n\nconst RUA=['
    idx = html.find(marker)
    if idx < 0:
        print("  ⚠ Marcador FES não encontrado!")
        return html, 0
    block = f"\n\n  /* AUTO {TODAY} */\n" + "\n".join(f"  ,{e}" for e in entries) + "\n"
    return html[:idx] + block + html[idx:], len(entries)

# ── Insere TV ──────────────────────────────────────────────────────────────────

def insert_tv(html, tv_entries):
    if not tv_entries:
        return html, 0
    # Insere antes do fecho ]; do array TV_AGENDA
    marker = '];\n\nconst MES_TV'
    idx = html.find(marker)
    if idx < 0:
        # Tenta alternativo
        marker = '];\n\nconst GANADARIAS'
        idx = html.find(marker)
    if idx < 0:
        print("  ⚠ Marcador TV não encontrado!")
        return html, 0
    block = "\n" + "\n".join(f"  ,{e}" for e in tv_entries) + "\n"
    return html[:idx] + block + html[idx:], len(tv_entries)

# ── Valida JS ──────────────────────────────────────────────────────────────────

def validate_js(html):
    """Valida a sintaxe JS real (não apenas contagem de chavetas) usando o
    'node --check'. Isto apanha qualquer corrupção — blocos ```json residuais,
    vírgulas em falta, aspas mal escapadas — antes do commit."""
    scripts = re.findall(r'<script(?:[^>]*)>(.*?)</script>', html, re.DOTALL)
    main_script = max(scripts, key=len) if scripts else ''
    if not main_script.strip():
        print("  ⚠ Nenhum script encontrado para validar")
        return False

    tmp_path = '/tmp/_ctb_validate.js'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        f.write(main_script)

    try:
        result = subprocess.run(
            ['node', '--check', tmp_path],
            capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:
        print("  ⚠ Node.js não disponível — a usar verificação de chavetas como fallback")
        opens, closes = main_script.count('{'), main_script.count('}')
        if opens != closes:
            print(f"  ❌ Erro sintaxe: {{ {opens}  }} {closes}  diff {opens-closes}")
            return False
        print(f"  ✅ JS OK (fallback): {{ }} = {opens}")
        return True

    if result.returncode != 0:
        print("  ❌ Erro de sintaxe JS detectado pelo node --check:")
        print(result.stderr)
        return False

    print("  ✅ JS válido (node --check)")
    return True

# ── Versão ─────────────────────────────────────────────────────────────────────

def update_version(html):
    hoje = TODAY.strftime("%Y%m%d")
    return re.sub(
        r'<!DOCTYPE html><!-- CTB-v\d{8}[^>]* -->',
        f'<!DOCTYPE html><!-- CTB-v{hoje}-AUTO -->',
        html
    )

# ── Remove eventos passados (mantém o ficheiro leve para mobile) ───────────────

def prune_past_events(html):
    """Remove entradas com data anterior a hoje dos arrays FES e TV_AGENDA.
    Evita que o ficheiro cresça indefinidamente e crache o Safari mobile."""

    scripts_iter = list(re.finditer(r'<script(?:[^>]*)>(.*?)</script>', html, re.DOTALL))
    if not scripts_iter:
        return html, 0
    main_match = max(scripts_iter, key=lambda m: len(m.group(1)))
    main = main_match.group(1)
    total_removed = 0

    def prune_array(script, array_name, date_field="dt"):
        nonlocal total_removed
        start_m = re.search(rf'const {array_name}\s*=\s*\[', script)
        if not start_m:
            return script
        arr_open = script.find('[', start_m.start())
        depth = 0
        arr_close = None
        for i in range(arr_open, len(script)):
            c = script[i]
            if c == '[': depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    arr_close = i; break
        if arr_close is None:
            return script
        body = script[arr_open+1:arr_close]
        kept = []
        for obj in split_top_level_objects(body):
            m = re.search(rf"{date_field}:'(\d{{4}}-\d{{2}}-\d{{2}})'", obj)
            if not m:
                kept.append(obj); continue
            try:
                dt = datetime.date.fromisoformat(m.group(1))
                if TODAY <= dt <= HORIZON:
                    kept.append(obj)
                else:
                    total_removed += 1
            except:
                kept.append(obj)
        new_body = '\n' + ',\n'.join(kept) + '\n'
        return script[:arr_open+1] + new_body + script[arr_close:]

    main = prune_array(main, 'FES')
    main = prune_array(main, 'TV_AGENDA')

    new_html = html[:main_match.start(1)] + main + html[main_match.end(1):]
    return new_html, total_removed

# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    print(f"\n🐂 CTB Scraper v3 — {TODAY}\n{'='*50}")

    if not API_KEY:
        print("❌ ANTHROPIC_API_KEY não definida!")
        sys.exit(1)

    with open(HTML_FILE, 'r', encoding='utf-8') as f:
        html = f.read()

    client = anthropic.Anthropic(api_key=API_KEY)
    changed = False

    # Fase 0: Limpeza de eventos passados
    print("\n🧹 FASE 0: Limpeza de eventos passados")
    html, pruned = prune_past_events(html)
    if pruned > 0:
        print(f"  → {pruned} eventos passados removidos")
        changed = True
    else:
        print("  → Nada a remover")

    # Fase 1: Agenda
    print("\n📋 FASE 1: Festejos")
    new_fes = scrape_agenda(client, html)
    if new_fes:
        html, n = insert_fes(html, new_fes)
        print(f"  → {n} eventos FES inseridos")
        changed = True

    # Fase 2: TV
    print("\n📺 FASE 2: Agenda TV")
    corrections, new_tv = scrape_tv(client, html)

    if corrections:
        html = apply_corrections(html, corrections)
        changed = True

    if new_tv:
        html, n = insert_tv(html, new_tv)
        print(f"  → {n} eventos TV inseridos")
        changed = True

    # Validar
    print("\n🔍 FASE 3: Validação JS")
    if not validate_js(html):
        print("❌ ABORTADO — sintaxe inválida, ficheiro NÃO gravado")
        sys.exit(1)

    if changed:
        html = update_version(html)
        with open(HTML_FILE, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"\n💾 {HTML_FILE} actualizado com sucesso")
    else:
        print("\nℹ Sem alterações")

    print(f"\n✅ Concluído — {TODAY}\n")

if __name__ == "__main__":
    main()
