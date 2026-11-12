# Banco de dados de CA (EPI) com atualização diária

Baixa a base oficial do CAEPI (Ministério do Trabalho e Emprego), guarda tudo em um
arquivo SQLite (`caepi.db`) e, a cada execução, aplica só o que mudou: CAs novos,
alterados e removidos.

## 1. Requisitos

- Python 3.8 ou mais novo (https://www.python.org/downloads/). No Windows, marque
  "Add Python to PATH" na instalação.
- Nada mais para instalar.

## 2. Primeira execução

Coloque `caepi_atualizador.py` em uma pasta (ex.: `C:\caepi`) e rode:

    python caepi_atualizador.py --db C:\caepi\caepi.db

Na primeira vez ele baixa a base inteira (~120 mil CAs) e cria o banco.
Se aparecer erro de colunas, o próprio script lista as colunas do arquivo oficial.

## 3. Consultar um CA

    python caepi_atualizador.py --db C:\caepi\caepi.db --consultar 12345

Para consultas mais livres, abra o `caepi.db` no DB Browser for SQLite
(https://sqlitebrowser.org, gratuito) e use a tabela `ca`:

    SELECT * FROM ca WHERE nomeequipamento LIKE '%luva%';

## 4. Atualizar todo dia automaticamente

### Windows (Agendador de Tarefas)

No Prompt de Comando:

    schtasks /create /tn "Atualizar CAEPI" /sc daily /st 22:00 /tr "python C:\caepi\caepi_atualizador.py --db C:\caepi\caepi.db"

O computador precisa estar ligado no horário. Se estiver desligado, abra o
Agendador de Tarefas > "Atualizar CAEPI" > Propriedades > Configurações e marque
"Executar a tarefa assim que possível após uma inicialização agendada ser perdida".

### Linux / Mac (cron)

    crontab -e

e adicione a linha:

    0 22 * * * /usr/bin/python3 /caminho/caepi_atualizador.py --db /caminho/caepi.db

## 5. O que fica registrado

- Tabela `ca`: todos os CAs. Colunas extras: `primeira_vez` (quando o CA entrou no
  banco) e `atualizado_em` (última mudança).
- Tabela `atualizacoes`: histórico de cada execução (data, total, novos, alterados,
  removidos, erros).
- Arquivo `caepi.log` ao lado do banco, com o detalhe de cada execução.

## 6. Segurança

- A atualização é uma transação única: se der erro no meio, o banco não fica pela metade.
- Se o arquivo baixado vier com menos da metade dos CAs que o banco já tem (download
  cortado, arquivo errado), a atualização é abortada e o banco fica como estava.
- Se o download falhar, ele tenta 3 vezes em cada endereço (site do gov.br e FTP).
