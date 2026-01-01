# 📺🎛️ Painel de Áudio (TV Box + Termux + Controlador ESP32)

Manual completo do **programa** que roda no **TV Box (Android)** via **Termux** e controla um **ESP32 (Controlador)** pela rede, entregando um painel web para:
- **Volume master do Android (TV Box)**
- **Volumes IR por ambiente** (ex.: Quiosque / Piscina)
- **Relés (R1/R2/...)**
- **Botões de Home / YouTube / Vídeos locais**
- **Página de Configuração + Logs + Sincronização via GitHub**

---

## ⚠️ Avisos importantes (leia antes de tudo)

### ✅ 1) A pasta container TEM que se chamar `programa`
Este projeto foi organizado para você sempre extrair/atualizar mantendo **um nome fixo**:
- ✅ `.../programa/server.py`
- ✅ `.../programa/data/config.json`
- ✅ `.../programa/static/...`

> Se você mudar o nome da pasta principal, você corre risco de quebrar caminhos, scripts e rotinas de atualização.

---

### ✅ 2) A UI pode estar vindo do **GitHub** (e rodando do **cache**)
Se `remote_assets.base_url` estiver preenchido no `data/config.json`, o servidor **prioriza**:
1. **Remoto (GitHub)** → baixa e atualiza cache
2. **Cache local** (`data/remote_cache/`)
3. **Local fixo** (`static/`)

Ou seja: muitas vezes o **HTML/CSS que você está vendo** é o que está dentro de:
- `data/remote_cache/index.html`
- `data/remote_cache/esp.html`
- `data/remote_cache/style.css`
- `data/remote_cache/images/background.jpg`

✅ Isso é intencional: permite “tema remoto” com fallback offline.

---

## 🧠 Visão geral do funcionamento

### 🔹 TV Box / Termux (Servidor)
- Roda `server.py` (Flask)
- Controla o Android via comandos:
  - `termux-volume` (volume master)
  - `am start ...` (Home/YouTube)
  - player de vídeo (abre MP4 local via intent)

### 🔹 ESP32 (Controlador)
- Exponde endpoints HTTP (na LAN) para:
  - estado (`/state`)
  - IR (`/ir`)
  - relés (`/gpio`)

O TV Box “faz proxy” e serve a UI.

---

## 📁 Estrutura do projeto (o que existe dentro de `programa/`)

programa/
├─ server.py
├─ start.sh
├─ stop.sh
├─ data/
│ ├─ config.json
│ └─ remote_cache/
│ ├─ index.html
│ ├─ esp.html
│ ├─ style.css
│ ├─ images/background.jpg
│ └─ *.meta.json
├─ logs/
│ └─ esp.log
└─ static/
├─ index.html
├─ esp.html
├─ style.css
├─ images/background.jpg
└─ videos/
├─ bemvindo.mp4
├─ saudacao.mp4
├─ video1.mp4
└─ *.meta.json

---

## 📦 Onde instalar no Android (pasta no SD / raiz / interno)

### ✅ Recomendado (mais compatível com Android moderno / SD):
Crie/coloque a pasta aqui:

/storage/SEU_SD/Android/media/com.termux/programa


Exemplo real (como aparece em muitos aparelhos):
/storage/6432-3432/Android/media/com.termux/programa


✅ Vantagens:
- Melhor chance de **permissão de escrita** (Scoped Storage)
- Mais estável para atualização por USB/SD
- Evita erros na hora de baixar vídeos (`.tmp` → rename)

---

### ✅ Alternativa (armazenamento interno):
/storage/emulated/0/programa


> Pode funcionar bem, mas depende de permissões e do seu fluxo de cópia.

---

### ⚠️ “Colocar na raiz do SD” (ex.: `/storage/XXXX-XXXX/programa`)
Pode falhar em Android mais novo por permissão.  
Se insistir, faça teste de escrita **antes**:

termux-setup-storage
touch /storage/XXXX-XXXX/programa/teste.txt
Se der erro, use o caminho recomendado em Android/media/com.termux/.

🧰 Instalação no Termux (obrigatório)
1) Termux + Termux:API
Você precisa:

✅ App Termux

✅ App Termux:API

✅ Pacote termux-api dentro do Termux

2) Permitir acesso ao armazenamento

termux-setup-storage
3) Instalar dependências


pkg update -y
pkg install -y python termux-api
pip install --upgrade pip
pip install flask requests

▶️ Como iniciar e acessar
Iniciar
Entre na pasta programa e rode:


cd /storage/XXXX-XXXX/Android/media/com.termux/programa
bash start.sh
start.sh usa a porta 8080 por padrão (via PANEL_PORT).

Acessar pelo navegador (mesma rede Wi-Fi)
Painel principal:

http://IP_DO_TVBOX:8080/

Config / logs / sync:

http://IP_DO_TVBOX:8080/esp

🛑 Como parar
stop.sh existe, mas atenção:

Ele pode conter um caminho hardcoded que não bate com o seu.

Se não parar, use:

pkill -f "python.*server.py"
⚙️ Configuração principal (data/config.json)
Este é o “cérebro” do sistema.

Campos que você mais vai mexer
✅ IP do Controlador (ESP32)
json
Copiar código
"esp_ip": "192.168.0.2"
Você também pode configurar pela página /esp.

✅ UI remota (tema via GitHub)
json
Copiar código
"remote_assets": {
  "base_url": "https://insight-aju.github.io/tvbox-panel-theme",
  "cache_ttl_s": 3600,
  "timeout_s": 3.0
}
Se base_url estiver preenchido → modo remoto ligado

Se ficar vazio → modo remoto desligado (100% local)

✅ Vídeos remotos (opcional)
json
Copiar código
"remote_videos": {
  "base_url": "https://insight-aju.github.io/tvbox-panel-theme/videos",
  "cache_ttl_s": 86400,
  "timeout_s": 20.0
}
✅ Diretório dos vídeos locais (muito importante!)
json
Copiar código
"video_dir": "static/videos"
✅ Você pode trocar para um caminho absoluto, por exemplo:

"video_dir": "/storage/emulated/0/Movies/painel"
O programa sempre procura estes nomes:

bemvindo.mp4

saudacao.mp4

video1.mp4

🎨 Entendendo a “dinâmica remota” (o ponto que mais confunde)
🔥 Regra de ouro
Se remote_assets.base_url estiver ligado, editar static/esp.html pode não mudar nada, porque a página pode estar vindo do:

✅ data/remote_cache/esp.html

✅ Prioridade real do servidor (quando você abre / e /esp)
Sempre nesta ordem:

Remoto (GitHub)

Cache (data/remote_cache/...)

Local (static/...)

👀 Como confirmar “de onde veio”
O servidor envia o header:

X-Asset-Source: remote (acabou de baixar)

X-Asset-Source: cache (está usando cache)

ou cai em static/ (quando não consegue remoto)

Você pode checar pelo navegador (DevTools → Network → Headers) ou via terminal:


curl -I http://IP_DO_TVBOX:8080/ | grep -i x-asset-source
🛠️ Fluxos de edição (qual arquivo editar?)
✅ Cenário A — Quero testar rápido no TV Box (SEM GitHub)
Desligue o remoto:

abra data/config.json

deixe vazio:

remote_assets.base_url: ""

Reinicie o servidor

Agora sim:

edite static/index.html, static/esp.html, static/style.css

✅ Resultado: o que você vê é o que está no static/.

✅ Cenário B — Quero usar o modo “tema remoto” (GitHub)
Edite no repositório/host do tema (GitHub Pages)

No painel /esp, use:

Sincronizar UI (ou /api/sync-remote what=ui)

O servidor baixa e guarda em:

data/remote_cache/

✅ Resultado: o que roda é o cache atualizado.

⚠️ Cenário C — “Emergency patch” direto no cache (não recomendado, mas funciona)
Você pode editar direto:

data/remote_cache/esp.html etc.

Atenção: isso pode ser sobrescrito na próxima sincronização ou quando o TTL vencer.

🔄 Sincronização (UI e Vídeos)
Botões na página /esp
Sincronizar UI (cache de tema)

Sincronizar Vídeos (baixa MP4)

Forçar download (ignora cache/304)

Endpoints (para automação / debug)
✅ Disparar sync
bash
Copiar código
curl -X POST http://IP_DO_TVBOX:8080/api/sync-remote \
  -H "Content-Type: application/json" \
  -d '{"what":"ui","force":true}'
what pode ser:

"ui"

"videos"

"all"

✅ Ver progresso

curl "http://IP_DO_TVBOX:8080/api/sync-progress?sync_id=SEU_ID"
✅ Ver último sync

curl "http://IP_DO_TVBOX:8080/api/sync-last"
🎬 Vídeos: onde ficam e como trocar
Padrão do projeto

static/videos/
  bemvindo.mp4
  saudacao.mp4
  video1.mp4
Se quiser usar outro local (ex.: pasta “raiz” no SD)
Crie a pasta

Coloque os MP4 lá

Ajuste em data/config.json:

"video_dir": "/storage/XXXX-XXXX/Android/media/com.termux/videos_painel"
✅ Pronto: os botões passam a tocar a partir desse diretório.

🌐 Rotas importantes (para entender e depurar)
Páginas
GET / → painel principal (index.html)

GET /esp → configuração e logs (esp.html)

API (principais)
GET /api/vol → volume master (Termux)

POST /api/vol/set → set volume master

POST /api/mute → mute/unmute master

GET /api/status → estado normalizado do ESP (com cache/robustez)

GET /api/state → estado bruto direto do ESP

POST /api/ir → comando IR (proxy pro ESP)

POST /api/gpio → relés (proxy pro ESP)

GET/POST /api/esp-ip → ler/salvar IP do ESP

GET /api/logs → logs do servidor

POST /api/sync-remote → sincronização UI/vídeos

GET /api/sync-progress / GET /api/sync-last

🧯 Troubleshooting (problemas clássicos)
❌ “falha ao renomear .tmp” / “No such file or directory”
Quase sempre é:

diretório de vídeo não existe

ou sem permissão de escrita (SD / Scoped Storage)

✅ Solução:

garanta que a pasta existe

use o caminho recomendado em Android/media/com.termux/

rode termux-setup-storage

✅ “304 / skipped” no sync
Isso é normal: significa “não mudou no servidor”.
Se você quer baixar mesmo assim, use:

Forçar download

❌ Volume master não muda
Falta Termux:API (app) ou pacote termux-api

Teste:

Copiar código
termux-volume
❌ ESP não responde
Teste do TV Box:

Copiar código
curl http://IP_DO_ESP/state
Se falhar:

IP errado

ESP offline

Wi-Fi diferente

🔐 Segurança
Este painel não tem autenticação.
✅ Use somente em rede local (LAN).
⚠️ Não exponha a porta 8080 para a internet.

✅ Checklist de operação (rápido)
 Pasta principal chama programa

 Rodou termux-setup-storage

 Termux:API instalado + pkg install termux-api

 Configurou esp_ip (pela /esp ou no config.json)

 Entendeu se a UI está vindo de static/ ou data/remote_cache/

 Vídeos estão em video_dir com nomes corretos

📌 Dica final (para não sofrer com “qual arquivo está rodando?”)
Quando estiver mexendo em HTML/CSS:

Se você quer editar LOCAL:
✅ desligue remote_assets.base_url

Se você quer editar TEMA REMOTO:
✅ edite o GitHub → sincronize → o que roda é data/remote_cache/

Pronto. Esse é o coração do “modo remoto”.


#### PARTE DOIS ####

Este projeto é um **servidor Flask** que roda no **TV Box/Android (Termux)** e controla um **ESP32 (Controlador)** pela rede. Ele entrega **duas páginas web**:

* **`/`** → painel principal (**volume master**, **volumes por ambiente via IR**, **relés**, **atalhos YouTube/vídeos**).
* **`/esp`** → configurações/diagnóstico (**definir IP do Controlador**, **ver feedback**, **logs**, **atualizar via GitHub**).

> ### ⚠️ Importante
> A pasta container do projeto deve se chamar **`programa`**. **Não renomeie.**

---

## ## ✅ O que o sistema faz

### ### 1) No TV Box (Termux)

* Ajusta **volume master** do Android via `termux-volume`.
* Aciona **HOME** e abre **YouTube** via `am start`.
* Reproduz **vídeos MP4 locais** (bem-vindo/saudação/programados) no player padrão do Android.

### ### 2) No Controlador (ESP32)

* Lê estado pelo endpoint **`/state`** (JSON).
* Envia comandos:

  * **IR** via **`/ir`**
  * **Relés** via **`/gpio`**
  * *(Opcional)* Wi-Fi via **`/wifi`**
  * *(Opcional)* Push de estado do ESP para o servidor via **`/api/esp-state-sink`**

---

## ## 📁 Estrutura de pastas

Dentro da pasta **`programa/`**:

* `server.py` → servidor Flask (**backend**)
* `static/` → UI local (**fallback** quando não há internet ou quando o remoto falha)

  * `index.html` → painel principal
  * `esp.html` → página de config/logs/sync
  * `style.css`, `images/background.jpg`
  * `videos/` → vídeos locais MP4
* `data/config.json` → configurações (**IP do ESP**, **URLs remotas**, etc.)
* `data/remote_cache/` → cache de UI remota (**quando habilitado**)
* `logs/esp.log` → log do servidor
* `start.sh` / `stop.sh` → scripts simples de iniciar/parar

---

## ## 🧩 Requisitos

No **TV Box/Android**:

* **Termux** instalado
* **Termux:API** (app) instalado **e** o pacote `termux-api` dentro do Termux
* **Python** + libs do servidor

---

## ## ⚡ Instalação rápida (Termux)

### ### 1) Dar acesso ao armazenamento

termux-setup-storage
### 2) Instalar dependências

pkg update -y
pkg install -y python termux-api
pip install --upgrade pip
pip install flask requests
### 3) Colocar a pasta programa/ em um local fixo (recomendado)
Recomendado para este projeto:


/storage/XXXX-XXXX/Android/media/com.termux/programa/
Dica: o caminho exato muda conforme o cartão/armazenamento, mas a pasta final deve ser .../com.termux/programa/.

## ▶️ Como iniciar
Entre na pasta e rode:


cd /storage/XXXX-XXXX/Android/media/com.termux/programa
bash start.sh
Por padrão o servidor sobe na porta 8080 (pode mudar com PANEL_PORT).

## 🌐 Como acessar as páginas
No celular/PC na mesma rede Wi-Fi, abra:

Painel principal:
http://IP_DO_TVBOX:8080/

Config/Logs/Sincronização:
http://IP_DO_TVBOX:8080/esp

## 🔧 Configurar o IP do Controlador (ESP32)
Abra http://IP_DO_TVBOX:8080/esp

Em “Definir IP do Controlador”, informe o IP (ex.: 192.168.0.150)

Salve

Isso grava em data/config.json (chave esp_ip).

## 🎬 Vídeos locais (boas-vindas / saudação / programados)
Os vídeos ficam em:

static/videos/bemvindo.mp4

static/videos/saudacao.mp4

static/videos/video1.mp4

Você pode substituir esses arquivos mantendo os nomes.

## 🔄 Atualizações via GitHub (UI e Vídeos)
A página /esp tem um card “Atualizações (GitHub)” com:

Sincronizar UI

Sincronizar Vídeos

Checkbox Forçar download

### Como funciona
UI remota (tema): o servidor tenta servir primeiro do remoto → cache → local (static/).

Vídeos remotos: só baixa quando você manda sincronizar (não baixa no “play”).

As URLs ficam em data/config.json:

"remote_assets": {
  "base_url": "https://insight-aju.github.io/tvbox-panel-theme",
  "cache_ttl_s": 3600,
  "timeout_s": 3.0
},
"remote_videos": {
  "base_url": "https://insight-aju.github.io/tvbox-panel-theme/videos",
  "cache_ttl_s": 86400,
  "timeout_s": 20.0
}
### Desabilitar “remoto” (usar só o local)
Deixe base_url vazio ("") em remote_assets e/ou remote_videos.

## 🪵 Logs
Arquivo: logs/esp.log

Pela página: /esp → Logs do servidor

## 🧪 Endpoints principais (para debug)
### Servidor (TV Box)
GET /api/vol → volume master atual

POST /api/vol/set → ajusta volume master

POST /api/mute → mute/unmute master

GET /api/status → estado normalizado do ESP (cacheado)

POST /api/ir → envia comando IR (proxy para o ESP)

POST /api/gpio → aciona relé (proxy para o ESP)

POST /api/youtube → abre YouTube

POST /api/welcome → toca bemvindo.mp4

POST /api/playvideo → toca vídeo por chave (welcome, saudacao, video1)

POST /api/home → volta para HOME

POST /api/startshow → liga R1/R2 + bemvindo + abre YouTube após delay

POST /api/stopshow → desliga R1/R2 + HOME

POST /api/sync-remote → sincroniza UI/Vídeos (async com sync_id)

GET /api/sync-progress?sync_id=... → progresso da sincronização

GET /api/logs → últimas linhas do log

GET/POST /api/esp-ip → ler/salvar IP do ESP no config

### Controlador (ESP32) esperado
GET /state → JSON com estado (gpio/volumes etc.)

POST /ir → {device, command}

POST /gpio → {pin, state}

## 🧯 Solução de problemas (rápido)
Volume master não funciona: confirme Termux:API instalado + pkg install termux-api.

Botões do ESP não funcionam: verifique o IP em /esp e se o ESP responde http://IP_DO_ESP/state.

Sync de vídeos falha com .tmp:

confira se static/videos/ existe e é gravável

rode termux-setup-storage

tente novamente com Forçar download

UI remota não atualiza: sem internet, o sistema cai automaticamente no local (static/).

## 🔐 Segurança
Não há autenticação. Use em rede local (LAN) e evite expor a porta 8080 para a internet.