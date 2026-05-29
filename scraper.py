#!/usr/bin/env python3
"""
Calendário Taurino — Scraper Automático
Corre diariamente via GitHub Actions.
Vai aos sites taurinos, extrai eventos novos, actualiza index.html.
"""

import os, re, json, time, datetime
import requests
from bs4 import BeautifulSoup
import anthropic

# ── Configuração ──────────────────────────────────────────────────────────────
API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
HTML_FILE = "index.html"
TODAY     = datetime.date.today()
YEAR      = TODAY.year

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "pt-PT,pt;q=0.9,es;q=0.8",
}

SITES = [
    {
        "nome": "touradas.pt",
        "url":  "https://www.touradas.pt/agenda",
        "pais": "pt",
    },
    {
        "nome": "portadossustos.com",
        "url":  "https://www.portadossustos.com/agenda/",
        "pais": "pt",
    },
    {
        "nome": "elmuletazo.com",
        "url":  "https://www.elmuletazo.com/agenda-de-toros-en-television/",
        "pais": "es",
        "tv":   True,
    },
    {
        "nome": "mundotoro.com",
        "url":  "https://www.mundotoro.com/agenda-taurina",
        "pais": "es",
    },
    {
        "nome": "cultoro.es",
        "url":  "https://www.cultoro.es/agenda-taurina",
        "pais": "es",
    },
]

MESES = {
    1:"Jan",2:"Feb",3:"Mar",4:"Abr",5:"Mai",6:"Jun",
    7:"Jul",8:"Ago",9:"Set",10:"Out",11:"Nov",12:"Dez",
    "enero":"Jan","febrero":"Feb","marzo":"Mar","abril":"Abr",
    "mayo":"Mai","junio":"Jun","julio":"Jul","agosto":"Ago",
    "septiembre":"Set","octubre":"Out","noviembre":"Nov","diciembre":"Dez",
    "janeiro":"Jan","fevereiro":"Feb","março":"Mar",
    "junho":"Jun","julho":"Jul","setembro":"Set","outubro":"Out",
    "novembro":"Nov","dezembro":"Dez",
}

# ── Funções de apoio ───────────────────────────────────────────────────────────

def fetch(url, timeout=15):
    """Faz GET a uma página e devolve o texto HTML."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  ⚠ Erro ao aceder {url}: {e}")
        return ""

def extrair_texto_util(html, max_chars=12000):
    """Remove scripts/estilos e devolve texto limpo truncado."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script","style","nav","footer","header","aside","form"]):
        tag.decompose()
    texto = soup.get_text(separator="\n", strip=True)
    # Reduz linhas vazias consecutivas
    texto = re.sub(r'\n{3,}', '\n\n', texto)
    return texto[:max_chars]

def eventos_existentes(html):
    """Extrai todos os dt existentes no array FES do index.html."""
    dts = re.findall(r"dt:'(\d{4}-\d{2}-\d{2})'", html)
    locs = re.findall(r"loc:'([^']+)'", html)
    # Chave de dedup: data + primeiras 30 chars da localização
    existentes = set()
    for i, dt in enumerate(dts):
        loc = locs[i][:30] if i < len(locs) else ""
        existentes.add(f"{dt}|{loc}")
    return existentes

def pedir_claude(client, texto_site, nome_site, pais, is_tv=False):
    """Envia o texto raspado ao Claude e pede objectos JS formatados."""
    hoje_str = TODAY.strftime("%Y-%m-%d")
    tv_note  = "IMPORTANTE: marca tv:1 em TODOS os eventos pois é um site de TV." if is_tv else ""

    prompt = f"""Analisa este texto extraído do site taurino "{nome_site}" (país: {pais}).
Hoje é {hoje_str}. Ano em curso: {YEAR}.
{tv_note}

Extrai APENAS eventos que ainda NÃO ocorreram (data >= {hoje_str}).
Para cada evento, devolve um objecto JavaScript EXACTAMENTE neste formato — sem nenhum texto adicional, apenas os objectos separados por vírgula:

{{dt:'YYYY-MM-DD',dtE:'YYYY-MM-DD',dia:'D',mes:'Mmm',p:'{pais}',flag:'🇵🇹',pN:'Portugal',nom:'Nome do evento',loc:'Praça, Cidade, Região',mod:'corrida',top:0,feria:0,tv:0,lat:0,lon:0,bi:'https://url-fonte',c:{{dh:'D Mmm YYYY',t:'Ganadaria',to:[{{n:'Nome Toureiro',nat:'🇵🇹',r:'CAVALEIRO'}}],p:'Praça',cap:'A confirmar'}},no:'Nota curta se relevante',fi:null}}

Regras:
- flag e pN conforme o país (🇵🇹 Portugal, 🇪🇸 Espanha, 🇫🇷 França)
- mod: 'corrida', 'rejones' ou 'misto'
- mes: Jan Feb Mar Abr Mai Jun Jul Ago Set Out Nov Dez
- Se não souberes a lat/lon, usa 0
- tv:1 apenas se for evento transmitido em televisão{' (ou se vier deste site de TV)' if is_tv else ''}
- top:1 apenas se for evento de grande destaque (San Isidro, Feria de Abril, etc.)
- feria:1 se fizer parte de uma feria ou festa popular
- Se não houver toureiros conhecidos, usa: to:[{{n:'A anunciar',nat:'🇵🇹',r:'TOUREIRO'}}]
- Devolve APENAS os objectos JS, sem ```javascript, sem explicações

TEXTO DO SITE:
{texto_site}
"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"  ⚠ Erro Claude API: {e}")
        return ""

def validar_objectos(raw):
    """Verifica se o texto parece conter objectos JS válidos."""
    if not raw:
        return []
    # Divide por padrão de início de objecto
    blocos = re.split(r'\},\s*\{', raw)
    validos = []
    for b in blocos:
        b = b.strip().lstrip(',').strip()
        if not b.startswith('{'):
            b = '{' + b
        if not b.endswith('}'):
            b = b + '}'
        # Verifica campos mínimos obrigatórios
        if "dt:'" in b and "nom:'" in b and "loc:'" in b:
            validos.append(b)
    return validos

def inserir_eventos(html, novos_objectos, existentes):
    """Insere novos eventos no array FES antes do '];'."""
    inseridos = 0
    linhas_novas = []

    for obj in novos_objectos:
        # Extrai dt e loc para verificar duplicados
        m_dt  = re.search(r"dt:'(\d{4}-\d{2}-\d{2})'", obj)
        m_loc = re.search(r"loc:'([^']+)'", obj)
        if not m_dt:
            continue
        dt  = m_dt.group(1)
        loc = m_loc.group(1)[:30] if m_loc else ""
        chave = f"{dt}|{loc}"

        # Ignora eventos passados
        try:
            data_ev = datetime.date.fromisoformat(dt)
            if data_ev < TODAY:
                continue
        except:
            continue

        if chave in existentes:
            print(f"  ↷ Já existe: {dt} | {loc[:40]}")
            continue

        linhas_novas.append(f"  ,{obj}")
        existentes.add(chave)
        inseridos += 1
        print(f"  ✓ Novo evento: {dt} | {loc[:50]}")

    if not linhas_novas:
        return html, 0

    # Encontra o fecho do array FES e insere antes
    # Marca de inserção: comentário especial + "];
    marker = "\n];"
    bloco  = "\n\n  /* ── AUTO-UPDATE " + TODAY.strftime("%Y-%m-%d") + " ── */\n"
    bloco += "\n".join(linhas_novas)

    # Insere antes do último "];" do array FES
    pos = html.rfind("\n];")
    if pos == -1:
        print("⚠ Não encontrei o fecho do array FES!")
        return html, 0

    html = html[:pos] + bloco + html[pos:]
    return html, inseridos

def actualizar_versao(html):
    """Actualiza o comentário de versão no DOCTYPE."""
    hoje = TODAY.strftime("%Y%m%d")
    html = re.sub(
        r'<!DOCTYPE html><!-- CTB-v\d{8}[^>]* -->',
        f'<!DOCTYPE html><!-- CTB-v{hoje}-AUTO -->',
        html
    )
    return html

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"\n🐂 Calendário Taurino — Scraper Automático — {TODAY}\n")

    if not API_KEY:
        print("❌ ANTHROPIC_API_KEY não definida!")
        return

    # Lê o index.html actual
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    existentes = eventos_existentes(html)
    print(f"📋 Eventos existentes no array: {len(existentes)}\n")

    client = anthropic.Anthropic(api_key=API_KEY)
    total_inseridos = 0

    for site in SITES:
        print(f"🌐 A raspar {site['nome']}...")
        raw_html = fetch(site["url"])
        if not raw_html:
            continue

        texto = extrair_texto_util(raw_html)
        if len(texto) < 100:
            print(f"  ⚠ Pouco texto extraído, a saltar.")
            continue

        print(f"  → {len(texto)} chars extraídos, a enviar ao Claude...")
        resposta = pedir_claude(
            client, texto,
            site["nome"], site["pais"],
            site.get("tv", False)
        )

        if not resposta:
            continue

        objectos = validar_objectos(resposta)
        print(f"  → {len(objectos)} eventos identificados pelo Claude")

        html, n = inserir_eventos(html, objectos, existentes)
        total_inseridos += n

        time.sleep(2)  # Respeita rate limits

    print(f"\n✅ Total de novos eventos inseridos: {total_inseridos}")

    if total_inseridos > 0:
        html = actualizar_versao(html)
        with open(HTML_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"💾 {HTML_FILE} actualizado com sucesso.")
    else:
        print("ℹ Nenhum evento novo — ficheiro não alterado.")

if __name__ == "__main__":
    main()
