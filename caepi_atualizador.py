#!/usr/bin/env python3
"""
Banco de dados de CAs (Certificado de Aprovação de EPI) com atualização diária.

Baixa a base oficial do CAEPI (Ministério do Trabalho e Emprego), compara com o
banco SQLite local e aplica só o que mudou: CAs novos, CAs alterados e CAs
removidos. Cada execução fica registrada na tabela "atualizacoes".

Só usa a biblioteca padrão do Python (3.8+). Nada para instalar.

Uso:
    python caepi_atualizador.py                    # baixa e atualiza caepi.db
    python caepi_atualizador.py --db meu_banco.db  # outro arquivo de banco
    python caepi_atualizador.py --arquivo base.zip # usa um arquivo local (teste)
    python caepi_atualizador.py --consultar 12345  # mostra um CA do banco
"""

import argparse
import csv
import hashlib
import io
import logging
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

URLS_PADRAO = [
    "https://www.gov.br/trabalho-e-emprego/pt-br/assuntos/inspecao-do-trabalho/"
    "seguranca-e-saude-no-trabalho/equipamentos-de-protecao-individual-epi/tgg_export_caepi.zip",
    "ftp://ftp.mtps.gov.br/portal/fiscalizacao/seguranca-e-saude-no-trabalho/caepi/tgg_export_caepi.zip",
]

# Nomes (já normalizados) que podem identificar a coluna do número do CA.
CANDIDATOS_CHAVE = [
    "nrregistroca", "registroca", "ca", "nrca", "numeroca", "numero_ca", "nr_ca", "registro_ca", "nr_registro_ca",
]
DELIMITADORES = ";|\t,"  # o arquivo oficial atual usa ponto e vírgula; o antigo usava barra vertical


def e_chave(nome):
    """True se o nome (já normalizado) parece ser o da coluna do número do CA."""
    return nome in CANDIDATOS_CHAVE or nome.endswith("registroca") or nome.endswith("registro_ca")

# Se o arquivo novo tiver menos que esta fração dos registros atuais, a
# atualização é abortada (protege contra download cortado ou arquivo errado).
FRACAO_MINIMA = 0.5

log = logging.getLogger("caepi")


# ----------------------------------------------------------------------------
# Download e leitura
# ----------------------------------------------------------------------------
def baixar(urls, tentativas=3):
    ultimo_erro = None
    for url in urls:
        for n in range(1, tentativas + 1):
            try:
                log.info("Baixando %s (tentativa %d/%d)", url, n, tentativas)
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (caepi-atualizador)"})
                with urllib.request.urlopen(req, timeout=180) as resp:
                    dados = resp.read()
                if len(dados) < 1000:
                    raise ValueError("arquivo baixado pequeno demais (%d bytes)" % len(dados))
                log.info("Download concluído: %.1f MB", len(dados) / 1e6)
                return dados
            except Exception as e:  # noqa: BLE001
                ultimo_erro = e
                log.warning("Falhou: %s", e)
                time.sleep(5 * n)
    raise RuntimeError("Não consegui baixar a base em nenhuma das URLs: %s" % ultimo_erro)


def extrair_texto(conteudo):
    """Recebe os bytes (zip ou csv/txt puro) e devolve o texto do arquivo de dados."""
    if zipfile.is_zipfile(io.BytesIO(conteudo)):
        with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
            nomes = [n for n in z.namelist() if not n.endswith("/")]
            if not nomes:
                raise ValueError("O .zip está vazio")
            tamanhos = {i.filename: i.file_size for i in z.infolist() if not i.filename.endswith("/")}
            log.info("Arquivos no zip: %s", ", ".join("%s (%d bytes)" % (n, t) for n, t in tamanhos.items()))
            nome = max(nomes, key=lambda n: tamanhos[n])  # a base de dados é o maior arquivo
            log.info("Lendo '%s' de dentro do zip", nome)
            conteudo = z.read(nome)
    if conteudo.startswith((b"\xff\xfe", b"\xfe\xff")):
        log.info("Codificação detectada: utf-16")
        return conteudo.decode("utf-16")
    for enc in ("utf-8-sig", "cp1252", "latin-1"):  # o tutorial oficial indica Windows-1252
        try:
            texto = conteudo.decode(enc)
            log.info("Codificação detectada: %s", enc)
            return texto
        except UnicodeDecodeError:
            continue
    raise ValueError("Não consegui decodificar o arquivo")


def normalizar(nome):
    nome = unicodedata.normalize("NFKD", nome)
    nome = "".join(c for c in nome if not unicodedata.combining(c))
    return re.sub(r"[^0-9a-zA-Z]+", "_", nome).strip("_").lower()


def _achar_cabecalho(linhas):
    """Procura o cabeçalho nas primeiras linhas. Devolve (indice_da_linha, separador)."""
    melhor, vistas = None, 0  # melhor = (n_campos, indice, separador)
    for i, linha in enumerate(linhas[:60]):
        if not linha.strip():
            continue
        vistas += 1
        for d in DELIMITADORES:
            campos = linha.split(d)
            if len(campos) < 2:
                continue
            if any(e_chave(normalizar(c)) for c in campos):
                return i, d
            if vistas <= 5 and (melhor is None or len(campos) > melhor[0]):
                melhor = (len(campos), i, d)
    if melhor and melhor[0] >= 3:
        log.warning("Não achei coluna de CA no cabeçalho; usando a linha %d como cabeçalho", melhor[1] + 1)
        return melhor[1], melhor[2]
    amostra = [repr(l[:200]) for l in linhas if l.strip()][:3]
    raise ValueError(
        "Não identifiquei o cabeçalho nem o separador de colunas. Primeiras linhas do arquivo: " + " || ".join(amostra)
    )


def interpretar(texto):
    """Devolve (colunas_normalizadas, lista_de_linhas)."""
    linhas_texto = texto.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not any(l.strip() for l in linhas_texto):
        raise ValueError("Arquivo sem conteúdo")

    i0, delim = _achar_cabecalho(linhas_texto)
    if i0:
        log.info("Ignorando %d linha(s) antes do cabeçalho", i0)
    resto = linhas_texto[i0:]

    # Campos entre aspas (padrão do Excel) ou sem aspas (formato antigo do CAEPI)?
    amostra = [l for l in resto[1:41] if l.strip()]
    entre_aspas = bool(amostra) and sum(1 for l in amostra if l.startswith('"') or (delim + '"') in l) >= len(amostra) / 2
    if entre_aspas:
        log.info("Campos entre aspas detectados")
        leitor = csv.reader(io.StringIO("\n".join(resto), newline=""), delimiter=delim)
    else:
        leitor = csv.reader(resto, delimiter=delim, quoting=csv.QUOTE_NONE)
    cabecalho = next(leitor)

    colunas, usados = [], set()
    for i, c in enumerate(cabecalho):
        nome = normalizar(c) or "coluna_%d" % (i + 1)
        base, k = nome, 2
        while nome in usados:
            nome = "%s_%d" % (base, k)
            k += 1
        usados.add(nome)
        colunas.append(nome)
    n = len(colunas)

    linhas, pendente, malformadas = [], None, 0
    for row in leitor:
        if not row or (len(row) == 1 and not row[0].strip()):
            continue
        if pendente is not None:
            # campo com quebra de linha: junta a continuação na linha anterior
            row = pendente[:-1] + [pendente[-1] + " " + row[0]] + row[1:]
            pendente = None
        if len(row) < n:
            pendente = row
            continue
        if len(row) > n:
            # sobra de colunas: normalmente é só um separador no final
            if any(x.strip() for x in row[n:]):
                malformadas += 1
                continue
            row = row[:n]
        linhas.append([x.replace("\n", " ").strip() for x in row])
    if pendente is not None:
        malformadas += 1
    if malformadas:
        log.warning("%d linha(s) fora do padrão foram ignoradas", malformadas)
    log.info("Colunas encontradas: %s", ", ".join(colunas))
    return colunas, linhas


def achar_chave(colunas):
    for i, c in enumerate(colunas):
        if e_chave(c):
            return i
    raise ValueError("Não achei a coluna do número do CA. Colunas do arquivo: %s" % ", ".join(colunas))


def preparar(colunas, linhas):
    """Devolve (colunas_db, novas): colunas com a chave renomeada para 'ca' e um
    dict {numero_do_ca: linha} sem duplicidades. Compartilhado com caepi_nuvem.py."""
    idx_chave = achar_chave(colunas)
    colunas_db = ["ca" if i == idx_chave else c for i, c in enumerate(colunas)]
    if len(set(colunas_db)) != len(colunas_db):
        raise ValueError("Colunas repetidas após renomear a chave: %s" % ", ".join(colunas_db))
    novas, duplicadas = {}, 0
    for row in linhas:
        chave = row[idx_chave]
        if not chave:
            continue
        if chave in novas:
            duplicadas += 1
        novas[chave] = row  # em duplicidade, vale a última linha
    if duplicadas:
        log.warning("%d CA(s) apareceram mais de uma vez no arquivo (mantida a última linha)", duplicadas)
    if not novas:
        raise ValueError("O arquivo não tem nenhum CA válido")
    return colunas_db, novas


def hash_linha(row):
    return hashlib.sha1("\x1f".join(row).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# Banco
# ----------------------------------------------------------------------------
def abrir_banco(caminho):
    con = sqlite3.connect(caminho)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """CREATE TABLE IF NOT EXISTS atualizacoes (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               data_hora TEXT NOT NULL,
               status TEXT NOT NULL,
               total INTEGER, novos INTEGER, alterados INTEGER, removidos INTEGER,
               mensagem TEXT)"""
    )
    return con


def registrar(con, status, total=None, novos=None, alterados=None, removidos=None, mensagem=None):
    con.execute(
        "INSERT INTO atualizacoes (data_hora, status, total, novos, alterados, removidos, mensagem) "
        "VALUES (?,?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), status, total, novos, alterados, removidos, mensagem),
    )
    con.commit()


def garantir_tabela(con, colunas_db):
    existe = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ca'").fetchone()
    if not existe:
        defs = ", ".join('"%s" TEXT' % c for c in colunas_db if c != "ca")
        con.execute(
            'CREATE TABLE ca ("ca" TEXT PRIMARY KEY, %s, _hash TEXT, primeira_vez TEXT, atualizado_em TEXT)' % defs
        )
        return
    atuais = {r[1] for r in con.execute("PRAGMA table_info(ca)")}
    for c in colunas_db:
        if c not in atuais:
            log.info("Nova coluna no layout oficial: %s", c)
            con.execute('ALTER TABLE ca ADD COLUMN "%s" TEXT' % c)


def atualizar_banco(con, colunas, linhas):
    colunas_db, novas = preparar(colunas, linhas)

    garantir_tabela(con, colunas_db)
    existentes = dict(con.execute("SELECT ca, _hash FROM ca"))

    if existentes and len(novas) < FRACAO_MINIMA * len(existentes):
        raise ValueError(
            "Arquivo novo tem só %d CAs e o banco tem %d. Atualização abortada por segurança."
            % (len(novas), len(existentes))
        )

    agora = datetime.now().isoformat(timespec="seconds")
    para_gravar, n_novos, n_alt = [], 0, 0
    for chave, row in novas.items():
        h = hash_linha(row)
        if chave not in existentes:
            n_novos += 1
        elif existentes[chave] != h:
            n_alt += 1
        else:
            continue
        para_gravar.append(tuple(row) + (h, agora, agora))
    removidos = [(k,) for k in existentes if k not in novas]

    nomes = ", ".join('"%s"' % c for c in colunas_db)
    marcas = ", ".join("?" for _ in colunas_db)
    sql = (
        'INSERT INTO ca (%s, _hash, primeira_vez, atualizado_em) VALUES (%s, ?, ?, ?) '
        'ON CONFLICT(ca) DO UPDATE SET %s, _hash=excluded._hash, atualizado_em=excluded.atualizado_em'
        % (nomes, marcas, ", ".join('"%s"=excluded."%s"' % (c, c) for c in colunas_db if c != "ca"))
    )
    with con:  # transação única: ou grava tudo, ou nada
        con.executemany(sql, para_gravar)
        con.executemany("DELETE FROM ca WHERE ca = ?", removidos)
    return len(novas), n_novos, n_alt, len(removidos)


def consultar(con, numero):
    con.row_factory = sqlite3.Row
    try:
        r = con.execute("SELECT * FROM ca WHERE ca = ?", (numero,)).fetchone()
    except sqlite3.OperationalError:
        print("O banco ainda não tem dados. Rode a atualização primeiro.")
        return 1
    if not r:
        print("CA %s não encontrado." % numero)
        return 1
    for k in r.keys():
        if not k.startswith("_"):
            print("%-28s %s" % (k, r[k]))
    return 0


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Atualiza o banco de dados de CAs de EPI (CAEPI).")
    ap.add_argument("--db", default="caepi.db", help="arquivo do banco SQLite (padrão: caepi.db)")
    ap.add_argument("--url", help="URL alternativa do arquivo da base")
    ap.add_argument("--arquivo", help="usa um .zip/.csv local em vez de baixar (para testes)")
    ap.add_argument("--consultar", metavar="CA", help="mostra os dados de um CA e sai")
    args = ap.parse_args()

    db = Path(args.db)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(db.with_suffix(".log"), encoding="utf-8")],
    )
    con = abrir_banco(str(db))

    if args.consultar:
        return consultar(con, args.consultar)

    try:
        if args.arquivo:
            conteudo = Path(args.arquivo).read_bytes()
        else:
            conteudo = baixar([args.url] if args.url else URLS_PADRAO)
        colunas, linhas = interpretar(extrair_texto(conteudo))
        log.info("Arquivo lido: %d linhas, %d colunas", len(linhas), len(colunas))
        total, novos, alt, rem = atualizar_banco(con, colunas, linhas)
        registrar(con, "ok", total, novos, alt, rem)
        log.info("Pronto. Total: %d | novos: %d | alterados: %d | removidos: %d", total, novos, alt, rem)
        return 0
    except Exception as e:  # noqa: BLE001
        log.error("ERRO: %s", e)
        registrar(con, "erro", mensagem=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
