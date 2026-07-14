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
    m = re.search(r'const FES=\[', html)
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

    ("Espanha - agenda geral",
     f"agenda taurina España corridas {TODAY.strftime('%B %Y')} próximos festejos ganadería toreros site:mundotoro.com OR site:cultoro.es OR site:agendataurina.info",
     "es", "🇪🇸", "Espanha"),

    ("Espanha - San Fermín Pamplona",
     f"San Fermín 2026 Pamplona corridas carteles completos ganadería toreros julio 2026",
     "es", "🇪🇸", "Espanha"),

    ("França - agenda",
     f"agenda taurina France corridas ferias {TODAY.strftime('%B %Y')} cartels complets site:tertulias.fr OR site:vueltaalostoros.fr",
     "fr", "🇫🇷", "França"),

    ("França - Dax Bayonne",
     f"Feria de Dax 2026 Feria Bayonne 2026 agosto carteles toreros ganadería completo",
     "fr", "🇫🇷", "França"),

    ("América - Peru Colombia Mexico",
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

def prune_past_events(html):
    scripts_iter = list(re.finditer(r'<script(?:[^>]*)>(.*?)</script>', html, re.DOTALL))
    if not scripts_iter: return html, 0
    main_match = max(scripts_iter, key=lambda m: len(m.group(1)))
    main = main_match.group(1)
    total_removed = 0

    def prune_array(script, array_name, hor):
        nonlocal total_removed
        start_m = re.search(rf'const {array_name}\s*=\s*\[', script)
        if not start_m: return script
        arr_open = script.find('[', start_m.start())
        depth = 0; arr_close = None
        for i in range(arr_open, len(script)):
            c = script[i]
            if c=='[': depth+=1
            elif c==']':
                depth-=1
                if depth==0: arr_close=i; break
        if arr_close is None: return script
        body = script[arr_open+1:arr_close]
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
        return script[:arr_open+1] + '\n' + ',\n'.join(kept) + '\n' + script[arr_close:]

    main = prune_array(main, 'FES', HORIZON)
    main = prune_array(main, 'RUA', HORIZON)
    # TV_AGENDA: só remove passados, não corta no horizonte (dados são manuais)
    main = prune_array(main, 'TV_AGENDA', TODAY + datetime.timedelta(days=90))

    new_html = html[:main_match.start(1)] + main + html[main_match.end(1):]
    return new_html, total_removed

# ── Valida JS ──────────────────────────────────────────────────────────────────

def validate_js(html):
    scripts = re.findall(r'<script(?:[^>]*)>(.*?)</script>', html, re.DOTALL)
    main_script = max(scripts, key=len) if scripts else ''
    if not main_script.strip():
        print("  ⚠ Nenhum script encontrado"); return False
    tmp = '/tmp/_ctb_validate.js'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(main_script)
    try:
        result = subprocess.run(['node','--check',tmp], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        opens, closes = main_script.count('{'), main_script.count('}')
        if opens != closes:
            print(f"  ❌ Erro sintaxe (fallback): {opens} {{ vs {closes} }}"); return False
        print(f"  ✅ JS OK (fallback)"); return True
    if result.returncode != 0:
        print("  ❌ Erro JS:"); print(result.stderr); return False
    print("  ✅ JS válido (node --check)"); return True

def update_version(html):
    hoje = TODAY.strftime("%Y%m%d")
    return re.sub(r'<!DOCTYPE html><!-- CTB-v\d{8}[^>]* -->',
                  f'<!DOCTYPE html><!-- CTB-v{hoje}-AUTO -->', html)

# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    print(f"\n🐂 CTB Scraper v4 (web_search) — {TODAY}\n{'='*50}")

    if not API_KEY:
        print("❌ ANTHROPIC_API_KEY não definida!"); sys.exit(1)

    with open(HTML_FILE, 'r', encoding='utf-8') as f:
        html = f.read()

    client = anthropic.Anthropic(api_key=API_KEY)
    changed = False

    # Fase 0: Limpeza
    print("\n🧹 FASE 0: Limpeza de eventos passados")
    html, pruned = prune_past_events(html)
    if pruned > 0:
        print(f"  → {pruned} eventos removidos")
        changed = True
    else:
        print("  → Nada a remover")

    # Fase 1: Festejos via web_search
    print("\n📋 FASE 1: Festejos (web_search)")
    new_fes = scrape_agenda_websearch(client, html)
    if new_fes:
        html, n = insert_fes(html, new_fes)
        print(f"  → {n} eventos FES inseridos")
        changed = True

    # Fase 2: TV via web_search
    print("\n📺 FASE 2: Agenda TV (web_search)")
    corrections, new_tv = scrape_tv_websearch(client, html)
    if corrections:
        html = apply_corrections(html, corrections)
        changed = True
    if new_tv:
        html, n = insert_tv(html, new_tv)
        print(f"  → {n} eventos TV inseridos")
        changed = True

    # Fase 3: Validação
    print("\n🔍 FASE 3: Validação JS")
    if not validate_js(html):
        print("❌ ABORTADO — ficheiro NÃO gravado"); sys.exit(1)

    if changed:
        html = update_version(html)
        with open(HTML_FILE, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"\n💾 {HTML_FILE} actualizado")
    else:
        print("\nℹ Sem alterações")

    print(f"\n✅ Concluído — {TODAY}\n")

if __name__ == "__main__":
    main()
