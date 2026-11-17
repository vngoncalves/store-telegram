# Telegram Store — arquivo Python único e armazenamento JSON

Loja Telegram com catálogo, saldo em reais, cadastro de produtos e licenças, compra confirmada, entrega automática e histórico. O `/start` abre um novo menu; comandos, botões, respostas e atualizações posteriores editam o painel existente, cujo identificador é preservado no JSON.

Em **Minhas compras**, cada pedido é exibido em um painel com site, login e senha, botões para copiar os dados e acessar o site, e setas para navegar do pedido mais recente aos anteriores.

## Requisitos

- Python 3.10 ou superior
- Windows (a proteção das credenciais usa DPAPI nativo)
- Token de bot do Telegram
- Conexão com a internet
- Dependências listadas em `requirements.txt` para gerar a imagem do QR Pix

Toda a aplicação está no arquivo `store_bot.py`; os dados ficam em JSON. Não precisa instalar MySQL, Workbench ou servidor de banco de dados.

## 1. Instalar

Abra o PowerShell dentro da pasta do projeto:

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

O pacote `qrcode[pil]` gera localmente a imagem PNG a partir do código Pix retornado pela ROASClub.

## 2. Configurar

Copie o modelo e preencha o `.env` (ele fica na mesma pasta do `store_bot.py`):

```powershell
Copy-Item .env.example .env
notepad .env
```

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

- `ADMIN_IDS` aceita mais de um administrador separado por vírgula.
- `STORE_CHANNEL` e `SUPPORT_USERNAME` (sem `@`) aparecem no menu principal.
- `ROASCLUB_CUSTOMER_*` é o pagador enviado à ROASClub em todas as cobranças (veja a seção 6).
- O `.env` e a pasta `data/` **nunca** devem ir para o GitHub: estão no `.gitignore`. Não os coloque dentro de um `.zip` no repositório, porque o `.gitignore` não vale para o conteúdo de arquivos compactados.

## 3. Executar

```powershell
python store_bot.py
```

Na inicialização o bot confere o `.env`, testa o token com o Telegram (`getMe`), remove um webhook antigo, se houver (o modo de recebimento por polling não funciona com webhook ativo), e avisa se a recarga Pix está desativada por falta de configuração. Depois envie `/start` ao bot.

Se aparecer *"terminated by other getUpdates request"*, há outra cópia do bot rodando com o mesmo token: feche-a.

## 3.1 Testes

Os testes simulam o Telegram e a ROASClub e não acessam a internet:

```powershell
python -m unittest discover -s tests -v
```

## 4. Dados e backup

Todos os dados persistentes ficam em `data/store.json`. Se esse arquivo ainda não existir e o antigo `data/store.txt` estiver presente, o bot migra os dados automaticamente na primeira leitura. O TXT original é preservado como backup; depois de confirmar que o JSON foi criado corretamente, você pode guardá-lo em local seguro.

Para fazer backup, copie `data/store.json` com o bot parado. Guarde cópias em local seguro.

## 5. Comandos

- `/admin` — painel administrativo
- `/saldo ID_TELEGRAM 50.00` — adiciona R$ 50,00
- `/pix` — inicia uma recarga via Pix
- `/cancel` — cancela a operação em andamento

Os comandos `/admin` e `/saldo` são restritos aos administradores. No painel, cadastre produtos, adicione licenças e reenvie entregas pendentes.

## 6. Recarga via Pix

O botão **Adicionar saldo** ou o comando `/pix` exige um mínimo de R$ 10,00 e cria uma cobrança em `POST /v1/payment-transaction/create` com Basic Auth e validade de 1 dia (24 horas). A tela mostra a expiração exata informada pela ROASClub. O fluxo pede somente o valor. A ROASClub exige dados de cliente (nome, e-mail, CPF e telefone); o bot envia os dados configurados em `ROASCLUB_CUSTOMER_*` no `.env`. Use dados reais e confirme com a ROASClub que esse cadastro padrão é permitido. O bot **não** gera nomes ou CPFs fictícios. Se esses campos estiverem ausentes ou inválidos, a recarga Pix fica desativada e o motivo aparece no terminal. O bot consulta `GET /v1/payment-transaction/info/{id}` automaticamente a cada 30 segundos e também permite consulta manual pelo botão **Verificar pagamento**. As consultas são espaçadas para respeitar a janela de 2 segundos informada pela API. Quando a ROASClub confirma o status `PAID`, o bot credita o valor integral da cobrança uma única vez.

Quando a cobrança é criada, o bot transforma o código Pix em uma imagem QR e a envia no painel, com os botões, nesta ordem, para copiar o código, verificar o pagamento e cancelar/voltar ao menu. Cancelar fecha a tela e retorna ao menu, mas não anula a cobrança no provedor: ela ainda pode ser paga até expirar e continuará sendo monitorada para que um pagamento confirmado seja creditado. O painel anterior é removido após o envio bem-sucedido. Se a geração ou o envio da imagem falhar, o bot exibe o código Pix em texto para que a cobrança continue utilizável.

Configure `ROASCLUB_PUBLIC_KEY` e `ROASCLUB_SECRET_KEY` no `.env` com as credenciais da página **Credenciais API** da ROASClub. Não use ou copie para o projeto o header Basic Auth de exemplo da documentação. O endpoint não documenta suporte a idempotência; por isso, se a conexão falhar durante a criação e o resultado ficar incerto, o bot não repete o POST automaticamente para evitar gerar cobrança duplicada. A tela informa a referência local para conferência no painel da ROASClub.

Esta integração usa somente cobranças Pix; não realiza cobranças com cartão/boleto nem saques.

## 7. Segurança e limitações

- As credenciais de licença são protegidas com DPAPI do Windows antes de serem gravadas no JSON.
- Para descriptografar, execute o bot com a mesma conta do Windows e no mesmo computador que cadastrou as credenciais.
- Não compartilhe o `store.json`: ele contém dados de usuários, saldos, pedidos, cobranças Pix e credenciais cifradas.
- Este armazenamento foi pensado para uma única instância do bot. Não execute duas cópias simultaneamente apontando para o mesmo JSON.
- O arquivo JSON é um armazenamento local simples, não substitui um banco de dados para lojas de grande volume ou múltiplos processos. Faça backups regulares.
- O usuário precisa abrir o bot e enviar `/start` antes de receber mensagens privadas.
