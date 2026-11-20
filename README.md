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

### Catálogo: `produtos.json`

Os produtos e o estoque de acessos ficam em `produtos.json`, na raiz do projeto (mude com `PRODUCTS_FILE` no `.env`). O bot relê esse arquivo a cada ação, então qualquer mudança vale **na hora**, sem reiniciar. Isso inclui o painel admin, mas também uma edição feita no arquivo à mão.

```json
{
  "proximo_id": {"produto": 2, "acesso": 3},
  "produtos": [
    {
      "id": 1,
      "nome": "Netflix 30 dias",
      "descricao": "Tela exclusiva",
      "preco": "19.90",
      "ativo": true,
      "imagem": "https://i.imgur.com/abc123.jpg",
      "acessos": [
        {"url": "https://site.com", "login": "conta@exemplo.com", "senha": "123", "pedidos": 12, "valor_total": "350.00"}
      ]
    }
  ]
}
```

- `imagem` (opcional) é o **link direto** de uma foto (`https://…/foto.jpg`). Ela é enviada junto da mensagem quando o cliente abre o produto e aparece como miniatura na busca. Pelo painel: produto → **🖼 Imagem**. O bot mostra uma prévia e só salva se o Telegram conseguir abrir o link. Envie `0` para remover. Se o link quebrar depois, o produto aparece só com texto, e o motivo fica no terminal (`[PRODUTO]`).
- `acessos` é o estoque de logins. Cada login tem `pedidos` (quantidade de pedidos) e `valor_total` (soma dos pedidos), que o cliente vê antes de comprar. Na tela do produto, o cliente passa pelos logins com ⬅️ ➡️ ("Login 1 de X"), toca em **✅ Comprar** e recebe exatamente aquele login, que sai da lista.
- Ao adicionar um produto ou acesso à mão, pode omitir `id` e `proximo_id`: o bot preenche sozinho.
- Um acesso escrito em texto puro (`login`/`senha`) é criptografado e trocado por `"dados": "..."` na próxima leitura.
- Se o arquivo ficar com JSON inválido, o painel admin mostra o erro e o bot volta a funcionar assim que você corrigir.
- O arquivo está no `.gitignore`, porque guarda o estoque. Inclua `produtos.json` no backup.

## 7. Comandos

- `/admin` — painel administrativo
- `/saldo ID_TELEGRAM 50.00` — adiciona R$ 50,00
- `/pix` — inicia uma recarga via Pix
- `/cancel` — cancela a operação em andamento

Os comandos `/admin` e `/saldo` são restritos aos administradores. No painel, em **📦 Produtos e estoque**, escolha um produto para:

- **➕ Adicionar acessos**: mande um por linha: `site | login | senha | pedidos | valor total` (o site é opcional, ex.: `joao@gmail.com | senha123 | 12 | 350,00`). Dá pra colar vários de uma vez e continuar mandando. A mensagem com as senhas é apagada do chat depois de salva.
- **➖ Remover 1** / **🧹 Esvaziar estoque**: retira os acessos mais recentes.
- **💵 Preço**, **✏️ Nome**, **📝 Descrição**, **⏸ Pausar/▶️ Ativar vendas** e **🗑 Excluir produto**. As compras já feitas continuam no histórico do cliente.

### Menu inicial

O menu inicial mostra "👋 Seja bem-vindo(a) a maior loja de logins do telegram!", os avisos da loja e a carteira (ID e saldo). O botão **🛒 Logins** abre a vitrine. Com uma imagem configurada, o menu é enviado **como foto, com o texto e os botões logo abaixo** (sem a linha colorida das prévias de link). Se a imagem falhar, o menu aparece só como texto. Em **⚙️ Configurações**:
- **🖼 Imagens das telas**: cada tela pode ter a sua imagem (🏠 Menu principal, 🛒 Logins, 👤 Meu perfil e 💰 Adicionar saldo). Escolha a tela e envie o link direto da imagem. O bot testa o link antes de salvar. Envie `0` para remover. A do menu principal também pode vir de `HOME_IMAGE_URL` no `.env`.
- **📝 Avisos do menu**: envie os avisos, um por linha. Envie `0` para não mostrar nenhum.

### Bônus: boas-vindas e indicação

Os dois bônus são configurados no painel em **⚙️ Configurações (bônus)**. Valem na hora e ficam salvos em `data/store.json` (seção `settings`).

- **🎁 Boas-vindas** (padrão **R$ 10,00**): cada usuário **novo** recebe esse saldo no primeiro acesso. Quem já usava o bot não recebe. Envie `0` ou toque em ⏸ para desativar.
- **🤝 Indicação** (padrão **R$ 5,00** por indicado): cada usuário tem um link `https://t.me/SEU_BOT?start=ID`, que aparece em **👤 Meu perfil** com o botão **📋 Copiar link**. Quem indicou recebe o bônus **uma única vez por indicado**:
  - **assim que ele entra pelo link**, mesmo sem depositar (padrão), ou
  - **só no 1º depósito Pix** do indicado, o que dificulta fraude com contas falsas (troque em "📌 Pagar").

Só conta como indicado quem nunca tinha usado o bot. Ninguém pode indicar a si mesmo, e o indicado não pode trocar de padrinho. O bot avisa quem indicou por mensagem.

### Canal de logs

O bot pode publicar num canal tudo o que acontece na loja, com usuário (nome clicável, @ e ID), valores, saldo antes → depois e horário:

| Evento | O que mostra |
|---|---|
| 👋 Novo usuário | nome, ID, bônus de boas-vindas, saldo atual, quem indicou, total de usuários |
| 🤝 Bônus de indicação | quem indicou, indicado, valor, saldo de quem indicou, nº de indicados |
| 💠 Pix gerado / ⚠️ falha ao gerar | valor, transação, referência, expiração |
| ✅ Depósito confirmado / ❌ Pix expirado | valor, saldo antes → depois, transação, nº de depósitos do usuário |
| 🛒 Nova compra / 🚨 falha na entrega | produto, preço, saldo antes → depois, pedido, estoque restante (avisa quando esgota) |
| 💰 📥 📤 🗂 ⚙️ Ações do admin | saldo adicionado, estoque adicionado/removido, produto criado/editado/pausado/excluído, configurações |

Senhas e logins **nunca** são enviados ao canal.

**Como configurar:**
1. Crie um canal (pode ser privado) e adicione o bot como **administrador** com permissão de **publicar mensagens**.
2. No bot: `/admin` → **⚙️ Configurações** → **📢 Definir canal de logs**. **Encaminhe** qualquer post do canal para o bot ou envie o ID (`-100…`).
3. O bot posta uma mensagem de teste. Se não conseguir, ele mostra o erro e não salva.

Também dá pra definir por `LOG_CHANNEL_ID=-1001234567890` no `.env`, mas o valor do painel tem prioridade. Se o canal ficar indisponível, a loja continua funcionando normalmente; o erro só aparece no terminal.

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
