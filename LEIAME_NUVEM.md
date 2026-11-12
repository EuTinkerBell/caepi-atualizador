# Banco de CAs online com atualização diária (Supabase + GitHub Actions)

O que você vai ter no final:
- Um banco PostgreSQL online (Supabase) com todos os CAs de EPI.
- Uma atualização automática todo dia, rodando nos servidores do GitHub (seu PC pode ficar desligado).
- Uma API pronta para os seus projetos consultarem os CAs de qualquer lugar.

Tudo isso cabe nos planos gratuitos. Tempo estimado: 15 a 20 minutos.

---

## Passo 1: criar o banco no Supabase

1. Entre em https://supabase.com e crie uma conta.
2. **New project**. Escolha um nome (ex.: `caepi`), uma **senha do banco** (anote, você vai precisar dela) e a região **South America (São Paulo)**.
3. Espere o projeto ficar pronto (1 a 2 minutos).

## Passo 2: copiar a conexão do banco

1. No painel do projeto, clique em **Connect** (no topo).
2. Escolha a conexão **Session pooler** (porta 5432). Ela tem este formato:

       postgresql://postgres.xxxxxxxx:[YOUR-PASSWORD]@aws-0-sa-east-1.pooler.supabase.com:5432/postgres

3. Troque `[YOUR-PASSWORD]` pela senha do passo 1.
   Se a senha tiver caracteres especiais, troque cada um pelo código URL (`@` vira `%40`, `#` vira `%23`, `/` vira `%2F`, `:` vira `%3A`).

**Importante:** use o *Session pooler*, não a "Direct connection". A direta só funciona em IPv6 e o GitHub só alcança IPv4.
Se o painel estiver diferente do descrito, procure pela opção "Session pooler".

## Passo 3: colocar os arquivos no GitHub

1. Entre em https://github.com e crie um repositório **privado** (ex.: `caepi-atualizador`).
2. Descompacte o `caepi-nuvem.zip` e envie os arquivos para o repositório (**Add file > Upload files**).
3. A pasta `.github` começa com ponto e fica **oculta** no Windows/Mac. Se ela não subir junto:
   - **Add file > Create new file**
   - no campo do nome digite `.github/workflows/atualizar.yml` (as barras criam as pastas)
   - cole o conteúdo do arquivo `atualizar.yml` e salve (**Commit changes**).

## Passo 4: guardar a senha do banco como segredo

1. No repositório: **Settings > Secrets and variables > Actions > New repository secret**.
2. Nome: `DATABASE_URL`
3. Valor: a conexão do passo 2, já com a senha.

Nunca cole essa conexão dentro dos arquivos do código.

## Passo 5: primeira carga

1. Aba **Actions** > **Atualizar CAEPI** > **Run workflow**.
2. Espere ficar verde (alguns minutos na primeira vez, é a carga completa).
3. No Supabase: **Table Editor** > tabela `ca`. Os CAs estão lá. A tabela `atualizacoes` mostra o histórico de cada execução.

A partir daí roda sozinho duas vezes por dia (22h17 e 01h43, horário de Brasília). A segunda é reserva, porque o GitHub às vezes atrasa ou descarta execuções agendadas. Se nada mudou, ela não altera nada.

---

## Usar o banco nos seus projetos

**Project Settings > API** mostra a *Project URL* (`https://xxxx.supabase.co`) e a chave **anon**.

Consultar um CA:

    GET https://xxxx.supabase.co/rest/v1/ca?ca=eq.12345
    Headers:  apikey: SUA_CHAVE_ANON
              Authorization: Bearer SUA_CHAVE_ANON

Buscar por nome do equipamento (as colunas são as do arquivo oficial, em minúsculas; confira os nomes no Table Editor):

    GET https://xxxx.supabase.co/rest/v1/ca?nomeequipamento=ilike.*luva*&limit=20

A chave **anon** pode ir no seu site ou app: ela só consegue **ler** a tabela `ca` (os dados do CAEPI são públicos) e não enxerga o histórico. Já a chave `service_role` e a `DATABASE_URL` dão acesso total e **nunca** devem aparecer em site, app ou repositório.

Para fechar a leitura pública, mude no `atualizar.yml` a linha de execução para `python caepi_nuvem.py --sem-api-publica`.

---

## Se algo der errado

**O passo "Atualizar banco de CAs" falha com erro de download ou timeout.**
Os servidores do GitHub ficam fora do Brasil e sites do governo às vezes bloqueiam acesso de fora. Se for isso, rode o mesmo script no seu computador, apontando para o mesmo Supabase:

    pip install -r requirements.txt
    set DATABASE_URL=postgresql://...        (Windows; no Linux/Mac use export)
    python caepi_nuvem.py

e agende com o Agendador de Tarefas do Windows, como no `LEIAME.md`.

**Erro de conexão ou senha.** Confira se usou o *Session pooler* e se a senha foi colocada com os caracteres especiais codificados.

**Arquivo novo com poucos CAs.** O script aborta de propósito se o arquivo baixado tiver menos da metade dos CAs que o banco já tem (download cortado). O banco fica como estava e a falha aparece na tabela `atualizacoes`.

**Limites do plano grátis do Supabase.** 500 MB de banco e pausa do projeto após 1 semana sem atividade. As atualizações diárias devem manter o projeto ativo; se ele pausar, é só clicar em *Restore* no painel.

**Agendamento parou de rodar.** O GitHub desativa agendamentos de repositórios *públicos* após 60 dias sem atividade. Use repositório privado e dê uma olhada na aba Actions de vez em quando. A tabela `atualizacoes` mostra quando foi a última execução bem-sucedida.

---

## O que cada arquivo faz

- `caepi_nuvem.py`: baixa a base oficial e atualiza o Supabase (só o que mudou, em uma transação).
- `caepi_atualizador.py`: download e leitura do arquivo oficial. Também funciona sozinho com SQLite local (veja `LEIAME.md`).
- `.github/workflows/atualizar.yml`: o agendamento diário.
- `requirements.txt`: a biblioteca de conexão com o PostgreSQL.
