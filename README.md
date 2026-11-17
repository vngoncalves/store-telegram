# Telegram Store

Loja no Telegram com catálogo, saldo em reais, recarga via Pix (ROASClub), cadastro de produtos e licenças, compra confirmada, entrega automática e histórico de compras. Os dados ficam em um arquivo JSON local, sem precisar de banco de dados.

O bot mantém um único painel por conversa: `/start` abre um menu novo, e os botões, comandos e avisos posteriores editam esse mesmo painel. Em **Minhas compras**, cada pedido mostra site, login e senha, com botões para copiar os dados e abrir o site, e setas para navegar entre os pedidos.

## Estrutura do projeto

```text
store-telegram/
├── store_bot.py          # ponto de entrada: python store_bot.py
├── iniciar.bat           # Windows: prepara o ambiente e inicia o bot
├── requirements.txt      # dependências (qrcode[pil])
├── .env.example          # modelo de configuração (copie para .env)
├── loja/                 # código do bot
│   ├── app.py            # inicialização e laço principal
│   ├── config.py         # leitura do .env
│   ├── handlers.py       # comandos, botões, compras e fluxo Pix
│   ├── panels.py         # painel ativo de cada conversa
│   ├── views.py          # textos das telas e QR Code do Pix
│   ├── keyboards.py      # botões inline
│   ├── telegram_api.py   # chamadas à Bot API do Telegram
│   ├── payments.py       # integração com a ROASClub
│   ├── storage.py        # armazenamento em JSON
│   ├── security.py       # criptografia das licenças (DPAPI do Windows)
│   └── validators.py     # validação de valores, IDs e CPF
├── tests/                # testes com Telegram e ROASClub simulados
└── data/                 # criada automaticamente; NÃO vai para o GitHub
```

## Requisitos

- Windows (as credenciais das licenças são protegidas com o DPAPI do Windows)
- Python 3.10 ou superior, marcado no PATH durante a instalação
- Git
- Token de bot do Telegram (@BotFather) e credenciais da ROASClub

## 1. Baixar

```powershell
git clone https://github.com/vngoncalves/store-telegram.git
cd store-telegram
```

## 2. Instalar e iniciar

**Forma rápida:** dê dois cliques em `iniciar.bat` ou rode `.\iniciar.bat` no terminal. Na primeira vez ele:

1. cria o ambiente virtual `.venv`;
2. instala as dependências;
3. cria o `.env` a partir do `.env.example` e o abre no Bloco de Notas.

Preencha o `.env` (seção 3), salve e rode `iniciar.bat` de novo.

**Forma manual (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python store_bot.py
```

Se o PowerShell bloquear o `Activate.ps1`, rode antes `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## 3. Configurar o `.env`

```env
BOT_TOKEN=TOKEN_DO_BOTFATHER
ADMIN_IDS=SEU_ID_TELEGRAM
SUPPORT_USERNAME=usuario_suporte
STORE_CHANNEL=
DATA_FILE=data/store.json
POLLING_TIMEOUT=30
ROASCLUB_PUBLIC_KEY=sua_chave_publica
ROASCLUB_SECRET_KEY=sua_chave_secreta
ROASCLUB_CUSTOMER_NAME=Nome Sobrenome
ROASCLUB_CUSTOMER_EMAIL=financeiro@sualoja.com
ROASCLUB_CUSTOMER_CPF=00000000000
ROASCLUB_CUSTOMER_PHONE=11999998888
```

- `ADMIN_IDS` aceita vários administradores separados por vírgula. Para descobrir seu ID, fale com o @userinfobot.
- `SUPPORT_USERNAME` e `STORE_CHANNEL` são escritos sem `@` e aparecem no menu principal.
- `ROASCLUB_CUSTOMER_*` é o pagador enviado à ROASClub em todas as cobranças (seção 8).
- O `.env` e a pasta `data/` **nunca** devem ir para o GitHub: os dois estão no `.gitignore`. Também não coloque esses arquivos dentro de um `.zip` no repositório, porque o `.gitignore` não protege o conteúdo de arquivos compactados.

## 4. O que acontece ao iniciar

Ao iniciar, o bot:

1. confere o `.env`;
2. testa o token com o Telegram;
3. remove um webhook antigo, se houver (ele impede o recebimento das mensagens);
4. avisa se a recarga Pix está desativada por falta de configuração.

Depois disso, envie `/start` ao bot. Para encerrar, use `Ctrl+C`.

Se aparecer *"terminated by other getUpdates request"*, há outra cópia do bot rodando com o mesmo token. Feche-a.

**Migrando de uma instalação anterior:** copie a pasta `data/` antiga para dentro de `store-telegram/`. Rode o bot com a mesma conta do Windows e no mesmo computador; caso contrário, as licenças já cadastradas não podem ser lidas (seção 9).

## 5. Testes

Os testes simulam o Telegram e a ROASClub e não acessam a internet:

```powershell
python -m unittest discover -s tests -v
```

## 6. Dados e backup

Todos os dados persistentes ficam em `data/store.json`. Se esse arquivo ainda não existir e o antigo `data/store.txt` estiver presente, o bot migra os dados automaticamente na primeira leitura. O TXT original é preservado como backup; depois de confirmar que o JSON foi criado corretamente, você pode guardá-lo em local seguro.

Para fazer backup, copie `data/store.json` com o bot parado. Guarde cópias em local seguro.

## 7. Comandos

- `/admin` — painel administrativo
- `/saldo ID_TELEGRAM 50.00` — adiciona R$ 50,00
- `/pix` — inicia uma recarga via Pix
- `/cancel` — cancela a operação em andamento

Os comandos `/admin` e `/saldo` são restritos aos administradores. No painel, cadastre produtos, adicione licenças e reenvie entregas pendentes.

## 8. Recarga via Pix

O botão **Adicionar saldo** ou o comando `/pix` exige um mínimo de R$ 10,00 e cria uma cobrança em `POST /v1/payment-transaction/create` com Basic Auth e validade de 1 dia (24 horas). A tela mostra a expiração exata informada pela ROASClub. O fluxo pede somente o valor. A ROASClub exige dados de cliente (nome, e-mail, CPF e telefone); o bot envia os dados configurados em `ROASCLUB_CUSTOMER_*` no `.env`. Use dados reais e confirme com a ROASClub que esse cadastro padrão é permitido. O bot **não** gera nomes ou CPFs fictícios. Se esses campos estiverem ausentes ou inválidos, a recarga Pix fica desativada e o motivo aparece no terminal. O bot consulta `GET /v1/payment-transaction/info/{id}` automaticamente a cada 30 segundos e também permite consulta manual pelo botão **Verificar pagamento**. As consultas são espaçadas para respeitar a janela de 2 segundos informada pela API. Quando a ROASClub confirma o status `PAID`, o bot credita o valor integral da cobrança uma única vez.

Quando a cobrança é criada, o bot transforma o código Pix em uma imagem QR e a envia no painel, com os botões, nesta ordem, para copiar o código, verificar o pagamento e cancelar/voltar ao menu. Cancelar fecha a tela e retorna ao menu, mas não anula a cobrança no provedor: ela ainda pode ser paga até expirar e continuará sendo monitorada para que um pagamento confirmado seja creditado. O painel anterior é removido após o envio bem-sucedido. Se a geração ou o envio da imagem falhar, o bot exibe o código Pix em texto para que a cobrança continue utilizável.

Configure `ROASCLUB_PUBLIC_KEY` e `ROASCLUB_SECRET_KEY` no `.env` com as credenciais da página **Credenciais API** da ROASClub. Não use ou copie para o projeto o header Basic Auth de exemplo da documentação. O endpoint não documenta suporte a idempotência; por isso, se a conexão falhar durante a criação e o resultado ficar incerto, o bot não repete o POST automaticamente para evitar gerar cobrança duplicada. A tela informa a referência local para conferência no painel da ROASClub.

Esta integração usa somente cobranças Pix; não realiza cobranças com cartão/boleto nem saques.

## 9. Segurança e limitações

- As credenciais de licença são protegidas com DPAPI do Windows antes de serem gravadas no JSON.
- Para descriptografar, execute o bot com a mesma conta do Windows e no mesmo computador que cadastrou as credenciais.
- Não compartilhe o `store.json`: ele contém dados de usuários, saldos, pedidos, cobranças Pix e credenciais cifradas.
- Este armazenamento foi pensado para uma única instância do bot. Não execute duas cópias simultaneamente apontando para o mesmo JSON.
- O arquivo JSON é um armazenamento local simples, não substitui um banco de dados para lojas de grande volume ou múltiplos processos. Faça backups regulares.
- O usuário precisa abrir o bot e enviar `/start` antes de receber mensagens privadas.
