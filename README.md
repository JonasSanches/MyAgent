# Agente pessoal de desenvolvimento — MVP

Um agente local, pequeno e extensível. Memória, descoberta de habilidades, base de conhecimento verificado e controles de orçamento funcionam sem dependências externas.

## Começar

Requer Python 3.9 ou superior.

```bash
cd personal-dev-agent
python3 -m venv .venv
.venv/bin/pip install --no-use-pep517 -e .
.venv/bin/python -m dev_agent.cli skills
.venv/bin/python -m dev_agent.cli remember "Prefiro soluções pequenas" --tags preferencia
.venv/bin/python -m dev_agent.cli recall pequenas
.venv/bin/python -m dev_agent.cli status
```

Em pastas cujo caminho contém espaços, prefira sempre `.venv/bin/python -m dev_agent...` aos atalhos `dev-agent` gerados pelo `pip`.

## Ciclo de aprendizagem

`solve` sempre procura primeiro soluções do conhecimento permanente que já passaram em testes. Quando encontra uma, cria uma tentativa local sem chamar a API. Quando não encontra, registra a lacuna e pede autorização para consultar o Codex como especialista, com raciocínio de alta qualidade. A resposta, por si só, nunca é aprendida.

```bash
.venv/bin/python -m dev_agent.cli solve "corrigir validação de CPF"
# aplique a proposta ao projeto alvo
.venv/bin/python -m dev_agent.cli complete 1 --test "python3 -m unittest" --workdir ../meu-projeto
```

Após o teste aprovado, o agente mostra um resumo do que aprendeu e pergunta se deve gravar aquilo permanentemente. Responder não deixa a solução validada apenas no histórico; depois, ela pode ser aprovada ou rejeitada explicitamente:

```bash
.venv/bin/python -m dev_agent.cli approve 1
.venv/bin/python -m dev_agent.cli reject 1
```

Se o teste falhar, a tentativa é registrada como falha e não entra na base. Se uma solução já conhecida passar novamente, o agente atualiza sua contagem de sucesso, sem duplicar conhecimento.

## Autonomia e permissões

O agente é autônomo para buscar conhecimento local, registrar histórico e executar o teste que você indicou. Ele pede confirmação apenas para consultar o Codex, tornar conhecimento permanente, modificar arquivos, ações destrutivas ou sistemas externos.

```bash
.venv/bin/python -m dev_agent.cli permissions
.venv/bin/python -m dev_agent.cli history
.venv/bin/python -m dev_agent.cli knowledge
```

## Interface web: Meu Agente

Execute localmente, sem serviços extras:

```bash
.venv/bin/python -m dev_agent.server
```

Abra `http://127.0.0.1:8787`. A interface usa o mesmo banco e as mesmas regras da CLI: mostra histórico e conhecimento, pede autorização para uma lacuna consultar o Codex, permite executar um teste indicado por você e, quando passar, apresenta o resumo antes de aprovar ou rejeitar o conhecimento permanente.

O campo de mensagem aceita texto, links e até quatro prints PNG, JPEG ou WebP de aproximadamente 5 MB cada. Pressione `Enter` para enviar e `Shift+Enter` para uma nova linha; a alça no canto inferior direito permite aumentar a altura do campo. Prints permanecem no navegador até a autorização para consultar o Codex. Quando a mensagem contém um link e você aprova a consulta, o agente pode usar pesquisa web para obter contexto da página.

Para consultar o Codex, defina a chave apenas no seu ambiente (nunca no repositório):

```bash
export OPENAI_API_KEY="sua-chave"
.venv/bin/dev-agent ask "Crie testes para esta função"
```

O agente pede confirmação antes da chamada. Use `--yes` somente em automações conscientes do custo.

## Publicar na internet, de forma privada

O repositório já inclui um `Dockerfile`, pronto para uma hospedagem que aceite contêineres. A instância publicada exige uma senha, cookie seguro HTTPS e um segredo de sessão; ela se recusa a iniciar sem isso. A chave da OpenAI fica apenas nas variáveis secretas do servidor.

Crie um volume persistente na hospedagem e monte-o em `/data`. Depois, cadastre estas variáveis como **secrets** no painel da hospedagem — nunca no repositório nem no navegador:

```bash
OPENAI_API_KEY=...
DEV_AGENT_APP_PASSWORD=uma-senha-longa-e-exclusiva
DEV_AGENT_SESSION_SECRET=um-segredo-aleatorio-com-ao-menos-32-caracteres
DEV_AGENT_COOKIE_SECURE=true
DEV_AGENT_DATABASE_PATH=/data/agent.db
DEV_AGENT_DAILY_REQUEST_LIMIT=5
DEV_AGENT_DAILY_TOKEN_BUDGET=30000
```

Para gerar o segredo de sessão no seu terminal, sem exibi-lo no agente, execute:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

A plataforma deve encaminhar a variável `PORT`; o contêiner já a usa e expõe o endpoint técnico `GET /healthz`. O banco SQLite só persiste se o volume `/data` estiver conectado. Sem esse volume, histórico e conhecimento podem desaparecer a cada novo deploy.

Por segurança, testes executados pela interface só aceitam diretórios dentro de `DEV_AGENT_WORKSPACE` (ou da pasta do agente localmente) e recebem um ambiente sem `OPENAI_API_KEY`, senha ou segredo de sessão. Em uma fase futura, para rodar testes de outro projeto na nuvem, monte esse repositório como um volume separado e defina `DEV_AGENT_WORKSPACE` para ele.

## Controles de custo

- Nenhuma consulta silenciosa à API: em uma lacuna de conhecimento, há confirmação antes do gasto.
- Nunca envie chaves no chat; padrões de chaves da OpenAI são mascarados antes de chegar ao histórico, conhecimento ou mensagens de erro. Se uma chave aparecer em uma tela, revogue-a e gere outra.
- Limite padrão de 5 consultas e 30.000 tokens contabilizados por dia.
- Raciocínio `high`, resposta de até 4.000 tokens e verbosidade `medium`: qualidade não é reduzida para economizar.
- Respostas remotas não são armazenadas pela API (`store: false`).
- Altere os limites pelas variáveis do arquivo `.env.example`; o programa lê variáveis de ambiente, não carrega `.env` automaticamente.

## Roteamento híbrido em nuvem

Depois de verificar o conhecimento permanente, o agente escolhe a próxima camada somente após sua autorização. Tarefas textuais simples usam `gpt-5.6-terra` com raciocínio `high`; tarefas com prints, links, segurança, arquitetura, banco de dados, produção ou uma falha anterior pulam diretamente para o Codex especialista configurado em `DEV_AGENT_MODEL`. Desative a camada de rotina com `DEV_AGENT_ROUTINE_ENABLED=false` se quiser que toda lacuna vá diretamente ao Codex.

Isso mantém o modelo open-source fora do servidor de 512 MB do Render. Um modelo local exigiria uma máquina maior; o roteamento reduz chamadas desnecessárias sem rebaixar tarefas críticas. Toda solução, independentemente da camada, só vira conhecimento após teste aprovado e sua aprovação explícita.

O painel também mede maturidade: taxa de soluções reutilizadas da própria base, consultas externas e testes verificados. Quando houver pelo menos 10 conhecimentos permanentes, 20 tarefas roteadas e 70% de resolução local, ele recomenda revisar o uso do Terra; essa mudança nunca acontece automaticamente.

## Arquitetura

- `core.py`: orquestra memória, habilidades, orçamento e Codex.
- `memory.py`: SQLite local, sem enviar memória para fora até uma consulta confirmada.
- `memories`: histórico e preferências locais; `attempts`: trilha de lacunas, soluções, testes, aprovações e falhas.
- `knowledge`: apenas soluções testadas e aprovadas pelo usuário para reutilização futura.
- `skills.py`: carrega `skills/*/SKILL.md` e inclui no máximo duas habilidades relevantes.
- `codex.py`: cliente mínimo da Responses API, sem SDK ou dependências extras.
- `cli.py`: interface explícita e testável.
- `server.py` e `web/`: servidor local e interface de chat do Meu Agente, sem dependências externas.

## Teste de aceite do MVP

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

O MVP passa quando reutiliza conhecimento sem API, bloqueia aprendizagem após teste falho, aguarda aprovação humana após teste aprovado e executa uma tarefa pequena com teste real — tudo sem chamar a API real.

## Próxima etapa (depois do aceite)

Adicionar uma capacidade por vez: navegador com lista de domínios permitidos, análise de imagem e, por último, delegação para outros agentes. Cada capacidade deve entrar atrás de permissão explícita e teste próprio.
