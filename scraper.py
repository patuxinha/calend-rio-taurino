#!/usr/bin/env python3
"""
Calendário Taurino — Scraper Automático v4
Usa a API da Anthropic com web_search para contornar bloqueios anti-scraping.
Corre diariamente via GitHub Actions às 07h00.
- NUNCA toca no CSS, HTML, vídeo de intro ou design
"""

import os, re, time, datetime, sys, subprocess, json
import anthropic

API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
HTML_FILE = "index.html"
TODAY     = datetime.date.today()
HORIZON   = TODAY + datetime.timedelta(days=183)

MES_MAP = {1:'Jan',2:'Feb',3:'Mar',4:'Abr',5:'Mai',6:'Jun',
           7:'Jul',8:'Ago',9:'Set',10:'Out',11:'Nov',12:'Dez'}

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

def fes_keys(html):
    m = re.search(r'(?:const|var) FES\s*=\s*\[', html)
    if not m: return set()
    arr_open = html.find('[', m.start())
    depth = 0; arr_close = None
    for i in range(arr_open, len(html)):
        if html[i]=='[': depth+=1
        elif html[i]==']':
            depth-=1
            if depth==0: arr_close=i; break
    body = html[arr_open+1:arr_close]
    dts  = re.findall(r"dt:'(\d{4}-\d{2}-\d{2})'", body)
    locs = re.findall(r"loc:'([^']+)'", body)
    return {f"{dt}|{locs[i][:30]}" for i, dt in enumerate(dts) if i < len(locs)}

def tv_keys(html):
    pat = r"dt:'(\d{4}-\d{2}-\d{2})'[^}]*?chan:'([^']*)'[^}]*?loc:'([^']*)'"
    return {f"{dt}|{ch}|{loc[:20]}" for dt, ch, loc in re.findall(pat, html)}

# ── Claude com web_search ──────────────────────────────────────────────────────

def ask_claude_with_search(client, prompt, max_tokens=4000):
    """Chama a API com a ferramenta web_search activa."""
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}]
        )
        # Extrai todo o texto da resposta
        text_parts = []
        for block in msg.content:
            if hasattr(block, 'text'):
                text_parts.append(block.text)
        result = ' '.join(text_parts)
        return strip_md_fences(result)
    except Exception as e:
        print(f"  ⚠ Erro Claude web_search: {e}")
        return ""

# ── Pesquisa de eventos por país/região ───────────────────────────────────────

PESQUISAS = [
    # (descricao, query, pais, flag, pN)
    ("Portugal - agenda",
     f"agenda taurina Portugal touradas corridas cavaleiros {TODAY.strftime('%B %Y')} próximos eventos site:touradas.pt OR site:portadossustos.com OR site:touroeouro.com",
     "pt", "🇵🇹", "Portugal"),

    ("Espanha - servitoro agenda completa",
     f"agenda taurina España corridas festejos {TODAY.strftime('%B %Y')} site:servitoro.com",
     "es", "🇪🇸", "Espanha"),

    ("Espanha - burladero.tv carteles",
     f"carteles taurinos {TODAY.strftime('%B %Y')} ganadería toreros site:escalafon.burladero.tv/carteles",
     "es", "🇪🇸", "Espanha"),

    ("Espanha - elestoconazo e agendataurina",
     f"agenda taurina corridas {TODAY.strftime('%B %Y')} carteles toreros ganadería site:elestoconazo.es OR site:agendataurina.info",
     "es", "🇪🇸", "Espanha"),

    ("Espanha - agendataurina mundotoro servitoro carteles",
     f"agenda taurina España corridas festejos {TODAY.strftime('%B %Y')} carteles completos ganadería toreros site:agendataurina.info OR site:mundotoro.com OR site:servitoro.com",
     "es", "🇪🇸", "Espanha"),

    ("França - agenda completa",
     f"agenda taurina France corridas ferias {TODAY.strftime('%B %Y')} cartels complets site:tertulias.fr OR site:vueltaalostoros.fr",
     "fr", "🇫🇷", "França"),

    ("França - Dax Bayonne Ceret Mont-de-Marsan",
     f"Feria de Dax 2026 Feria Bayonne 2026 Ceret Mont-de-Marsan agosto carteles toreros ganadería completo",
     "fr", "🇫🇷", "França"),

    ("América - Peru Colombia Mexico Venezuela",
     f"agenda taurina Peru Colombia Mexico Venezuela Ecuador {TODAY.strftime('%B %Y')} corridas carteles site:torosenelmundo.com OR site:voyalostoros.com",
     "am", "🌎", "América"),
]

PROMPT_TEMPLATE = """Pesquisa na web e lista TODOS os festejos taurinos futuros para {descricao}.
Hoje: {today}. Só eventos de {today} a {horizon}.

Para cada evento devolve UM objecto JS por linha com este formato EXACTO:
{{dt:'YYYY-MM-DD',dtE:'YYYY-MM-DD',dia:'D',mes:'Mmm',p:'{pais}',flag:'{flag}',pN:'{pN}',nom:'Nome do evento',loc:'Praça, Cidade',mod:'corrida',top:0,feria:0,tv:0,lat:0,lon:0,bi:'URL fonte',c:{{dh:'D Mmm YYYY',t:'Ganadaria exacta',to:[{{n:'Nome Toureiro',nat:'{flag}',r:'MATADOR'}}],p:'Nome da Praça',cap:'A confirmar'}},no:'',fi:null}}

REGRAS IMPORTANTES:
- dt/dtE: data ISO YYYY-MM-DD
- mes: Jan Feb Mar Abr Mai Jun Jul Ago Set Out Nov Dez
- t: ganadaria/vacada (ex: 'Miura', 'Fuente Ymbro') — obrigatório se disponível
- to: lista de toureiros/cavaleiros com nome completo
- r: 'MATADOR', 'CAVALEIRO', 'NOVILHEIRO' ou 'REJONEADOR'
- mod: 'corrida', 'rejones' ou 'misto'
- nom: nome da feria ou evento
- loc: nome da praça e cidade
- SÓ objectos JS válidos, sem texto extra, sem ```
- Pesquisa: {query}"""

PROMPT_ENRICH = """Pesquisa na web o cartel completo deste festejo taurino:
- Data: {dt}
- Local: {loc}
- Nome: {nom}

Devolve APENAS um objecto JS com o cartel completo:
{{dt:'{dt}',dtE:'{dt}',dia:'{dia}',mes:'{mes}',p:'{pais}',flag:'{flag}',pN:'{pN}',nom:'{nom}',loc:'{loc}',mod:'corrida',top:0,feria:0,tv:0,lat:0,lon:0,bi:'URL fonte',c:{{dh:'{dia} {mes}',t:'Ganadaria exacta',to:[{{n:'Nome Toureiro',nat:'{flag}',r:'MATADOR'}}],p:'Praça',cap:'A confirmar'}},no:'',fi:null}}

Pesquisa em: touroeouro.com, portadossustos.com, touradas.pt, mundotoro.com, agendataurina.info, burladero.tv
SÓ o objecto JS, sem texto extra."""

def enrich_entries(client, entries):
    """Para cada entrada sem toureiros, faz pesquisa específica para encontrar o cartel completo."""
    enriched = []
    for obj in entries:
        # Verifica se já tem toureiros
        if "to:[{n:'" in obj:
            enriched.append(obj)
            continue

        m_dt  = re.search(r"dt:'(\d{4}-\d{2}-\d{2})'", obj)
        m_loc = re.search(r"loc:'([^']+)'", obj)
        m_nom = re.search(r"nom:'([^']+)'", obj)
        m_dia = re.search(r"dia:'([^']+)'", obj)
        m_mes = re.search(r"mes:'([^']+)'", obj)
        m_p   = re.search(r",p:'([^']+)'", obj)
        m_flag= re.search(r"flag:'([^']+)'", obj)
        m_pN  = re.search(r"pN:'([^']+)'", obj)

        if not m_dt or not m_loc:
            enriched.append(obj)
            continue

        dt  = m_dt.group(1)
        loc = m_loc.group(1)
        nom = m_nom.group(1) if m_nom else ''
        dia = m_dia.group(1) if m_dia else dt.split('-')[2].lstrip('0')
        mes = m_mes.group(1) if m_mes else ''
        pais = m_p.group(1) if m_p else 'es'
        flag = m_flag.group(1) if m_flag else '🇪🇸'
        pN   = m_pN.group(1) if m_pN else 'Espanha'

        print(f"  🔎 A enriquecer: {dt} | {loc[:30]} | {nom[:25]}")

        prompt = PROMPT_ENRICH.format(
            dt=dt, loc=js_escape(loc), nom=js_escape(nom),
            dia=dia, mes=mes, pais=pais, flag=flag, pN=pN
        )

        resp = ask_claude_with_search(client, prompt, max_tokens=2000)
        if resp:
            objs = split_top_level_objects(resp)
            if objs and "to:[{n:'" in objs[0]:
                print(f"    ✓ Cartel encontrado!")
                enriched.append(objs[0])
                time.sleep(2)
                continue

        enriched.append(obj)

    return enriched

def enrich_existing(client, dados_content, max_to_enrich=10):
    """Enriquece eventos existentes no dados.js que não têm toureiros.
    Faz no máximo max_to_enrich por execução para não exceder o tempo do workflow."""
    
    # Encontra o array FES
    m = re.search(r'(?:const|var) FES\s*=\s*\[', dados_content)
    if not m: return dados_content, 0
    arr_open = dados_content.find('[', m.start())
    depth = 0; arr_close = None
    for i in range(arr_open, len(dados_content)):
        c = dados_content[i]
        if c=='[': depth+=1
        elif c==']':
            depth-=1
            if depth==0: arr_close=i; break
    if arr_close is None: return dados_content, 0

    body = dados_content[arr_open+1:arr_close]
    objects = split_top_level_objects(body)

    enriched_count = 0
    new_objects = []

    for obj in objects:
        # Só enriquece se não tiver toureiros E for evento futuro
        if "to:[{n:'" in obj or enriched_count >= max_to_enrich:
            new_objects.append(obj)
            continue

        m_dt = re.search(r"dt:'(\d{4}-\d{2}-\d{2})'", obj)
        if not m_dt or not is_in_window(m_dt.group(1)):
            new_objects.append(obj)
            continue

        m_loc = re.search(r"loc:'([^']+)'", obj)
        m_nom = re.search(r"nom:'([^']+)'", obj)
        m_dia = re.search(r"dia:'([^']+)'", obj)
        m_mes = re.search(r"mes:'([^']+)'", obj)
        m_p   = re.search(r",p:'([^']+)'", obj)
        m_flag= re.search(r"flag:'([^']+)'", obj)
        m_pN  = re.search(r"pN:'([^']+)'", obj)

        dt   = m_dt.group(1)
        loc  = m_loc.group(1) if m_loc else ''
        nom  = m_nom.group(1) if m_nom else ''
        dia  = m_dia.group(1) if m_dia else dt.split('-')[2].lstrip('0')
        mes  = m_mes.group(1) if m_mes else ''
        pais = m_p.group(1) if m_p else 'es'
        flag = m_flag.group(1) if m_flag else '🇪🇸'
        pN   = m_pN.group(1) if m_pN else 'Espanha'

        print(f"  🔎 {dt} | {loc[:30]} | {nom[:25]}")

        prompt = PROMPT_ENRICH.format(
            dt=dt, loc=js_escape(loc), nom=js_escape(nom),
            dia=dia, mes=mes, pais=pais, flag=flag, pN=pN
        )

        resp = ask_claude_with_search(client, prompt, max_tokens=2000)
        if resp:
            objs = split_top_level_objects(resp)
            if objs and "to:[{n:'" in objs[0]:
                # Verifica que tem a data e local correcto
                m_dt2 = re.search(r"dt:'(\d{4}-\d{2}-\d{2})'", objs[0])
                if m_dt2 and m_dt2.group(1) == dt:
                    print(f"    ✓ Cartel encontrado!")
                    new_objects.append(objs[0])
                    enriched_count += 1
                    time.sleep(2)
                    continue

        new_objects.append(obj)

    if enriched_count > 0:
        new_body = '\n' + ',\n'.join(new_objects) + '\n'
        dados_content = dados_content[:arr_open+1] + new_body + dados_content[arr_close:]

    return dados_content, enriched_count


def scrape_agenda_websearch(client, html):
    existing = fes_keys(html)
    new_entries = []

    for descricao, query, pais, flag, pN in PESQUISAS:
        print(f"\n🔍 {descricao}...")

        prompt = PROMPT_TEMPLATE.format(
            descricao=descricao, today=TODAY, horizon=HORIZON,
            pais=pais, flag=flag, pN=pN, query=query
        )

        resp = ask_claude_with_search(client, prompt, max_tokens=4000)
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

            if not is_in_window(dt) or key in existing:
                continue

            new_entries.append(obj)
            existing.add(key)
            m_nom = re.search(r"nom:'([^']*)'", obj)
            m_gan = re.search(r"t:'([^']*)'", obj)
            print(f"  ✓ {dt} | {loc[:35]} | {m_nom.group(1)[:30] if m_nom else ''} | gan:{m_gan.group(1)[:20] if m_gan else ''}")

        time.sleep(3)

    # Fase de enriquecimento: busca cartels completos para eventos sem toureiros
    without_to = [e for e in new_entries if "to:[{n:'" not in e]
    if without_to:
        print(f"\n🔎 Enriquecimento: {len(without_to)} eventos sem toureiros...")
        enriched_without = enrich_entries(client, without_to)
        # Replace in new_entries
        with_to = [e for e in new_entries if "to:[{n:'" in e]
        new_entries = with_to + enriched_without

    return new_entries

# ── TV via web_search ──────────────────────────────────────────────────────────

PROMPT_TV = """Pesquisa na web e lista TODOS os eventos taurinos em televisão a partir de hoje {today} até {horizon}.
Fontes prioritárias: elmuletazo.com, cultoro.es toros television, agendataurina.info televisión.

Uma linha por evento, formato EXACTO (separado por |):
DATA|HORA|CANAL|LOCAL|NOME DO EVENTO|GANADARIA|TOUREIROS (separados por vírgula)

Exemplo:
2026-07-14|18:30|Onetoro|Pamplona (Navarra)|Corrida San Fermín|Jandilla|Juan Ortega,Roca Rey,Tomás Rufo

- DATA: YYYY-MM-DD
- HORA: HH:MM
- CANAL: nome exacto do canal
- Sem cabeçalhos, sem texto extra
- Inclui TODOS os dias de San Fermín, Valencia, Dax, Bayonne, etc."""

def scrape_tv_websearch(client, html):
    existing = tv_keys(html)
    corrections = []
    new_tv = []

    tv_existing_list = re.findall(
        r"dt:'(\d{4}-\d{2}-\d{2})'[^}]*?chan:'([^']*)'[^}]*?loc:'([^']*)'", html
    )

    print(f"\n📺 TV via web_search...")
    prompt = PROMPT_TV.format(today=TODAY, horizon=TODAY + datetime.timedelta(days=60))
    resp = ask_claude_with_search(client, prompt, max_tokens=4000)
    if not resp:
        return [], []

    print(f"  → {len(resp.splitlines())} linhas")

    for line in resp.splitlines():
        parts = line.strip().split('|')
        if len(parts) < 5:
            continue
        try:
            dt_str   = parts[0].strip()
            hora     = parts[1].strip()
            canal    = parts[2].strip()
            local    = parts[3].strip()
            desc     = parts[4].strip() if len(parts) > 4 else ''
            ganadaria= parts[5].strip() if len(parts) > 5 else ''
            toureiros_raw = parts[6].strip() if len(parts) > 6 else ''
            toureiros = [t.strip() for t in toureiros_raw.split(',') if t.strip()]

            d = datetime.date.fromisoformat(dt_str)
            if not (TODAY <= d <= TODAY + datetime.timedelta(days=60)):
                continue

            # Correcções
            for ex_dt, ex_chan, ex_loc in tv_existing_list:
                if ex_dt == dt_str and local[:20] == ex_loc[:20] and canal != ex_chan:
                    corrections.append({'dt':dt_str,'old_chan':ex_chan,'new_chan':canal,'loc':ex_loc})

            key = f"{dt_str}|{canal}|{local[:20]}"
            if key in existing:
                continue

            mes = MES_MAP[d.month]
            dia = str(d.day)
            pais_flag = '🇵🇹' if any(p in canal for p in ['RTP','SIC','TVI']) else '🇪🇸'
            to_arr = ','.join([f"{{n:'{js_escape(t)}',nat:'🇪🇸',r:'MATADOR'}}" for t in toureiros])
            tv_line = (
                f"{{dt:'{dt_str}',dia:'{dia}',mes:'{mes}',"
                f"chan:'{js_escape(canal)}',hora:'{hora}h {pais_flag}',"
                f"loc:'{js_escape(local)}',nom:'{js_escape(desc)}',"
                f"cartel:'{js_escape(desc)}',"
                f"c:{{dh:'{dia} {mes} {d.year}',t:'{js_escape(ganadaria)}',to:[{to_arr}],"
                f"p:'{js_escape(local)}',cap:'A confirmar'}},"
                f"link:'https://elmuletazo.com/agenda-de-toros-en-television/'}}"
            )
            new_tv.append(tv_line)
            existing.add(key)
            print(f"  ✓ TV {dt_str} {canal[:25]} | {local[:25]}" + (f" | {ganadaria[:20]}" if ganadaria else ''))
        except:
            continue

    return corrections, new_tv

# ── Insere FES ─────────────────────────────────────────────────────────────────

def insert_fes(html, entries):
    if not entries: return html, 0
    marker = '];\n\nconst RUA=['
    idx = html.find(marker)
    if idx < 0:
        print("  ⚠ Marcador FES não encontrado!")
        return html, 0
    block = f"\n\n  /* AUTO {TODAY} */\n" + "\n".join(f"  ,{e}" for e in entries) + "\n"
    return html[:idx] + block + html[idx:], len(entries)

# ── Insere TV ──────────────────────────────────────────────────────────────────

def insert_tv(html, tv_entries):
    if not tv_entries: return html, 0
    marker = '];\n\nconst MES_TV'
    idx = html.find(marker)
    if idx < 0:
        marker = '];\n\nconst GANADARIAS'
        idx = html.find(marker)
    if idx < 0:
        print("  ⚠ Marcador TV não encontrado!")
        return html, 0
    block = "\n" + "\n".join(f"  ,{e}" for e in tv_entries) + "\n"
    return html[:idx] + block + html[idx:], len(tv_entries)

# ── Aplica correcções TV ───────────────────────────────────────────────────────

def apply_corrections(html, corrections):
    for corr in corrections:
        old_str = f"chan:'{corr['old_chan']}',hora:"
        new_str = f"chan:'{corr['new_chan']}',hora:"
        lines = html.split('\n')
        new_lines = []
        for line in lines:
            if corr['dt'] in line and corr['old_chan'] in line and corr['loc'][:15] in line:
                line = line.replace(old_str, new_str, 1)
                print(f"  ✅ Corrigido: {corr['dt']} {corr['old_chan']} → {corr['new_chan']}")
            new_lines.append(line)
        html = '\n'.join(new_lines)
    return html

# ── Remove eventos passados e além do horizonte ────────────────────────────────

def prune_past_events(content):
    """Remove eventos passados do conteúdo (funciona com dados.js ou index.html)."""
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

# ── Valida JS ──────────────────────────────────────────────────────────────────

def validate_js(content):
    """Valida sintaxe JS — funciona com dados.js (texto puro) ou index.html."""
    # Se for HTML, extrai o script principal
    if '<script' in content:
        scripts = re.findall(r'<script(?:[^>]*)>(.*?)</script>', content, re.DOTALL)
        js_to_check = max(scripts, key=len) if scripts else ''
    else:
        js_to_check = content
    
    if not js_to_check.strip():
        print("  ⚠ Nenhum JS encontrado"); return False
    tmp = '/tmp/_ctb_validate.js'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(js_to_check)
    try:
        result = subprocess.run(['node','--check',tmp], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return True  # node não disponível, assume válido
    if result.returncode != 0:
        print("  ❌ Erro JS:"); print(result.stderr); return False
    print("  ✅ JS válido"); return True

DADOS_FILE = "dados.js"

def update_version(html):
    hoje = TODAY.strftime("%Y%m%d")
    return re.sub(r'<!DOCTYPE html><!-- CTB-v\d{8}[^>]* -->',
                  f'<!DOCTYPE html><!-- CTB-v{hoje}-AUTO -->', html)

def rebuild_dados_js(dados):
    """Reconstrói o ficheiro dados.js a partir dos arrays de dados."""
    lines = ['// Calendário Taurino — Dados\n// Gerado automaticamente pelo scraper. Não editar.\n']
    for arr_name, arr_body in dados.items():
        lines.append(f'var {arr_name} = [{arr_body}];\n')
    return '\n'.join(lines)

def load_dados():
    """Lê o ficheiro dados.js e extrai os arrays de dados."""
    try:
        with open(DADOS_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
        return content
    except FileNotFoundError:
        print(f"  ⚠ {DADOS_FILE} não encontrado")
        return ""

# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    print(f"\n🐂 CTB Scraper v4 (web_search) — {TODAY}\n{'='*50}")

    if not API_KEY:
        print("❌ ANTHROPIC_API_KEY não definida!"); sys.exit(1)

    # Lê o index.html (estrutura) e dados.js (conteúdo)
    with open(HTML_FILE, 'r', encoding='utf-8') as f:
        html = f.read()

    # O scraper trabalha com dados.js + index.html em conjunto
    # Para deduplicação e inserção, usamos o conteúdo combinado
    dados_content = load_dados()
    combined = html + '\n' + dados_content  # para fes_keys e tv_keys

    client = anthropic.Anthropic(api_key=API_KEY)
    changed_dados = False
    changed_html = False

    # Fase 0: Limpeza de eventos passados no dados.js
    print("\n🧹 FASE 0: Limpeza de eventos passados")
    dados_content, pruned = prune_past_events(dados_content)
    if pruned > 0:
        print(f"  → {pruned} eventos removidos")
        changed_dados = True
    else:
        print("  → Nada a remover")

    # Fase 1: Festejos via web_search
    print("\n📋 FASE 1: Festejos (web_search)")
    new_fes = scrape_agenda_websearch(client, combined)
    if new_fes:
        dados_content, n = insert_fes(dados_content, new_fes)
        print(f"  → {n} eventos FES inseridos")
        changed_dados = True

    # Fase 1b: Enriquecimento de eventos existentes sem toureiros
    print("\n🔎 FASE 1b: Enriquecimento de cartels existentes")
    dados_content, enriched = enrich_existing(client, dados_content)
    if enriched > 0:
        print(f"  → {enriched} eventos enriquecidos com cartel completo")
        changed_dados = True
    else:
        print("  → Nada a enriquecer hoje")

    # Fase 2: TV via web_search
    print("\n📺 FASE 2: Agenda TV (web_search)")
    corrections, new_tv = scrape_tv_websearch(client, combined)
    if corrections:
        dados_content = apply_corrections(dados_content, corrections)
        changed_dados = True
    if new_tv:
        dados_content, n = insert_tv(dados_content, new_tv)
        print(f"  → {n} eventos TV inseridos")
        changed_dados = True

    # Fase 3: Validação do dados.js
    print("\n🔍 FASE 3: Validação JS")
    if not validate_js(html + '\n' + dados_content):
        print("❌ ABORTADO — ficheiro NÃO gravado"); sys.exit(1)

    if changed_dados:
        with open(DADOS_FILE, 'w', encoding='utf-8') as f:
            f.write(dados_content)
        print(f"\n💾 {DADOS_FILE} actualizado")

    if changed_html:
        html = update_version(html)
        with open(HTML_FILE, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"\n💾 {HTML_FILE} actualizado")

    if not changed_dados and not changed_html:
        print("\nℹ Sem alterações")

    print(f"\n✅ Concluído — {TODAY}\n")

if __name__ == "__main__":
    main()
