import os, json, subprocess, time, logging, hashlib, mimetypes, shlex, shutil
from collections import deque
from flask import Flask, jsonify, request, send_from_directory, Response, abort, g
import requests
import threading, uuid

"""
Servidor do Painel (Android / Termux + ESP32) - LAS CABANAS DEL CONDE

Arquitetura geral:

- FRONTEND (arquivos na pasta static/)
  - index.html
      - Painel principal:
          - Card "Aplicativos" (YouTube, Bem-vindo, Retorno, Iniciar, Encerrar)
          - Card "Principal" (volume master / stream music/media)
          - Card "Controles por ambientes" (Quiosque / Piscina via IR)
          - Card "Controle de acionamentos" (relés R1..R4)
      - Usa principalmente:
          - GET  /api/vol
          - POST /api/vol/set
          - POST /api/mute
          - GET  /api/status
          - POST /api/ir
          - POST /api/gpio
          - POST /api/youtube
          - POST /api/welcome
          - POST /api/home
          - POST /api/startshow
          - POST /api/stopshow

  - esp.html
      - Pagina de configuração / diagnóstico:
          - Card "Definir IP do controle"
          - Card "Feedback do ESP32"
          - Card "Logs do servidor"
      - Usa principalmente:
          - GET/POST /api/esp-ip
          - GET      /api/status
          - GET      /api/logs

- BACKEND (este arquivo)
  - Integra Termux (termux-volume, am start, sh)
  - Integra ESP32 (poll de estado, comandos IR/GPIO/Wi-Fi)
  - Mantém cache e logs em disco.
"""

# ============== BINÁRIOS EXTERNOS (ANDROID / TERMUX) ==============
BIN = {
    "termux_volume": "/data/data/com.termux/files/usr/bin/termux-volume",
    "sh":            "/system/bin/sh",
    "am":            "/system/bin/am",
}

# ============== CAMINHOS / PASTAS BÁSICAS ==============
APP_PORT = int(os.environ.get("PANEL_PORT", "8080"))
ROOT     = os.path.dirname(__file__)
DATA_DIR = os.path.join(ROOT, "data")
LOG_DIR  = os.path.join(ROOT, "logs")
CFG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(LOG_DIR, "esp.log")


# Flask - NÃO usamos static_url_path="" porque vamos interceptar (prioridade: GitHub -> cache -> local)
app = Flask(__name__)

# --- Tracing simples (logs claros: erro/slow com request-id) ---
@app.before_request
def _req_start():
    try:
        g.rid = uuid.uuid4().hex[:8]
    except Exception:
        g.rid = "--------"
    g.t0 = time.time()


@app.after_request
def _req_end(resp):
    try:
        dt_ms = int((time.time() - getattr(g, "t0", time.time())) * 1000)
        # loga só erro (5xx) ou requests realmente lentos
        if resp.status_code >= 500 or dt_ms >= 800:
            log(f"[REQ] rid={getattr(g, 'rid', '-')}"
                f" {request.method} {request.path} -> {resp.status_code} {dt_ms}ms")
    except Exception:
        pass
    return resp


# Guarda o último volume não-zero (para toggle de mute da stream principal)
_last_nonzero = 10

# =================== Atualização via GitHub (remote + fallback local) ===================
# Ideia:
# - Você pode hospedar index.html / esp.html / style.css / images/* no GitHub (raw ou pages).
# - O servidor tenta pegar primeiro do remoto, salva em cache e serve para os clientes.
# - Se não conseguir (sem internet / GitHub fora / URL errada), usa o arquivo local em /static.

CACHE_DIR = os.path.join(DATA_DIR, "remote_cache")

def _remote_assets_cfg():
    cfg = load_cfg()
    r = cfg.get("remote_assets", {}) if isinstance(cfg, dict) else {}
    base = str(r.get("base_url", "")).strip()
    ttl  = int(r.get("cache_ttl_s", 3600))  # 1h
    timeout = float(r.get("timeout_s", 3.0))
    # Habilita automaticamente se base_url estiver preenchida
    return base, ttl, timeout

def _safe_relpath(rel_path: str):
    if not rel_path:
        return None
    p = rel_path.strip().lstrip("/").replace("\\", "/")
    parts = [x for x in p.split("/") if x]
    if any(x == ".." for x in parts):
        return None
    return "/".join(parts)

def _cache_paths(rel_path: str):
    rel = _safe_relpath(rel_path)
    if not rel:
        return None, None
    fpath = os.path.join(CACHE_DIR, rel)
    mpath = fpath + ".meta.json"
    return fpath, mpath

def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _write_json(path: str, obj: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _write_bytes_atomic(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)

def _guess_mimetype(rel_path: str) -> str:
    mt, _ = mimetypes.guess_type(rel_path)
    return mt or "application/octet-stream"


# =================== REMOTE ASSETS: cache/local-first ===================
# Problema clássico em internet instável:
# - O painel é local, mas se o servidor tentar buscar GitHub "antes" de servir o arquivo local,
#   qualquer falha de DNS/Internet faz a página demorar MUITO para renderizar (especialmente CSS + background).
#
# Estratégia adotada aqui:
# 1) Se existir arquivo em cache (data/remote_cache), ele pode ser servido imediatamente.
# 2) Se não existir cache (ou cache for mais antigo que o arquivo local), serve o arquivo local (/static).
# 3) O download remoto só acontece em chamadas explícitas de sincronização (ou quando o arquivo local não existe).
#
# Além disso, quando ocorrer falha de rede, aplicamos um "cooldown" para não ficar tentando de novo a cada request.
_REMOTE_ASSETS_HEALTH = {"last_fail": 0.0, "fail_count": 0}

def _remote_assets_fail_cooldown_s() -> float:
    cfg = load_cfg()
    r = cfg.get("remote_assets", {}) if isinstance(cfg, dict) else {}
    try:
        return float(r.get("fail_cooldown_s", 90.0))  # padrão: 90s sem novas tentativas após falhar
    except Exception:
        return 90.0

def _read_cache_bytes(rel_path: str):
    """Lê bytes do cache (data/remote_cache) sem exigir base_url remoto."""
    cpath, mpath = _cache_paths(rel_path)
    if not cpath or not os.path.exists(cpath):
        return None, None
    try:
        with open(cpath, "rb") as f:
            data = f.read()
        meta = _read_json(mpath) or {}
        return data, meta
    except Exception:
        return None, None

def _make_asset_response(data: bytes, rel_path: str, src: str):
    resp = Response(data, mimetype=_guess_mimetype(rel_path))
    # HTML: sempre revalidar. CSS/imagens: pode cachear, mas como o servidor é local, revalidar é barato.
    ext = (os.path.splitext(rel_path)[1] or "").lower()
    if ext in (".html",):
        resp.headers["Cache-Control"] = "no-cache"
    else:
        # Permite cache, mas força revalidação (evita ficar preso em versão velha após Sync UI)
        resp.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
    resp.headers["X-Asset-Source"] = src
    return resp

def _cached_remote_get_bytes(rel_path: str, force: bool = False, respect_ttl: bool = True):
    # (bytes, source) onde source = 'remote'|'cache', ou (None, None).
    base, ttl, timeout = _remote_assets_cfg()
    if not base:
        return None, None

    rel = _safe_relpath(rel_path)
    if not rel:
        return None, None

    url = base.rstrip("/") + "/" + rel
    cpath, mpath = _cache_paths(rel)
    now = time.time()

    meta = _read_json(mpath) or {}
    have_cache = cpath and os.path.exists(cpath)

    # Se cache ainda estiver "fresco" e respeitando TTL, serve direto
    if respect_ttl and have_cache and meta.get("fetched_at") and (now - float(meta["fetched_at"]) < ttl):
        try:
            with open(cpath, "rb") as f:
                return f.read(), "cache"
        except Exception:
            pass


    # Se a rede acabou de falhar, evita ficar tentando de novo a cada request (DNS pode travar bastante em redes ruins).
    cooldown = _remote_assets_fail_cooldown_s()
    if not force:
        last_fail = float(_REMOTE_ASSETS_HEALTH.get("last_fail", 0.0) or 0.0)
        if last_fail and (now - last_fail) < cooldown:
            if have_cache:
                try:
                    with open(cpath, "rb") as f:
                        return f.read(), "cache"
                except Exception:
                    pass
            return None, None

    headers = {"User-Agent": "InsightPanel/1.0"}
    if not force:
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]

    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code == 304 and have_cache:
            meta["fetched_at"] = now
            _write_json(mpath, meta)
            with open(cpath, "rb") as f:
                return f.read(), "cache"

        if r.ok and r.content:
            # atualiza cache
            _write_bytes_atomic(cpath, r.content)
            meta = {
                "url": url,
                "etag": r.headers.get("ETag", ""),
                "last_modified": r.headers.get("Last-Modified", ""),
                "fetched_at": now,
                "status": r.status_code,
            }
            _write_json(mpath, meta)
            return r.content, "remote"

        # Resposta válida, mas sem conteúdo útil (ou status != 2xx/304). Tenta cache/local.
        if not r.ok:
            _REMOTE_ASSETS_HEALTH["last_fail"] = now
            _REMOTE_ASSETS_HEALTH["fail_count"] = int(_REMOTE_ASSETS_HEALTH.get("fail_count", 0) or 0) + 1
            if have_cache:
                try:
                    with open(cpath, "rb") as f:
                        return f.read(), "cache"
                except Exception:
                    pass

    except Exception as e:
        _REMOTE_ASSETS_HEALTH["last_fail"] = now
        _REMOTE_ASSETS_HEALTH["fail_count"] = int(_REMOTE_ASSETS_HEALTH.get("fail_count", 0) or 0) + 1
        log(f"[remote-assets] falha GET {url}: {e}")

    # fallback: se existe cache (mesmo "velho"), usa ele
    if have_cache:
        try:
            with open(cpath, "rb") as f:
                return f.read(), "cache"
        except Exception:
            pass

    return None, None

def serve_asset(rel_path: str):
    """Serve um arquivo do painel (UI): prioridade cache -> local (/static) -> remoto (GitHub).

    Importante: em rede instável, NÃO tentamos GitHub durante o carregamento normal da UI,
    para evitar travar a renderização por falha de DNS/Internet.
    """
    # 1) cache (se existir e não for mais antigo que o arquivo local)
    rel = _safe_relpath(rel_path)
    if not rel:
        abort(404)

    local_path = os.path.join(ROOT, "static", rel)
    cache_data, cache_meta = _read_cache_bytes(rel)
    if cache_data is not None:
        # Se o arquivo local for mais novo (ex: você atualizou o programa via zip), prefere local.
        try:
            local_mtime = os.path.getmtime(local_path) if os.path.exists(local_path) else 0.0
        except Exception:
            local_mtime = 0.0
        try:
            cache_ts = float((cache_meta or {}).get("fetched_at") or 0.0)
        except Exception:
            cache_ts = 0.0

        if not os.path.exists(local_path) or (cache_ts >= local_mtime):
            return _make_asset_response(cache_data, rel, "cache")

    # 2) local
    if os.path.exists(local_path):
        resp = send_from_directory("static", rel, conditional=True)
        resp.headers["X-Asset-Source"] = "local"
        # Para HTML, revalidar sempre; para outros, revalidar rápido (local) e não prender versão velha
        ext = (os.path.splitext(rel)[1] or "").lower()
        if ext in (".html",):
            resp.headers["Cache-Control"] = "no-cache"
        else:
            resp.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
        return resp

    
    # favicon é opcional: se não existir local/cache, não tenta baixar do GitHub (evita travar por DNS)
    if rel == "favicon.ico":
        return Response(b"", status=204)

# 3) remoto (só se não existir local)
    data, src = _cached_remote_get_bytes(rel, force=False, respect_ttl=True)
    if data is not None:
        return _make_asset_response(data, rel, src or "remote")

    abort(404)




# =================== Persistência de configuração ===================
def load_cfg():
    """
    Carrega config.json (ESP IP, estado salvo, mute_state, prev_volumes).
    Usado em:
      - /api/esp-ip
      - /api/status
      - /api/esp-state-sink
      - /api/ir  (para controle de mute de QUIOSQUE/PISCINA)
    """
    if not os.path.exists(CFG_PATH):
        return {
            "esp_ip": "",
            "esp_state": {},
            "mute_state": {"QUIOSQUE": False, "PISCINA": False},
            "prev_volumes": {"QUIOSQUE": 15, "PISCINA": 15},
        "remote_assets": {"base_url": "", "cache_ttl_s": 3600, "timeout_s": 3.0},
        "remote_videos": {"base_url": "", "cache_ttl_s": 86400, "timeout_s": 20.0},
        }
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "esp_ip": "",
            "esp_state": {},
            "mute_state": {"QUIOSQUE": False, "PISCINA": False},
            "prev_volumes": {"QUIOSQUE": 15, "PISCINA": 15},
        "remote_assets": {"base_url": "", "cache_ttl_s": 3600, "timeout_s": 3.0},
        "remote_videos": {"base_url": "", "cache_ttl_s": 86400, "timeout_s": 20.0},
        }


def _atomic_write_json(path: str, obj: dict):
    """Escrita atômica de JSON (evita config corrompido em quedas de energia)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    #tmp = path + ".tmp"
    
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp, path)

# ---- Persistência "profissional": lock + debounce (evita escrever config a cada /sink) ----
CFG_LOCK = threading.RLock()
_CFG_DIRTY = False
_CFG_LAST_DIRTY_TS = 0.0
_CFG_LAST_SAVE_TS = 0.0
_CFG_SAVE_THREAD = None

_CFG_SAVE_TICK_S = 0.25
_CFG_DEBOUNCE_S = 0.8        # espera pequena após última mudança
_CFG_MIN_INTERVAL_S = 2.0    # mínimo entre gravações (quando há várias mudanças)
_CFG_MAX_STALE_S = 30.0      # força gravação eventual mesmo com mudanças contínuas


def _cfg_mark_dirty():
    global _CFG_DIRTY, _CFG_LAST_DIRTY_TS
    _CFG_DIRTY = True
    _CFG_LAST_DIRTY_TS = time.time()


def _cfg_worker():
    global _CFG_DIRTY, _CFG_LAST_SAVE_TS
    while True:
        time.sleep(_CFG_SAVE_TICK_S)

        if not _CFG_DIRTY:
            continue

        now = time.time()
        if (now - _CFG_LAST_DIRTY_TS) < _CFG_DEBOUNCE_S:
            continue

        # respeita intervalo mínimo, mas não deixa acumular pra sempre
        if (now - _CFG_LAST_SAVE_TS) < _CFG_MIN_INTERVAL_S and (now - _CFG_LAST_DIRTY_TS) < _CFG_MAX_STALE_S:
            continue

        with CFG_LOCK:
            snapshot = dict(CFG)

        try:
            _atomic_write_json(CFG_PATH, snapshot)
            _CFG_LAST_SAVE_TS = time.time()
            _CFG_DIRTY = False
        except Exception as e:
            # mantém dirty para tentar de novo; loga sem derrubar a thread
            try:
                log(f"[cfg] FAIL save err={e}")
            except Exception:
                pass


def _cfg_start_thread():
    global _CFG_SAVE_THREAD
    if _CFG_SAVE_THREAD and _CFG_SAVE_THREAD.is_alive():
        return
    _CFG_SAVE_THREAD = threading.Thread(target=_cfg_worker, daemon=True)
    _CFG_SAVE_THREAD.start()


def save_cfg(cfg, immediate: bool = True):
    """Salva config.json.

    - immediate=True: grava já (atômico).
    - immediate=False: marca dirty e grava em background (debounce/intervalo).
    """
    global CFG, _CFG_DIRTY, _CFG_LAST_SAVE_TS
    with CFG_LOCK:
        CFG = cfg

    if immediate:
        try:
            with CFG_LOCK:
                snapshot = dict(CFG)
            _atomic_write_json(CFG_PATH, snapshot)
            _CFG_LAST_SAVE_TS = time.time()
            _CFG_DIRTY = False
            return
        except Exception as e:
            _cfg_mark_dirty()
            try:
                log(f"[cfg] FAIL immediate err={e}")
            except Exception:
                pass
    else:
        _cfg_mark_dirty()


CFG = load_cfg()


# =================== Log circular + log em arquivo ===================
LOG_BUF = deque(maxlen=500)


def log(msg):
    """
    Registra linha de log no buffer em memória e em arquivo logs/esp.log.

    Consumido por:
      - /api/logs                  (esp.html -> card "Logs do servidor")
      - Funções internas (status, esp-state-sink, comandos IR/GPIO etc).
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    LOG_BUF.append(line)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


# inicia thread de persistência do config (depois de log() existir)
_cfg_start_thread()


# =================== Utilitários Termux (volume / comandos) ===================
def run(cmd, use_shell=False):
    """
    Executa um comando no Android:
      - Se use_shell=True: usa /system/bin/sh -c "<cmd>"
      - Caso contrario: executa lista [bin, arg1, arg2...]

    Retorna (ok, out).
    """
    try:
        if use_shell:
            p = subprocess.run([BIN["sh"], "-c", cmd], capture_output=True, text=True)
        else:
            p = subprocess.run(cmd, capture_output=True, text=True)
        out = (p.stdout or p.stderr or "").strip()
        ok = (p.returncode == 0)
        return ok, out
    except Exception as e:
        return False, str(e)


def parse_termux_volume(out: str):
    """
    Converte saída JSON do termux-volume em dict:
      { "stream_name": {"volume": int, "max": int}, ... }
    """
    data = json.loads(out)
    if isinstance(data, list):
        return {
            i.get("stream"): {
                "volume": int(i.get("volume", 0)),
                "max": int(i.get("max_volume", 0)),
            }
            for i in data
            if i.get("stream")
        }
    if isinstance(data, dict):
        d = {}
        for k, v in data.items():
            if isinstance(v, dict) and "volume" in v:
                d[k] = {
                    "volume": int(v.get("volume", 0)),
                    "max": int(v.get("max_volume", 0)),
                }
        return d
    raise RuntimeError("Formato inesperado do termux-volume")


def get_current():
    """
    Le a stream music/media atual no Termux.
    Usado em:
      - GET  /api/vol
      - POST /api/vol/set
      - POST /api/mute
    """
    ok, out = run([BIN["termux_volume"]])
    if not ok:
        raise RuntimeError(out)
    streams = parse_termux_volume(out)
    for name in ("music", "media"):
        if name in streams:
            info = streams[name]
            return name, info["volume"], info["max"]
    raise RuntimeError("stream music/media não encontrada")


def set_abs(stream, value):
    """
    Seta valor absoluto da stream (music/media) no Termux.

    Usado em:
      - POST /api/vol/set
      - POST /api/mute
    """
    ok, out = run([BIN["termux_volume"], stream, str(int(value))])
    if not ok:
        raise RuntimeError(out)
    return True


# =================== Normalização de estado do ESP ===================
"""
def normalize_state(resp):
    
    #Converte resposta bruta do ESP (endpoint /state) num formato padronizado:
    #  {
        #"volumes": {"quiosque": int, "piscina": int},
        #"relays":  {"r1":0/1,"r2":0/1,"r3":0/1,"r4":0/1},
       # "ble":     0,  # reservado
      #  "mute_state": {"QUIOSQUE": bool, "PISCINA": bool}
     # }

    #Consumido por:
    #  - GET /api/status   (index.html e esp.html)
    
    if not isinstance(resp, dict):
        return {}
    gpio = resp.get("gpio_states") or {}
    vols = resp.get("ir_volumes") or {}
    # mutes (toggle): prefer ESP `ir_mutes` (novo), senão `mute_state` (legado), senão CFG
    _im = resp.get("ir_mutes")
    if isinstance(_im, dict):
        mute_state = {
            "QUIOSQUE": bool(_im.get("QUIOSQUE", _im.get("quiosque", False))),
            "PISCINA":  bool(_im.get("PISCINA",  _im.get("piscina",  False))),
        }
    else:
        _ms = resp.get("mute_state")
        if isinstance(_ms, dict):
            mute_state = {
                "QUIOSQUE": bool(_ms.get("QUIOSQUE", False)),
                "PISCINA":  bool(_ms.get("PISCINA",  False)),
            }
        else:
            _cfg_ms = CFG.get("mute_state", {"QUIOSQUE": False, "PISCINA": False})
            mute_state = {
                "QUIOSQUE": bool(_cfg_ms.get("QUIOSQUE", False)) if isinstance(_cfg_ms, dict) else False,
                "PISCINA":  bool(_cfg_ms.get("PISCINA",  False)) if isinstance(_cfg_ms, dict) else False,
            }
    pin_map = [(23, "r1"), (22, "r2"), (21, "r3"), (19, "r4")]
    relays = {}
    for pin, name in pin_map:
        val = gpio.get(str(pin), gpio.get(pin))
        relays[name] = 1 if val in (True, 1, "1", "ON", "HIGH") else 0

    volumes = {
        "quiosque": int(vols.get("QUIOSQUE", 0)),
        "piscina": int(vols.get("PISCINA", 0)),
    }
    return {"volumes": volumes, "relays": relays, "ble": 0, "mute_state": mute_state}
"""
def normalize_state(resp):
    """
    Converte resposta bruta do ESP (endpoint /state) num formato padronizado
    consumido por GET /api/status (index.html e esp.html).
    """
    if not isinstance(resp, dict):
        return {}

    gpio = resp.get("gpio_states") or {}
    vols = resp.get("ir_volumes") or {}
    # mutes (toggle): prefer ESP `ir_mutes` (novo), senão `mute_state` (legado), senão CFG
    _im = resp.get("ir_mutes")
    if isinstance(_im, dict):
        mute_state = {
            "QUIOSQUE": bool(_im.get("QUIOSQUE", _im.get("quiosque", False))),
            "PISCINA":  bool(_im.get("PISCINA",  _im.get("piscina",  False))),
        }
    else:
        _ms = resp.get("mute_state")
        if isinstance(_ms, dict):
            mute_state = {
                "QUIOSQUE": bool(_ms.get("QUIOSQUE", False)),
                "PISCINA":  bool(_ms.get("PISCINA",  False)),
            }
        else:
            _cfg_ms = CFG.get("mute_state", {"QUIOSQUE": False, "PISCINA": False})
            mute_state = {
                "QUIOSQUE": bool(_cfg_ms.get("QUIOSQUE", False)) if isinstance(_cfg_ms, dict) else False,
                "PISCINA":  bool(_cfg_ms.get("PISCINA",  False)) if isinstance(_cfg_ms, dict) else False,
            }
    # relés/entradas (mantém seu mapeamento atual)
    pin_map = [(23, "r1"), (22, "r2"), (21, "r3"), (19, "r4")]
    relays = {}
    for pin, name in pin_map:
        val = gpio.get(str(pin), gpio.get(pin))
        relays[name] = 1 if val in (True, 1, "1", "ON", "HIGH") else 0

    volumes = {
        "quiosque": int(vols.get("QUIOSQUE", 0)),
        "piscina": int(vols.get("PISCINA", 0)),
    }

    out = {"volumes": volumes, "relays": relays, "ble": 0, "mute_state": mute_state}

    # ===== extras de Wi-Fi / identidade do ESP (para /esp) =====
    out["wifi"] = bool(resp.get("wifi", False))

    ssid = resp.get("ssid")
    if isinstance(ssid, str):
        out["ssid"] = ssid

    mac = resp.get("mac")
    if isinstance(mac, str):
        out["mac"] = mac

    rssi_dbm = resp.get("rssi_dbm", resp.get("rssi"))
    if rssi_dbm is not None:
        try:
            out["rssi_dbm"] = float(rssi_dbm)
        except Exception:
            out["rssi_dbm"] = rssi_dbm

    rssi_bars = resp.get("rssi_bars")
    if rssi_bars is not None:
        try:
            out["rssi_bars"] = int(rssi_bars)
        except Exception:
            out["rssi_bars"] = rssi_bars

    # (opcional) se quiser usar depois no /esp
    for k in ("local_ip", "gateway", "subnet", "free_heap_kb", "uptime_s", "flask_connected", "last_event"):
        if k in resp:
            out[k] = resp.get(k)

    return out

# =================== Helpers/Cache de polling do ESP ===================
def esp_base():
    """Retorna URL base do ESP (ex: http://192.168.0.150) ou None se não configurado."""
    ip = (CFG.get("esp_ip") or "").strip()
    if not ip:
        return None
    return ip if ip.startswith("http") else f"http://{ip}"


# Estado para logs de transição de polling (evitar flood de logs)
POLL_HINT_SECS = 3
_poll_state = {"announced": False, "was_ok": None}


def _poll_log_ok_once():
    if not _poll_state["announced"]:
        log(
            f"Solicitando estados do ESP e sincronizando em background (≈{POLL_HINT_SECS}s). "
            "Novos logs só em caso de falha ou reconexão."
        )
        _poll_state["announced"] = True


def _poll_transition(ok: bool):
    prev = _poll_state["was_ok"]
    _poll_state["was_ok"] = ok
    if ok:
        if prev is False:
            log("Conexão restabelecida: estado do ESP atualizado novamente.")
        elif prev is None:
            _poll_log_ok_once()
    else:
        if prev is not False:
            log("Falha ao atualizar estado do ESP (timeout/erro). Mantendo último estado salvo.")


# Cache de status do ESP (evita espancar o ESP quando ha varios clientes)
STATUS_CACHE = {
    "ts": 0.0,
    "ok": False,
    "state": {},
    "min_ok": 1.0,   # minimo entre consultas reais quando OK
    "min_fail": 3.0, # backoff quando falhando
}

# --- ONLINE via "last_seen" (poll OU sink) ---
ONLINE_GRACE_S = 12.0  # considera online se recebemos estado recentemente

SINK_META = {
    "last_seen_ts": 0.0,
    "last_ip": "",
    "last_seq": 0,
}


def _local_tz_offset_s() -> int:
    """Offset local em segundos (east of UTC)."""
    try:
        lt = time.localtime()
        if getattr(lt, "tm_isdst", 0) and time.daylight:
            return int(-time.altzone)
        return int(-time.timezone)
    except Exception:
        return 0


def _last_seen_ts() -> float:
    # STATUS_CACHE["ts"] = última atualização (poll ou sink)
    # SINK_META["last_seen_ts"] = último push do ESP
    try:
        return max(float(STATUS_CACHE.get("ts", 0.0) or 0.0), float(SINK_META.get("last_seen_ts", 0.0) or 0.0))
    except Exception:
        return float(STATUS_CACHE.get("ts", 0.0) or 0.0)


def _is_online(now: float) -> bool:
    last = _last_seen_ts()
    return bool(STATUS_CACHE.get("ok", False)) and last > 0 and (now - last) <= ONLINE_GRACE_S



# Locks/Meta para evitar consultas concorrentes ao ESP e expor "online/offline" de forma confiável
STATUS_LOCK = threading.Lock()
_STATUS_INFLIGHT = {"running": False, "started_ts": 0.0}
_STATUS_META = {"fail_streak": 0, "last_ok_ts": 0.0, "last_fail_ts": 0.0}


def _status_min_gap(is_ok: bool) -> float:
    """Define o intervalo mínimo entre consultas REAIS ao ESP.

    - Online: rápido (min_ok).
    - Offline: "knock" controlado (base min_fail = 3s) e backoff progressivo se ficar muito tempo caído.
    """
    if is_ok:
        return float(STATUS_CACHE.get("min_ok", 1.0))

    fs = int(_STATUS_META.get("fail_streak", 0))
    base = float(STATUS_CACHE.get("min_fail", 3.0))  # base knock (ex: 3s)

    if fs <= 1:
        return base
    if fs <= 3:
        return max(base, 4.0)
    if fs <= 6:
        return max(base, 6.0)
    return max(base, 10.0)


def proxy_get(path, params=None, timeout=4):
    """
    GET simples no ESP (JSON ou texto).
    Usado em:
      - /api/status
      - /api/state
      - /api/ir
      - /api/gpio
      - /api/startshow (ligar reles)
      - /api/stopshow  (desligar reles)
    """
    base = esp_base()
    if not base:
        return False, "ESP IP not set"
    url = f"{base}/{path.lstrip('/')}"
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code // 100 == 2:
            try:
                return True, r.json()
            except Exception:
                return True, r.text
        return False, f"{r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


def send_esp32_command(method, endpoint, params=None):
    """
    Helper generico para comandos no ESP (GET/POST).
    Usado em:
      - /api/status_esp32
      - /api/wifi_config
    """
    base = esp_base()
    if not base:
        log("ESP32 IP não configurado, comando não enviado.")
        return False, "ESP32 IP não configurado"
    try:
        url = base + "/" + endpoint.lstrip("/")
        if method == "GET":
            r = requests.get(url, params=params, timeout=5)
        elif method == "POST":
            r = requests.post(url, json=params, timeout=5)
        else:
            return False, "Método HTTP não suportado"
        if r.status_code // 100 != 2:
            log(f"REQ {method} {url} -> {r.status_code}")
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "")
        return True, (r.json() if "application/json" in ctype else r.text)
    except requests.exceptions.RequestException as e:
        log(f"ERR {method} {endpoint}: {e}")
        return False, str(e)


# =================== Páginas HTML (frontend) ===================
@app.route("/")
def index():
    """
    Entrega index.html (Painel principal).
    - Arquivo: static/index.html
    """
    return serve_asset("index.html")


@app.route("/esp")
def esp_page():
    """
    Entrega esp.html (Configurações / Diagnóstico do ESP).
    - Arquivo: static/esp.html
    """
    return serve_asset("esp.html")

# ---------- Static (CSS / imagens / outros) ----------
@app.route("/style.css")
def style_css():
    return serve_asset("style.css")

@app.route("/images/<path:fname>")
def images(fname):
    return serve_asset("images/" + fname)

# Catch-all: se você adicionar futuramente app.js, favicon, etc., e colocar no GitHub, funciona sem mexer no server.
@app.route("/<path:filename>")
def any_static(filename):
    # evita interceptar /api/...
    if filename.startswith("api/"):
        abort(404)
    # só serve arquivos que existam localmente ou quando remoto estiver configurado
    base, _, _ = _remote_assets_cfg()
    local_path = os.path.join(ROOT, "static", _safe_relpath(filename) or "")
    if (base and _safe_relpath(filename)) or (os.path.exists(local_path)):
        return serve_asset(filename)
    abort(404)




# =================== API: Master volume (Termux) ===================
# Cache leve de /api/vol para nao chamar termux-volume a cada 200ms
_VOL_CACHE = {"ts": 0.0, "data": None, "ttl": 2.0}  # 2s basta para UI


@app.get("/api/vol")
def vol_get():
    """
    Le volume master (Termux).
    Usado por:
      - index.html, card "Principal"
        - slider #slider
        - texto #volPct, #maxInfo, #absInfo
    """
    now = time.time()
    if _VOL_CACHE["data"] and (now - _VOL_CACHE["ts"] < _VOL_CACHE["ttl"]):
        return jsonify(**_VOL_CACHE["data"])
    name, cur, mx = get_current()
    _VOL_CACHE["data"] = {"stream": name, "value": cur, "max": mx}
    _VOL_CACHE["ts"] = now
    return jsonify(**_VOL_CACHE["data"])


@app.post("/api/vol/set")
def vol_set():
    """
    Ajusta volume master para valor absoluto (Termux).
    Usado por:
      - index.html, slider #slider (card "Principal")
    """
    j = request.get_json(force=True, silent=True) or {}
    target = int(j.get("value", 0))
    name, cur, mx = get_current()
    target = max(0, min(target, mx))
    global _last_nonzero
    if target > 0:
        _last_nonzero = target
    set_abs(name, target)
    _VOL_CACHE["data"] = {"stream": name, "value": target, "max": mx}
    _VOL_CACHE["ts"] = time.time()
    return jsonify(**_VOL_CACHE["data"])


@app.post("/api/mute")
def mute_toggle():
    """
    Toggle de mute da stream principal (music/media).
    Usado por:
      - index.html, botão #btnMute (card "Principal")
    """
    name, cur, mx = get_current()
    global _last_nonzero
    if cur > 0:
        _last_nonzero = cur
        set_abs(name, 0)
        _VOL_CACHE["data"] = {"stream": name, "value": 0, "max": mx}
        _VOL_CACHE["ts"] = time.time()
        return jsonify(muted=True, stream=name, value=0, max=mx)

    restore = _last_nonzero if _last_nonzero > 0 else max(1, mx // 2)
    restore = min(restore, mx)
    set_abs(name, restore)
    _VOL_CACHE["data"] = {"stream": name, "value": restore, "max": mx}
    _VOL_CACHE["ts"] = time.time()
    return jsonify(muted=False, stream=name, value=restore, max=mx)


# =================== API: Abrir apps no Android TV/Box ===================
@app.post("/api/youtube")
def youtube():
    """
    Abre o aplicativo/URL do YouTube no Android.
    Usado por:
      - index.html, card "Aplicativos" -> botão #btnYouTube
    """
    cmd = 'am start -a android.intent.action.VIEW -d "https://www.youtube.com"'
    ok, out = run(cmd, use_shell=True)
    return (jsonify(ok=ok, out=out, ran=cmd), 200 if ok else 500)

# 1) mapeia as chaves para CAMINHO REAL (recomendo padronizar tudo num lugar só)
def _video_dir():
    """Diretório local dos vídeos.
    Por padrão: static/videos (relativo ao projeto).
    Você pode trocar em data/config.json, chave: "video_dir".
    Ex.: "/storage/emulated/0/movies" (caso prefira armazenamento público).
    """
    cfg = load_cfg()
    vdir = str(cfg.get("video_dir", "static/videos")).strip() or "static/videos"
    if not os.path.isabs(vdir):
        vdir = os.path.join(ROOT, vdir)
    return vdir


def video_map():
    """Mapa chave->caminho completo do vídeo local (usado pelos botões)."""
    d = _video_dir()
    return {
        "welcome":  os.path.join(d, "bemvindo.mp4"),
        "video1":   os.path.join(d, "video1.mp4"),
        "saudacao": os.path.join(d, "saudacao.mp4"),
    }


def _remote_videos_cfg():
    cfg = load_cfg()
    r = cfg.get("remote_videos", {}) if isinstance(cfg, dict) else {}
    base = str(r.get("base_url", "")).strip()          # ex: https://raw.githubusercontent.com/USER/REPO/main/videos
    ttl  = int(r.get("cache_ttl_s", 86400))            # 1 dia
    timeout = float(r.get("timeout_s", 20.0))
    # habilita automaticamente se base_url estiver preenchida
    return base, ttl, timeout

def _video_meta_path(local_path: str) -> str:
    return local_path + ".meta.json"

def _download_to_file(url: str, dest_path: str, timeout: float, etag: str = "", last_modified: str = "", force: bool = False):
    """Baixa um arquivo grande de forma segura (tmp -> replace), com suporte a 304."""
    headers = {"User-Agent": "InsightPanel/1.0"}
    if not force:
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

    r = requests.get(url, headers=headers, timeout=timeout, stream=True)
    if r.status_code == 304:
        return {"changed": False, "status": 304, "etag": etag, "last_modified": last_modified}

    if not r.ok:
        return {"changed": False, "status": r.status_code, "error": f"HTTP {r.status_code}"}

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + ".tmp"
    h = hashlib.sha256()
    total = 0
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            f.write(chunk)
            h.update(chunk)
            total += len(chunk)

    os.replace(tmp, dest_path)
    return {
        "changed": True,
        "status": r.status_code,
        "etag": r.headers.get("ETag", ""),
        "last_modified": r.headers.get("Last-Modified", ""),
        "bytes": total,
        "sha256": h.hexdigest(),
    }

def sync_remote_videos(force: bool = False, respect_ttl: bool = True):
    """Sincroniza vídeos locais (bemvindo/video1/saudacao) a partir de um base_url no GitHub.
    Importante: NÃO baixa no momento do play (evita delay). Baixa apenas quando você chamar este endpoint.
    """
    base, ttl, timeout = _remote_videos_cfg()
    if not base:
        return {"ok": False, "msg": "remote_videos.base_url vazio (desabilitado)", "results": {}}

    now = time.time()
    results = {}

    for key, local_path in video_map().items():
        filename = os.path.basename(local_path)
        url = base.rstrip("/") + "/" + filename
        # Se for "forçar download", adiciona cache-buster para evitar CDN entregando versão antiga
        if force:
            url = url + ("&" if "?" in url else "?") + "v=" + str(int(now))
        meta_path = _video_meta_path(local_path)
        meta = _read_json(meta_path) or {}
        have_local = os.path.exists(local_path)

        # Se foi checado recentemente (TTL) e não é force, pula
        if respect_ttl and (not force) and have_local and meta.get("fetched_at") and (now - float(meta["fetched_at"]) < ttl):
            results[key] = {"skipped": True, "path": local_path}
            continue

        try:
            res = _download_to_file(
                url=url,
                dest_path=local_path,
                timeout=timeout,
                etag=str(meta.get("etag", "")),
                last_modified=str(meta.get("last_modified", "")),
                force=force,
            )
            meta.update({
                "url": url,
                "etag": res.get("etag", meta.get("etag", "")),
                "last_modified": res.get("last_modified", meta.get("last_modified", "")),
                "fetched_at": now,
                "status": res.get("status", 0),
                "sha256": res.get("sha256", meta.get("sha256", "")),
            })
            _write_json(meta_path, meta)
            results[key] = {"path": local_path, **res}
        except Exception as e:
            results[key] = {"path": local_path, "error": str(e)}

    return {"ok": True, "base_url": base, "results": results}

def sync_remote_ui(force: bool = False, respect_ttl: bool = True):
    """Pré-carrega os arquivos de UI no cache (index/esp/style/bg)."""
    base, _, _ = _remote_assets_cfg()
    if not base:
        return {"ok": False, "msg": "remote_assets.base_url vazio (desabilitado)", "results": {}}

    files = ["index.html", "esp.html", "style.css", "images/background.jpg", "favicon.ico"]
    results = {}
    for f in files:
        data, src = _cached_remote_get_bytes(f, force=force, respect_ttl=respect_ttl)
        results[f] = {"ok": data is not None, "source": src or "none", "bytes": (len(data) if data else 0)}
    return {"ok": True, "base_url": base, "results": results}

"""
def play_local_mp4(path: str):
    # IMPORTANTE:
    # - Nunca passe caminho do Termux (/data/data/.../home/storage/...) para apps externos.
    # - Convertemos para o caminho REAL em /storage/... usando realpath.
    # - Usamos file:/// + fallback termux-open.
    real = os.path.realpath(path)

    # Garante URI file:///... (3 barras)
    uri = "file:///" + real.lstrip("/")

    cmd = (
        "am start -a android.intent.action.VIEW "
        f"-d {shlex.quote(uri)} -t video/mp4"
    )
    ok, out = run(cmd, use_shell=True)

    # Fallback: alguns players aceitam melhor abrir pelo termux-open
    if not ok or ("não é possível reproduzir" in (out or "").lower()) or ("cannot play" in (out or "").lower()):
        cmd2 = f"termux-open {shlex.quote(real)}"
        ok2, out2 = run(cmd2, use_shell=True)
        if ok2:
            return ok2, out2, cmd2
        return False, (out or "") + "\n" + (out2 or ""), cmd + " || " + cmd2

    return ok, out, cmd
"""

def _termux_open_bin():
    '''Retorna o binário do termux-open se disponível (caminho absoluto), senão None.'''
    to = BIN.get("termux_open")
    if to and os.path.exists(to):
        return to
    to = shutil.which("termux-open")
    if to:
        return to
    cand = "/data/data/com.termux/files/usr/bin/termux-open"
    if os.path.exists(cand):
        return cand
    return None


def find_welcome_mp4_path():
    '''Encontra bemvindo.mp4 na pasta Movies (varia Maiúscula/Minúscula por dispositivo).'''
    cand_cfg = (CFG.get("welcome_video_path") if isinstance(globals().get("CFG"), dict) else None)
    cands = []
    if cand_cfg:
        cands.append(str(cand_cfg))

    cands += [
        "/storage/emulated/0/Movies/bemvindo.mp4",
        "/storage/emulated/0/movies/bemvindo.mp4",
        "/sdcard/Movies/bemvindo.mp4",
        "/sdcard/movies/bemvindo.mp4",
    ]
    for p in cands:
        try:
            if p and os.path.exists(p):
                return p
        except Exception:
            pass
    return "/storage/emulated/0/Movies/bemvindo.mp4"


# --- VIDEO CONTROL (anti-bloqueio) -----------------------------------------
# Esses recursos existem para evitar o bug observado: "um vídeo em execução impede outro de iniciar".
# Mantemos tudo em best-effort (se algum comando não existir/for bloqueado, seguimos com o próximo).

VIDEO_LOCK = threading.Lock()

def _video_play_cfg():
    """Configurações de reprodução de vídeo (compatível com o legado).

    config.json -> "video_play":
      - pre_home: bool (default True) -> manda HOME antes de tocar vídeo
      - force_stop_pkgs: [str] (default ["org.videolan.vlc"]) -> pacotes para force-stop
      - intent_flags: str/int (default "0x10008000") -> flags no am start (substitui --activity-new-task)
    """
    try:
        cfg = CFG if isinstance(CFG, dict) else load_cfg()
    except Exception:
        cfg = load_cfg()

    v = cfg.get("video_play", {}) if isinstance(cfg, dict) else {}

    pre_home = bool(v.get("pre_home", True))

    pkgs = v.get("force_stop_pkgs", ["org.videolan.vlc"])
    if isinstance(pkgs, str):
        pkgs = [pkgs]
    if not isinstance(pkgs, list):
        pkgs = []
    pkgs = [str(x).strip() for x in pkgs if str(x).strip()]

    flags = v.get("intent_flags", "0x10008000")
    # aceita int ou str
    if isinstance(flags, (int, float)):
        flags = hex(int(flags))
    flags = str(flags).strip() if flags is not None else ""
    if not flags:
        flags = "0x10008000"

    return {"pre_home": pre_home, "force_stop_pkgs": pkgs, "intent_flags": flags}

def stop_video_player():
    """Para o player (best effort): MEDIA_STOP + HOME + force-stop.

    A ordem e o uso de flags seguem o comportamento do server legado que funcionava bem.
    """
    vcfg = _video_play_cfg()

    # 0) tenta parar mídia
    try:
        run("input keyevent 86", use_shell=True)  # KEYCODE_MEDIA_STOP
    except Exception:
        pass

    # 1) volta para HOME antes de matar o player (reduz chance de ficar "preso" em foreground)
    if vcfg.get("pre_home", True):
        try:
            run("am start -a android.intent.action.MAIN -c android.intent.category.HOME", use_shell=True)
        except Exception:
            pass

    # 2) encerra pacotes conhecidos (default: VLC)
    for pkg in (vcfg.get("force_stop_pkgs") or []):
        try:
            run(f"am force-stop {pkg}", use_shell=True)
        except Exception:
            pass

def play_local_mp4(path: str):
    """Abre um MP4 no Android TV/Box (modo legado/robusto).

    Regras importantes (legado):
    - Resolve o caminho real (evita symlink do Termux /data/data/.../home/storage/...).
    - Monta URI file:/// corretamente.
    - Usa 'am start' com '-f <flags>' (em alguns Androids, '--activity-new-task' NÃO existe).
    - Faz pre-stop (HOME + force-stop) antes de iniciar.
    - Fallback: termux-open (FileProvider/content://) quando o intent falhar.
    """
    if not path:
        return False, "path vazio", ""

    # serializa para evitar corrida entre 2 cliques (um vídeo atropelando o outro)
    with VIDEO_LOCK:
        # 0) valida existência (primeiro no path bruto, depois no realpath)
        if not os.path.exists(path):
            real_try = os.path.realpath(os.path.abspath(path))
            if not os.path.exists(real_try):
                return False, f"Arquivo não encontrado: {path}", ""

        # 1) encerra player anterior (best effort)
        stop_video_player()
        time.sleep(0.15)  # pequeno gap para o sistema soltar o player anterior

        # 2) caminho real + URI file:///
        real = os.path.realpath(os.path.abspath(path))
        uri = "file:///" + real.lstrip("/")

        # 3) flags (legado)
        vcfg = _video_play_cfg()
        flags = (vcfg.get("intent_flags") or "").strip()

        # 4) tenta abrir via intent padrão de vídeo
        cmd = f'am start -a android.intent.action.VIEW -d "{uri}" -t video/mp4'
        if flags:
            cmd += f" -f {flags}"

        ok, out = run(cmd, use_shell=True)

        # Alguns Android retornam rc=0 mas escrevem "Error:" no output
        if ok and isinstance(out, str) and out.strip().lower().startswith("error"):
            ok = False

        # 5) fallback via termux-open (content:// com grant)
        if not ok:
            to = _termux_open_bin()
            if to:
                ok2, out2 = run([to, real], use_shell=False)
                if ok2:
                    return ok2, out2, f"{to} {shlex.quote(real)}"
                # tenta também via shell (PATH)
                ok3, out3 = run(f'termux-open {shlex.quote(real)}', use_shell=True)
                if ok3:
                    return ok3, out3, f"termux-open {shlex.quote(real)}"
                return False, (out or "") + "\n" + (out2 or "") + "\n" + (out3 or ""), cmd
            else:
                ok2, out2 = run(f'termux-open {shlex.quote(real)}', use_shell=True)
                if ok2:
                    return ok2, out2, f"termux-open {shlex.quote(real)}"
                return False, (out or "") + "\n" + (out2 or ""), cmd

        return True, out, cmd

@app.post("/api/welcome")
def welcome():
    path = video_map()["welcome"]
    ok, out, cmd = play_local_mp4(path)
    return jsonify(ok=ok, out=out, ran=cmd, path=path), (200 if ok else 500)


@app.post("/api/playvideo")
def playvideo():
    j = request.get_json(silent=True) or {}
    key = (j.get("key") or "").strip().lower()

    path = video_map().get(key)
    if not path:
        return jsonify(ok=False, out="key invalida", key=key), 400

    ok, out, cmd = play_local_mp4(path)
    return jsonify(ok=ok, out=out, ran=cmd, key=key, path=path), (200 if ok else 500)

@app.post("/api/home")
def go_home():
    """
    Envia 'HOME' para o Android (voltar para tela inicial).
    Usado por:
      - index.html, card "Aplicativos" -> botão #btnHome (Retorno)
      - /api/stopshow (depois de desligar R1/R2)
    """
    cmd = "am start -a android.intent.action.MAIN -c android.intent.category.HOME"
    ok, out = run(cmd, use_shell=True)
    return (jsonify(ok=ok, out=out, ran=cmd), 200 if ok else 500)


# =================== API: Configuração do ESP & Logs ===================
@app.route("/api/esp-ip", methods=["GET", "POST"])
def api_esp_ip():
    """
    GET:
      - Retorna IP configurado do ESP (CFG["esp_ip"]).
    POST:
      - Atualiza IP e persiste em config.json.

    Usado por:
      - esp.html:
          - Card "Definir IP do Controle"
              - Input  #esp_ip
              - Botão  #save
    """
    global CFG
    if request.method == "GET":
        return jsonify(ok=True, esp_ip=CFG.get("esp_ip", ""))

    data = request.get_json(silent=True) or request.form
    ip = (data.get("ip") or data.get("esp_ip") or "").strip()
    if not ip:
        return jsonify(ok=False, error="ip vazio"), 400

    CFG["esp_ip"] = ip
    save_cfg(CFG)
    log(f"ESP IP salvo: {ip}")
    return jsonify(ok=True, ip=ip)


@app.get("/api/logs")
def api_logs():
    """
    Retorna últimas linhas do buffer de logs (LOG_BUF).
    Usado por:
      - esp.html:
          - Card "Logs do Servidor"
              - <pre id="logs">
              - Botão #reload-logs
    """
    try:
        n = int(request.args.get("n", "200"))
    except Exception:
        n = 200
    lines = list(LOG_BUF)[-n:]
    return jsonify(ok=True, lines=lines)


# =================== API: Status do ESP32 (poll) ===================
@app.get("/api/state")
def api_state_raw():
    """
    #Endpoint bruto (debug) - repassa /state do ESP sem normalizar.

    #Opcional para diagnósticos (não usado diretamente pelo frontend).
    """
    ok, res = proxy_get("state")
    return (jsonify(ok=True, state=res), 200) if ok else (jsonify(ok=False, error=res), 502)


# =================== Sync remoto: progresso + logs (UI e Videos) ===================
# Mantemos o modo "wait" (legado) e um modo "async" com sync_id para UI mostrar progresso.

_SYNC_LOCK = threading.Lock()
_SYNC_JOBS = {}   # sync_id -> job dict
_SYNC_LAST = {"ui": None, "videos": None, "all": None}

def _now_ts():
    return time.time()

def _new_sync_job(what: str, force: bool, respect_ttl: bool):
    sid = uuid.uuid4().hex[:12]
    job = {
        "sync_id": sid,
        "what": what,
        "force": bool(force),
        "respect_ttl": bool(respect_ttl),
        "started_at": _now_ts(),
        "finished_at": None,
        "done": False,
        "ok": None,
        "progress": 0,
        "items": [],     # list of per-file/per-video statuses
        "summary": {"ok": 0, "skipped": 0, "failed": 0, "total": 0},
        "error": None,
    }
    with _SYNC_LOCK:
        _SYNC_JOBS[sid] = job
        # registra "last" por tipo
        _SYNC_LAST[what] = sid
        if what in ("ui", "videos"):
            _SYNC_LAST["all"] = sid  # ultimo global também
    return sid

def _job_set(sid: str, **kwargs):
    with _SYNC_LOCK:
        j = _SYNC_JOBS.get(sid)
        if not j:
            return
        j.update(kwargs)

def _job_add_items(sid: str, new_items: list):
    with _SYNC_LOCK:
        j = _SYNC_JOBS.get(sid)
        if not j:
            return
        j["items"].extend(new_items)

def _recalc_summary(job: dict):
    ok = skipped = failed = 0
    for it in job.get("items", []):
        st = it.get("status")
        if st == "ok":
            ok += 1
        elif st == "skipped":
            skipped += 1
        elif st == "failed":
            failed += 1
    total = len(job.get("items", []))
    job["summary"] = {"ok": ok, "skipped": skipped, "failed": failed, "total": total}

def _run_sync_job(sid: str):
    """
    Executa o sync em thread separada e atualiza PROGRESSO por item,
    para a barra subir gradualmente (a cada arquivo/vídeo concluído).
    """
    with _SYNC_LOCK:
        job = _SYNC_JOBS.get(sid)
    if not job:
        return

    what = job["what"]
    force = job["force"]
    respect_ttl = job["respect_ttl"]

    # listas reais de itens (evita descompasso com "total_items")
    ui_files = ["index.html", "esp.html", "style.css", "images/background.jpg", "favicon.ico"]
    video_items = list(video_map().items())  # [(key, local_path), ...]

    # total esperado para percentual
    total_items = 0
    if what in ("ui", "all"):
        total_items += len(ui_files)
    if what in ("videos", "all"):
        total_items += len(video_items)
    if total_items <= 0:
        total_items = 1

    _job_set(sid, progress=0)
    done_count = 0

    try:
        log(f"[sync] start id={sid} what={what} force={force} respect_ttl={respect_ttl}")

        # ================= UI =================
        if what in ("ui", "all"):
            base, _, _ = _remote_assets_cfg()
            if not base:
                # desabilitado
                item = {
                    "kind": "ui",
                    "name": "config",
                    "status": "failed",
                    "source": "config",
                    "bytes": 0,
                    "error": "remote_assets.base_url vazio (desabilitado)",
                }
                _job_add_items(sid, [item])
                log(f"[sync][ui] config -> failed err={item['error']}")
                done_count += 1
                _job_set(sid, progress=int(done_count * 100 / max(1, total_items)))
            else:
                for f in ui_files:
                    data, src = _cached_remote_get_bytes(f, force=force, respect_ttl=respect_ttl)

                    if data is None:
                        st = "failed"
                    elif src == "cache":
                        st = "skipped"
                    else:
                        st = "ok"

                    item = {
                        "kind": "ui",
                        "name": f,
                        "status": st,
                        "source": (src or "none"),
                        "bytes": (len(data) if data else 0),
                        "error": ("" if data is not None else "falha no download e sem cache"),
                    }
                    _job_add_items(sid, [item])
                    log(f"[sync][ui] {f} -> {st} source={item['source']} bytes={item['bytes']}")

                    done_count += 1
                    _job_set(sid, progress=int(done_count * 100 / max(1, total_items)))

        # ================= VIDEOS =================
        if what in ("videos", "all"):
            base, ttl, timeout = _remote_videos_cfg()
            if not base:
                item = {
                    "kind": "videos",
                    "name": "config",
                    "status": "failed",
                    "path": "",
                    "bytes": 0,
                    "http": 0,
                    "sha256": "",
                    "error": "remote_videos.base_url vazio (desabilitado)",
                }
                _job_add_items(sid, [item])
                log(f"[sync][videos] config -> failed err={item['error']}")
                done_count += 1
                _job_set(sid, progress=int(done_count * 100 / max(1, total_items)))
            else:
                now = time.time()
                for key, local_path in video_items:
                    filename = os.path.basename(local_path)
                    url = base.rstrip("/") + "/" + filename
                    if force:
                        url += ("&" if "?" in url else "?") + f"cb={int(now * 1000)}"

                    meta_path = _video_meta_path(local_path)
                    meta = _read_json(meta_path) or {}
                    have_local = os.path.exists(local_path)

                    # TTL: se checado recentemente e existe arquivo local, pula (a não ser force)
                    if respect_ttl and (not force) and have_local and meta.get("fetched_at") and (now - float(meta["fetched_at"]) < ttl):
                        info = {"skipped": True, "path": local_path, "status": 0, "bytes": 0, "sha256": meta.get("sha256", "")}
                    else:
                        try:
                            res = _download_to_file(
                                url=url,
                                dest_path=local_path,
                                timeout=timeout,
                                etag=str(meta.get("etag", "")),
                                last_modified=str(meta.get("last_modified", "")),
                                force=force,
                            )
                            meta.update({
                                "url": url,
                                "etag": res.get("etag", meta.get("etag", "")),
                                "last_modified": res.get("last_modified", meta.get("last_modified", "")),
                                "fetched_at": now,
                                "status": res.get("status", 0),
                                "sha256": res.get("sha256", meta.get("sha256", "")),
                            })
                            _write_json(meta_path, meta)
                            info = {"path": local_path, **res}
                        except Exception as e:
                            info = {"path": local_path, "error": str(e), "status": 0, "bytes": 0, "sha256": ""}

                    # status p/ UI
                    if info.get("error"):
                        st = "failed"
                    elif info.get("skipped") is True:
                        st = "skipped"
                    elif int(info.get("status", 0) or 0) == 304:
                        st = "skipped"
                    elif info.get("changed") is False:
                        st = "skipped"
                    else:
                        st = "ok"

                    item = {
                        "kind": "videos",
                        "name": key,
                        "status": st,
                        "path": info.get("path", local_path),
                        "bytes": int(info.get("bytes", 0) or 0),
                        "http": int(info.get("status", 0) or 0),
                        "sha256": info.get("sha256", ""),
                        "error": info.get("error", ""),
                    }
                    _job_add_items(sid, [item])

                    msg = f"[sync][videos] {key} -> {st} http={item['http']} bytes={item['bytes']}"
                    if item["error"]:
                        msg += f" err={item['error']}"
                    log(msg)

                    done_count += 1
                    _job_set(sid, progress=int(done_count * 100 / max(1, total_items)))

        # Finaliza
        with _SYNC_LOCK:
            job2 = _SYNC_JOBS.get(sid)
            if job2:
                _recalc_summary(job2)
                job2["done"] = True
                job2["ok"] = (job2["summary"]["failed"] == 0)
                job2["finished_at"] = _now_ts()

        with _SYNC_LOCK:
            job2 = _SYNC_JOBS.get(sid)
        if job2:
            s = job2["summary"]
            log(f"[sync] done id={sid} ok={job2['ok']} total={s['total']} ok={s['ok']} skipped={s['skipped']} failed={s['failed']}")

    except Exception as e:
        _job_set(sid, done=True, ok=False, finished_at=_now_ts(), error=str(e))
        log(f"[sync] FAIL id={sid} err={e}")

# =================== Auto-update UI (quando TTL vencer) ===================
_AUTO_UI_LOCK = threading.Lock()
_AUTO_UI = {
    "enabled": False,
    "running": False,
    "last_check": 0.0,
    "last_summary": {"total": 0, "ok": 0, "skipped": 0, "failed": 0},
    "last_error": "",
}
_AUTO_UI_TICK_S = 2.0  # loop leve; checagem real só quando TTL vence
_AUTO_UI_FILES = ["index.html", "esp.html", "style.css", "images/background.jpg", "favicon.ico"]

_AUTO_UI_THREAD = None

def _auto_ui_set_enabled(enabled: bool):
    with _AUTO_UI_LOCK:
        _AUTO_UI["enabled"] = bool(enabled)
    log(f"[auto-ui] enabled={bool(enabled)}")

def _auto_ui_status_dict():
    base, ttl, _ = _remote_assets_cfg()
    now = time.time()
    ttl = float(ttl or 0) or 3600.0
    with _AUTO_UI_LOCK:
        enabled = _AUTO_UI["enabled"]
        running = _AUTO_UI["running"]
        last = float(_AUTO_UI["last_check"] or 0.0)
        summ = _AUTO_UI["last_summary"]
        err = _AUTO_UI["last_error"]

    next_in = 0
    if enabled:
        if last > 0:
            next_in = max(0, int((last + ttl) - now))
        else:
            next_in = 0

    return {
        "enabled": enabled,
        "running": running,
        "ttl_s": int(ttl),
        "last_check_ts": last,
        "next_check_in_s": next_in,
        "base_url": base,
        "summary": summ,
        "error": err,
    }

def _auto_ui_worker():
    while True:
        time.sleep(_AUTO_UI_TICK_S)

        with _AUTO_UI_LOCK:
            enabled = _AUTO_UI["enabled"]
            running = _AUTO_UI["running"]
            last = float(_AUTO_UI["last_check"] or 0.0)

        if (not enabled) or running:
            continue

        base, ttl, _ = _remote_assets_cfg()
        if not base:
            continue

        ttl = float(ttl or 0) or 3600.0
        now = time.time()
        due = (last <= 0) or ((now - last) >= ttl)
        if not due:
            continue

        # marca já no começo p/ não martelar quando internet cair
        with _AUTO_UI_LOCK:
            if _AUTO_UI["running"]:
                continue
            _AUTO_UI["running"] = True
            _AUTO_UI["last_check"] = now

        try:
            summary = {"total": len(_AUTO_UI_FILES), "ok": 0, "skipped": 0, "failed": 0}

            for rel in _AUTO_UI_FILES:
                # respeita cache, mas como o worker só roda quando TTL venceu, forçamos a checagem remota
                data, src = _cached_remote_get_bytes(rel, force=False, respect_ttl=False)

                if data is None:
                    summary["failed"] += 1
                elif src == "cache":
                    summary["skipped"] += 1
                else:
                    summary["ok"] += 1

            with _AUTO_UI_LOCK:
                _AUTO_UI["last_summary"] = summary
                _AUTO_UI["last_error"] = ""

            log(f"[auto-ui] check ttl={int(ttl)}s -> ok={summary['ok']} skipped={summary['skipped']} failed={summary['failed']}")

        except Exception as e:
            with _AUTO_UI_LOCK:
                _AUTO_UI["last_error"] = str(e)
            log(f"[auto-ui] FAIL err={e}")

        finally:
            with _AUTO_UI_LOCK:
                _AUTO_UI["running"] = False

def _auto_ui_start_thread():
    global _AUTO_UI_THREAD
    if _AUTO_UI_THREAD and _AUTO_UI_THREAD.is_alive():
        return
    _AUTO_UI_THREAD = threading.Thread(target=_auto_ui_worker, daemon=True)
    _AUTO_UI_THREAD.start()



@app.get("/api/sync-progress")
def api_sync_progress():
    sid = (request.args.get("sync_id") or "").strip()
    if not sid:
        return jsonify(ok=False, error="sync_id ausente"), 400
    with _SYNC_LOCK:
        job = _SYNC_JOBS.get(sid)
        if not job:
            return jsonify(ok=False, error="sync_id nao encontrado"), 404
        # recalcula summary (barato)
        _recalc_summary(job)
        # evita conflito: ok (API) vs job['ok'] (resultado)
        job_out = dict(job)
        job_out['job_ok'] = job_out.pop('ok', None)
        return jsonify(ok=True, **job_out)

@app.get("/api/sync-last")
def api_sync_last():
    what = (request.args.get("what") or "all").strip().lower()
    if what not in ("ui", "videos", "all"):
        what = "all"
    with _SYNC_LOCK:
        return jsonify(ok=True, what=what, sync_id=_SYNC_LAST.get(what))

@app.post("/api/sync-remote")
def api_sync_remote():
    """Solicita sincronizacao do TVBox com arquivos remotos (GitHub) para cache/local.

    Body JSON (ou querystring):
      - what: "ui" | "videos" | "all"   (default: all)
      - force: true|false              (default: false)
      - respect_ttl: true|false        (default: false)
          * false = checa remoto mesmo se TTL nao venceu (ideal para sync manual).
      - wait: true|false               (default: false)
          * true = modo legado: a requisicao fica aberta ate terminar (sem sync_id).
    """
    j = request.get_json(silent=True) or {}
    what = (j.get("what") or request.args.get("what") or "all").strip().lower()
    if what not in ("ui", "videos", "all"):
        what = "all"

    force = bool(j.get("force", False))
    # compat: alguns clientes antigos podem mandar force_download
    if "force_download" in j:
        force = bool(j.get("force_download", False))

    respect_ttl = bool(j.get("respect_ttl", False))

    wait = bool(j.get("wait", False)) or (str(request.args.get("wait", "")).lower() in ("1", "true", "yes"))

    if wait:
        out = {"ok": True, "what": what, "force": force, "respect_ttl": respect_ttl, "wait": True}
        if what in ("ui", "all"):
            out["ui"] = sync_remote_ui(force=force, respect_ttl=respect_ttl)
        if what in ("videos", "all"):
            out["videos"] = sync_remote_videos(force=force, respect_ttl=respect_ttl)
        return jsonify(out)

    # modo async (progresso via /api/sync-progress)
    sid = _new_sync_job(what, force=force, respect_ttl=respect_ttl)
    th = threading.Thread(target=_run_sync_job, args=(sid,), daemon=True)
    th.start()
    return jsonify(ok=True, sync_id=sid, what=what, force=force, respect_ttl=respect_ttl, wait=False)




@app.get("/api/status")
def api_status():
    """
    Endpoint principal de estado normalizado do ESP.

    Problema que este endpoint resolve:
      - A UI pode chamar /api/status em loop (vários clientes).
      - Quando o ESP cai, não podemos ficar "espancando" o ESP com requisições concorrentes.
      - A UI precisa de um indicador confiável de Online/Offline (não pode ser baseado só em "ip existe").

    Regras:
      - Cache com gap mínimo (STATUS_CACHE) e backoff em caso de falha.
      - Anti-concorrência: apenas 1 thread faz a consulta real ao ESP por vez.
      - Compat: continua retornando HTTP 200 e {ok:true, state:{...}}; o online real fica em state.online.
    """
    now = time.time()

    # --- Fast path (cache / anti-flood) ---
    with STATUS_LOCK:
        min_gap = _status_min_gap(bool(STATUS_CACHE.get("ok", False)))
        age = now - STATUS_CACHE["ts"]

        # Se ainda está dentro da janela, responde do cache
        if age < min_gap:
            base = (STATUS_CACHE.get("state") or {}).copy()
            state_out = base.copy()
            state_out["ts"] = int(now)
            state_out["online"] = _is_online(now)
            state_out["fail_streak"] = int(_STATUS_META.get("fail_streak", 0))
            state_out["last_ok_ts"] = int(_STATUS_META.get("last_ok_ts", 0.0) or 0)
            state_out["last_fail_ts"] = int(_STATUS_META.get("last_fail_ts", 0.0) or 0)
            state_out["next_poll_in_s"] = round(max(0.0, min_gap - age), 2)
            return jsonify(ok=True, state=state_out)

        # Se recebemos push recente do ESP (sink), evitamos poll desnecessário.
        sink_ts = float(SINK_META.get("last_seen_ts", 0.0) or 0.0)
        if sink_ts > 0 and (now - sink_ts) <= ONLINE_GRACE_S:
            base = (STATUS_CACHE.get("state") or {}).copy()
            state_out = base.copy()
            state_out["ts"] = int(now)
            state_out["online"] = True
            state_out["via"] = base.get("via", "sink")
            state_out["fail_streak"] = int(_STATUS_META.get("fail_streak", 0))
            state_out["last_ok_ts"] = int(max(float(_STATUS_META.get("last_ok_ts", 0.0) or 0.0), sink_ts))
            state_out["last_fail_ts"] = int(_STATUS_META.get("last_fail_ts", 0.0) or 0)
            state_out["last_seen_ts"] = int(sink_ts)
            state_out["age_s"] = round(now - sink_ts, 2)
            state_out["poll_skipped"] = "sink_recent"
            return jsonify(ok=True, state=state_out)


        # Se outra requisição já está consultando o ESP, não duplica: devolve cache (mesmo que "velho")
        if _STATUS_INFLIGHT.get("running") and (now - float(_STATUS_INFLIGHT.get("started_ts", 0.0)) < 5.0):
            base = (STATUS_CACHE.get("state") or {}).copy()
            state_out = base.copy()
            state_out["ts"] = int(now)
            state_out["online"] = _is_online(now)
            state_out["fail_streak"] = int(_STATUS_META.get("fail_streak", 0))
            state_out["last_ok_ts"] = int(_STATUS_META.get("last_ok_ts", 0.0) or 0)
            state_out["last_fail_ts"] = int(_STATUS_META.get("last_fail_ts", 0.0) or 0)
            state_out["next_poll_in_s"] = 0.0
            state_out["poll_inflight"] = True
            return jsonify(ok=True, state=state_out)

        # Marca que esta requisição fará a consulta real
        _STATUS_INFLIGHT["running"] = True
        _STATUS_INFLIGHT["started_ts"] = now

    # --- Consulta real ao ESP (fora do lock) ---
    ok, res = proxy_get("state")

    # --- Atualiza cache/CFG ---
    with STATUS_LOCK:
        STATUS_CACHE["ts"] = now
        STATUS_CACHE["ok"] = ok
        _STATUS_INFLIGHT["running"] = False
        _STATUS_INFLIGHT["started_ts"] = 0.0

        _poll_transition(ok)

        if ok:
            _STATUS_META["last_ok_ts"] = now
            _STATUS_META["fail_streak"] = 0
        else:
            _STATUS_META["last_fail_ts"] = now
            _STATUS_META["fail_streak"] = int(_STATUS_META.get("fail_streak", 0)) + 1

    global CFG
    if ok and isinstance(res, dict):
        state = normalize_state(res)
        # Sincroniza mute_state em CFG (fonte única)
        with CFG_LOCK:
            if state.get("mute_state"):
                CFG["mute_state"] = state["mute_state"]
            state["mute_state"] = CFG.get("mute_state")
            state["ip"] = CFG.get("esp_ip", "")
            if (CFG.get("esp_state") or {}) != state:
                CFG["esp_state"] = state
                save_cfg(CFG, immediate=False)
    else:
        # Fallback para último estado salvo em disco
        with CFG_LOCK:
            state = CFG.get("esp_state") or {}
            if "ip" not in state and CFG.get("esp_ip"):
                state["ip"] = CFG["esp_ip"]

    with STATUS_LOCK:
        STATUS_CACHE["state"] = state

        # meta para o cliente (não persistimos no CFG para não poluir)
        min_gap = _status_min_gap(bool(STATUS_CACHE.get("ok", False)))
        age = now - STATUS_CACHE["ts"]
        next_in = max(0.0, min_gap - age)

        state_out = state.copy()
        state_out["ts"] = int(now)
        state_out["online"] = _is_online(now)
        state_out["fail_streak"] = int(_STATUS_META.get("fail_streak", 0))
        state_out["last_ok_ts"] = int(_STATUS_META.get("last_ok_ts", 0.0) or 0)
        state_out["last_fail_ts"] = int(_STATUS_META.get("last_fail_ts", 0.0) or 0)
        state_out["next_poll_in_s"] = round(next_in, 2)

    return jsonify(ok=True, state=state_out)


# =================== API: Sink de estado do ESP (push) ===================
_last_sink_hash = None
_last_sink_log_ts = 0.0
_sink_keepalive = 60.0  # logar 1x/min mesmo sem mudança


def _json_hash(obj) -> str:
    try:
        s = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    except Exception:
        s = str(obj)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()



@app.get("/api/time")
def api_time():
    """Relógio do servidor (para sincronizar o ESP e depurar horários)."""
    now = time.time()
    return jsonify(
        ok=True,
        epoch=int(now),
        iso=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        tz_offset_s=_local_tz_offset_s(),
    ), 200

@app.post("/api/esp-state-sink")
def esp_state_sink():
    """
    Endpoint para o ESP dar push do estado (telemetria).

    Metas:
      - Atualizar cache/CFG com estado normalizado
      - Responder MUITO rápido (sem escrita em disco no caminho crítico)
      - Fornecer ACK (seq) + horário do servidor para sincronização do ESP
    """
    global CFG, _last_sink_hash, _last_sink_log_ts

    t0 = time.time()
    remote = request.remote_addr or ""

    try:
        state_data = request.get_json(force=True, silent=True) or {}
        if not state_data:
            return jsonify(ok=False, error="No state data provided"), 400

        # O ESP manda {"state": {...}}. Aceitamos também payload direto.
        inner = None
        if isinstance(state_data, dict) and isinstance(state_data.get("state"), dict):
            inner = state_data.get("state")
        elif isinstance(state_data, dict):
            inner = state_data
        else:
            inner = None

        if not isinstance(inner, dict):
            return jsonify(ok=False, error="Invalid state payload"), 400

        seq = 0
        try:
            seq = int(inner.get("seq", 0) or 0)
        except Exception:
            seq = 0

        norm = normalize_state(inner)

        # marca Wi-Fi se tiver sinais relevantes
        if (norm.get("ssid") or norm.get("mac") or (norm.get("rssi_dbm") is not None)):
            norm["wifi"] = True

        # metadados (não quebram UI)
        norm["via"] = "sink"
        norm["local_ip"] = remote
        norm["server_seen_ts"] = int(t0)
        norm["esp_seq"] = seq

        changed = False

        # Atualiza CFG e padroniza mute_state (fonte única = CFG)
        with CFG_LOCK:
            if isinstance(norm.get("mute_state"), dict):
                CFG["mute_state"] = {
                    "QUIOSQUE": bool(norm["mute_state"].get("QUIOSQUE", False)),
                    "PISCINA":  bool(norm["mute_state"].get("PISCINA",  False)),
                }
            norm["mute_state"] = CFG.get("mute_state")

            # Detecta mudança para evitar gravação desnecessária
            h = _json_hash(norm)
            changed = (h != _last_sink_hash)
            if changed:
                _last_sink_hash = h

            CFG["esp_state"] = norm

        # Atualiza cache/online
        with STATUS_LOCK:
            STATUS_CACHE["state"] = norm
            STATUS_CACHE["ok"] = True
            STATUS_CACHE["ts"] = t0
            _STATUS_META["last_ok_ts"] = t0
            _STATUS_META["fail_streak"] = 0

            # meta do sink
            SINK_META["last_seen_ts"] = t0
            SINK_META["last_ip"] = remote
            SINK_META["last_seq"] = seq

        # Persistência: apenas quando mudou (debounce em background)
        if changed:
            save_cfg(CFG, immediate=False)

        # Logs: keepalive 1x/min, e mudança no máximo a cada 5s (evita flood)
        now = t0
        do_log = False
        if (now - _last_sink_log_ts) >= _sink_keepalive:
            do_log = True
        elif changed and (now - _last_sink_log_ts) >= 5.0:
            do_log = True

        if do_log:
            log(f"[ESP->SRV] sink ok from={remote} seq={seq} changed={1 if changed else 0}")
            _last_sink_log_ts = now

        # Loga se o request ficou lento (sinal de IO/lock)
        dt_ms = int((time.time() - t0) * 1000)
        if dt_ms >= 300:
            log(f"[ESP->SRV] sink SLOW {dt_ms}ms from={remote} seq={seq}")

        return jsonify(
            ok=True,
            ack_seq=seq,
            server_epoch=int(now),
            server_iso=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            tz_offset_s=_local_tz_offset_s(),
        ), 200

    except Exception as e:
        try:
            log(f"[ESP->SRV] sink FAIL from={remote} err={e}")
        except Exception:
            pass
        return jsonify(ok=False, error=str(e)), 500



# =================== API: Proxies genericos para ESP ===================
@app.route("/api/ir", methods=["POST"])
def ir_command():
    """
    Envia comando IR via ESP.

    Espera JSON:
      {
        "device": "QUIOSQUE" | "PISCINA" | ...,
        "command": "VOL_UP" | "VOL_DOWN" | "MUTE" | ...
      }

    Usado por:
      - index.html, card "Controles por ambientes":
          - botoes Mute (#btnMuteQuiosque, #btnMutePiscina)
          - logica de steps de volume (VOL_UP/VOL_DOWN via JS)
    """
    j = request.get_json(force=True, silent=True) or {}
    device = j.get("device")
    command = j.get("command")
    if not device or not command:
        return jsonify(ok=False, error="Parâmetros IR ausentes"), 400

    # pequenos alias no lado do servidor
    if command.upper() == "UP":
        command = "VOL_UP"
    if command.upper() == "DOWN":
        command = "VOL_DOWN"

    log(f"IR ENVIADO: Device={device}, Command={command}")

    # Controle de mute_state em CFG para QUIOSQUE/PISCINA (toggle MUTE)
    if command.upper() == "MUTE":
        mute_state = CFG.get("mute_state", {"QUIOSQUE": False, "PISCINA": False})
        prev_volumes = CFG.get("prev_volumes", {"QUIOSQUE": 15, "PISCINA": 15})
        is_muted = mute_state.get(device.upper(), False)
        if is_muted:
            mute_state[device.upper()] = False
            CFG["mute_state"] = mute_state
            save_cfg(CFG)
            log(f"Desmutando {device}.")
        else:
            current_volume = CFG.get("esp_state", {}).get("volumes", {}).get(device.lower(), 0)
            prev_volumes[device.upper()] = current_volume
            mute_state[device.upper()] = True
            CFG["mute_state"] = mute_state
            CFG["prev_volumes"] = prev_volumes
            save_cfg(CFG)
            log(f"Mutando {device}: Volume atual ({current_volume}) salvo.")

    ok, res = proxy_get("ir", params={"device": device, "command": command})
    return (jsonify(ok=True, response=res), 200) if ok else (jsonify(ok=False, error=res), 502)


@app.route("/api/gpio", methods=["POST"])
def gpio_command():
    """
    Envia comando GPIO para o ESP.

    Espera JSON:
      {
        "pin": 23 | 22 | 21 | 19 | ...,
        "state": 1/0 | "ON"/"OFF" | "HIGH"/"LOW" | "true"/"false"
      }

    Usado por:
      - index.html, card "Controle de acionamentos":
          - botoes #r1..#r4 (Painel de LED, Sistema de som, etc.)
      - /api/startshow   (liga R1/R2)
      - /api/stopshow    (desliga R1/R2)
    """
    j = request.get_json(force=True, silent=True) or {}
    pin = j.get("pin")
    state = j.get("state")
    if pin is None or state is None:
        return jsonify(ok=False, error="Parâmetros GPIO ausentes"), 400

    state_str = "ON" if str(state) in ("1", "ON", "HIGH", "True", "true") else "OFF"
    log(f"GPIO ENVIADO: Pin={pin}, State={state_str}")
    ok, res = proxy_get("gpio", params={"pin": pin, "state": state_str})
    return (jsonify(ok=True, response=res), 200) if ok else (jsonify(ok=False, error=res), 502)


@app.get("/api/status_esp32")
def status_esp32():
    """
    Endpoint de debug para status bruto do ESP (via /status do ESP).
    Não é usado diretamente por index.html/esp.html no momento.
    """
    ok, res = send_esp32_command("GET", "status")
    return (jsonify(ok=True, response=res), 200) if ok else (jsonify(ok=False, error=res), 502)


@app.post("/api/wifi_config")
def wifi_config_esp32():
    """
    Configura Wi-Fi do ESP via POST /wifi no ESP.

    Espera JSON:
      {
        "ssid": "<rede>",
        "password": "<senha>"
      }

    Atualmente reservado para uso futuro (não integrado no HTML).
    """
    j = request.get_json(force=True, silent=True) or {}
    ssid = j.get("ssid")
    password = j.get("password") or j.get("pass")
    if not ssid or not password:
        return jsonify(ok=False, error="SSID ou senha ausentes"), 400
    ok, res = send_esp32_command("POST", "wifi", {"ssid": ssid, "pass": password})
    return (jsonify(ok=True, response=res), 200) if ok else (jsonify(ok=False, error=res), 502)


# =================== API: Controle de "show" (Iniciar / Encerrar) ===================
@app.post("/api/startshow")
def start_show():
    """
    #Fluxo "Iniciar" (botao #btnStartShow em index.html, card "Aplicativos"):

    #Passos:
     # 1) Liga reles R1/R2 (Painel de LED / Sistema de som) via /api/gpio (proxy_get gpio 23/22 ON).
      #2) Dispara video de boas-vindas (bemvindo.mp4) no Android.
      #3) Agenda (via sleep + am start) a abertura do YouTube apos delay_s segundos.

    #Entrada JSON opcional:
      #{ "delay_s": 45 }  # padrao 45s
    """
    j = request.get_json(silent=True) or {}
    delay = int(j.get("delay_s", 45))  # padrão: 45s

    # 1) Liga relés R1/R2 (Painel de LED / Sistema de som)
    try:
        proxy_get("gpio", params={"pin": 23, "state": "ON"})  # r1
        proxy_get("gpio", params={"pin": 22, "state": "ON"})  # r2
        log("start_show: relés R1/R2 ligados.")
    except Exception as e:
        log(f"start_show: erro ao ligar relés -> {e}")
        # se falhar, o front ainda pode ligar manualmente; seguimos

    # 2) Executa o vídeo de boas-vindas
    ok_video, out_video, cmd_video = play_local_mp4(find_welcome_mp4_path())
    if not ok_video:
        log(f"start_show: erro ao executar vídeo bemvindo -> {out_video}")
    else:
        log("start_show: vídeo bemvindo.mp4 disparado com sucesso.")

    # 3) Agenda abrir YouTube em background (não bloqueia o Flask)
    cmd_timer = (
        f'sh -c "sleep {delay}; '
        'am start -a android.intent.action.VIEW -d https://www.youtube.com" &'
    )
    ok_timer, out_timer = run(cmd_timer, use_shell=True)
    if not ok_timer:
        log(f"start_show: erro ao agendar YouTube -> {out_timer}")
    else:
        log(f"start_show: YouTube agendado para daqui {delay}s.")

    ok_final = ok_video and ok_timer

    return (
        jsonify(
            ok=ok_final,
            video_ok=ok_video,
            scheduled=True,
            delay_s=delay,
            ran_video=cmd_video,
            ran_timer=cmd_timer,
        ),
        200 if ok_final else 500,
    )


@app.post("/api/stopshow")
def stop_show():
    """
    Fluxo "Encerrar" (botao #btnStopShow em index.html, card "Aplicativos"):

    Passos:
      1) Desliga R1/R2 via ESP (gpio 23/22 OFF) com delay entre eles.
      2) Envia HOME para o Android (volta para tela inicial).
    """
    j = request.get_json(silent=True) or {}
    delay_s = float(j.get("delay_s", 3.0))  # padrão: 3s

    relays_ok = True
    relays_error = None

    try:
        proxy_get("gpio", params={"pin": 23, "state": "OFF"})  # r1
        time.sleep(delay_s)                                   # <- delay aqui
        proxy_get("gpio", params={"pin": 22, "state": "OFF"})  # r2
    except Exception as e:
        relays_ok = False
        relays_error = str(e)
        # ainda assim tenta mandar HOME

    ok_home, out_home = run(
        "am start -a android.intent.action.MAIN -c android.intent.category.HOME",
        use_shell=True
    )

    return jsonify(
        ok=ok_home,
        ran="HOME",
        relays_ok=relays_ok,
        delay_s=delay_s,
        relays_error=relays_error
    ), (200 if ok_home else 500)

@app.route("/api/auto-update", methods=["GET", "POST"])
def api_auto_update():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        enabled = payload.get("enabled")

        if enabled is None:
            enabled = request.form.get("enabled") or request.args.get("enabled")

        enabled = str(enabled).strip().lower() in ("1", "true", "on", "yes", "y")

        cfg = load_cfg()
        cfg["auto_update_ui"] = enabled
        save_cfg(cfg)

        _auto_ui_set_enabled(enabled)
        _auto_ui_start_thread()

        return jsonify(ok=True, **_auto_ui_status_dict())

    # GET (status)
    return jsonify(ok=True, **_auto_ui_status_dict())


# =================== Main (entrada do servidor) ===================

if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    log("server start")

    cfg = load_cfg()
    _auto_ui_set_enabled(bool(cfg.get("auto_update_ui", False)))
    _auto_ui_start_thread()

    app.run(host="0.0.0.0", port=APP_PORT, debug=True, use_reloader=False)
