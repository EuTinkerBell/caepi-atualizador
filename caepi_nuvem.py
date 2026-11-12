#!/usr/bin/env python3
"""
Atualiza o banco de CAs de EPI na nuvem (PostgreSQL, pensado para o Supabase).

Reaproveita o download e a leitura do caepi_atualizador.py. A conexão vem da
variável de ambiente DATABASE_URL, que nunca fica gravada no código.

    export DATABASE_URL="postgresql://usuario:senha@host:5432/postgres"
    python caepi_nuvem.py
    python caepi_nuvem.py --arquivo base.zip      # teste com arquivo local
    python caepi_nuvem.py --sem-api-publica       # banco fechado, sem leitura pública

Na prática: carrega o arquivo oficial numa tabela temporária, compara com a
tabela "ca" por hash de linha e aplica só a diferença, tudo em uma transação.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import caepi_atualizador as base

log = logging.getLogger("caepi")

RESERVADAS = {"_hash", "primeira_vez", "atualizado_em"}
COLUNAS_BUSCA = ("nomeequipamento", "descricaoequipamento", "razaosocial")


def ident(nome):
    return '"%s"' % nome.replace('"', '""')


# ----------------------------------------------------------------------------
# SQL (funções puras: só montam texto)
# ----------------------------------------------------------------------------
def _so_supabase(comandos):
    """Roda os comandos só se os papéis anon/authenticated existirem (Supabase)."""
    return (
        "DO $$ BEGIN IF (SELECT count(*) FROM pg_roles WHERE rolname IN ('anon','authenticated')) = 2 "
        "THEN %s; END IF; END $$" % comandos
    )


def sql_historico():
    return [
        """CREATE TABLE IF NOT EXISTS atualizacoes (
               id BIGSERIAL PRIMARY KEY,
               data_hora TIMESTAMPTZ NOT NULL DEFAULT now(),
               status TEXT NOT NULL,
               total INTEGER, novos INTEGER, alterados INTEGER, removidos INTEGER,
               mensagem TEXT)""",
        # RLS ligado e sem política: a API pública não enxerga nem altera o histórico
        "ALTER TABLE atualizacoes ENABLE ROW LEVEL SECURITY",
        _so_supabase("REVOKE ALL ON atualizacoes FROM anon, authenticated"),
    ]


def sql_ca(colunas_db):
    return (
        [
            'CREATE TABLE IF NOT EXISTS ca ("ca" TEXT PRIMARY KEY, _hash TEXT, '
            "primeira_vez TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now())"
        ]
        + ["ALTER TABLE ca ADD COLUMN IF NOT EXISTS %s TEXT" % ident(c) for c in colunas_db if c != "ca"]
        + ["ALTER TABLE ca ENABLE ROW LEVEL SECURITY"]
    )


def sql_acesso(api_publica):
    if api_publica:
        # leitura pública (os dados do CAEPI são públicos); escrita só por esta conexão
        return [
            'DROP POLICY IF EXISTS "leitura publica" ON ca',
            'CREATE POLICY "leitura publica" ON ca FOR SELECT USING (true)',
            _so_supabase("REVOKE ALL ON ca FROM anon, authenticated; GRANT SELECT ON ca TO anon, authenticated"),
        ]
    return [
        'DROP POLICY IF EXISTS "leitura publica" ON ca',
        _so_supabase("REVOKE ALL ON ca FROM anon, authenticated"),
    ]


def sql_carga(colunas_db):
    cols = ", ".join(ident(c) for c in colunas_db)
    defs = ", ".join(ident(c) + (" TEXT PRIMARY KEY" if c == "ca" else " TEXT") for c in colunas_db)
    sets = ", ".join("%s = EXCLUDED.%s" % (ident(c), ident(c)) for c in colunas_db if c != "ca")
    return {
        "stage": "CREATE TEMP TABLE ca_stage (%s, _hash TEXT) ON COMMIT DROP" % defs,
        "copy": "COPY ca_stage (%s, _hash) FROM STDIN" % cols,
        "conta_novos": "SELECT count(*) FROM ca_stage s WHERE NOT EXISTS (SELECT 1 FROM ca WHERE ca.ca = s.ca)",
        "conta_alterados": "SELECT count(*) FROM ca_stage s JOIN ca ON ca.ca = s.ca "
        "WHERE ca._hash IS DISTINCT FROM s._hash",
        "conta_removidos": "SELECT count(*) FROM ca WHERE NOT EXISTS (SELECT 1 FROM ca_stage s WHERE s.ca = ca.ca)",
        "upsert": "INSERT INTO ca (%s, _hash) SELECT %s, _hash FROM ca_stage "
        "ON CONFLICT (ca) DO UPDATE SET %s, _hash = EXCLUDED._hash, atualizado_em = now() "
        "WHERE ca._hash IS DISTINCT FROM EXCLUDED._hash" % (cols, cols, sets),
        "remove": "DELETE FROM ca WHERE NOT EXISTS (SELECT 1 FROM ca_stage s WHERE s.ca = ca.ca)",
    }


# ----------------------------------------------------------------------------
# Banco
# ----------------------------------------------------------------------------
def conectar(url):
    try:
        import psycopg
    except ImportError:
        raise SystemExit("Falta o pacote psycopg. Rode: pip install -r requirements.txt")
    # prepare_threshold=None: compatível com o pooler do Supabase
    return psycopg.connect(url, connect_timeout=30, prepare_threshold=None)


def _contar(cur, sql):
    cur.execute(sql)
    return int(cur.fetchone()[0])


def atualizar(con, colunas, linhas, api_publica=True):
    colunas_db, novas = base.preparar(colunas, linhas)
    conflito = RESERVADAS & set(colunas_db)
    if conflito:
        raise ValueError("O arquivo tem colunas com nome reservado: %s" % ", ".join(sorted(conflito)))

    cur = con.cursor()
    for s in sql_historico() + sql_ca(colunas_db) + sql_acesso(api_publica):
        cur.execute(s)

    existentes = _contar(cur, "SELECT count(*) FROM ca")
    if existentes and len(novas) < base.FRACAO_MINIMA * existentes:
        raise ValueError(
            "Arquivo novo tem só %d CAs e o banco tem %d. Atualização abortada por segurança."
            % (len(novas), existentes)
        )

    q = sql_carga(colunas_db)
    cur.execute(q["stage"])
    with cur.copy(q["copy"]) as copy:
        for row in novas.values():
            limpa = [v.replace("\x00", "") for v in row]
            copy.write_row(limpa + [base.hash_linha(limpa)])

    n_novos = _contar(cur, q["conta_novos"])
    n_alt = _contar(cur, q["conta_alterados"])
    n_rem = _contar(cur, q["conta_removidos"])
    cur.execute(q["upsert"])
    cur.execute(q["remove"])
    total = _contar(cur, "SELECT count(*) FROM ca")
    cur.execute(
        "INSERT INTO atualizacoes (status, total, novos, alterados, removidos) VALUES ('ok', %s, %s, %s, %s)",
        (total, n_novos, n_alt, n_rem),
    )
    con.commit()  # tudo ou nada
    return colunas_db, total, n_novos, n_alt, n_rem


def criar_indices(con, colunas_db):
    """Índices para busca por texto (LIKE '%luva%'). Falha aqui não afeta os dados."""
    alvo = [c for c in COLUNAS_BUSCA if c in colunas_db]
    if not alvo:
        return
    try:
        cur = con.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        for c in alvo:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS %s ON ca USING gin (%s gin_trgm_ops)" % (ident("ca_%s_trgm" % c), ident(c))
            )
        con.commit()
    except Exception as e:  # noqa: BLE001
        con.rollback()
        log.warning("Não consegui criar os índices de busca (os dados estão corretos): %s", e)


def registrar_falha(url, mensagem):
    try:
        con = conectar(url)
        cur = con.cursor()
        for s in sql_historico():
            cur.execute(s)
        cur.execute("INSERT INTO atualizacoes (status, mensagem) VALUES ('erro', %s)", (mensagem[:500],))
        con.commit()
        con.close()
    except Exception as e:  # noqa: BLE001
        log.warning("Não consegui registrar a falha no histórico: %s", e)


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Atualiza o banco de CAs de EPI na nuvem (PostgreSQL).")
    ap.add_argument("--url", help="URL alternativa do arquivo da base")
    ap.add_argument("--arquivo", help="usa um .zip/.csv local em vez de baixar (para testes)")
    ap.add_argument("--sem-api-publica", action="store_true", help="não libera leitura pública da tabela ca")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    url = os.environ.get("DATABASE_URL")
    if not url:
        log.error("Defina a variável de ambiente DATABASE_URL com a conexão do banco.")
        return 2

    con = None
    try:
        if args.arquivo:
            conteudo = Path(args.arquivo).read_bytes()
        else:
            conteudo = base.baixar([args.url] if args.url else base.URLS_PADRAO)
        colunas, linhas = base.interpretar(base.extrair_texto(conteudo))
        log.info("Arquivo lido: %d linhas, %d colunas", len(linhas), len(colunas))

        con = conectar(url)
        colunas_db, total, novos, alt, rem = atualizar(con, colunas, linhas, not args.sem_api_publica)
        log.info("Pronto. Total: %d | novos: %d | alterados: %d | removidos: %d", total, novos, alt, rem)
        criar_indices(con, colunas_db)
        return 0
    except Exception as e:  # noqa: BLE001
        log.error("ERRO: %s", e)
        if con is not None:
            try:
                con.rollback()
                con.close()
            except Exception:  # noqa: BLE001
                pass
        registrar_falha(url, str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
