from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from .config import settings
from .crypto import encrypt_secret, decrypt_secret
from .db import SessionLocal
from .models import AppSetting, QuotaWindow, ServiceConnection, User, utcnow


router = APIRouter()
TASK_TYPES = {"generation", "fetch", "search", "tts", "stt", "sandbox", "connector"}
NUMERIC_SETTINGS = {
    "user_token_limit": (1_000, 100_000_000, settings.user_token_limit),
    "user_limit_window_seconds": (60, 604_800, settings.user_limit_window_seconds),
    "agent_max_steps": (1, 500, settings.agent_max_steps),
    "agent_max_runtime_seconds": (10, 7_200, settings.agent_max_runtime_seconds),
    "agent_max_parallel_tools": (1, 50, settings.agent_max_parallel_tools),
    "agent_max_tool_result_chars": (1_000, 100_000, settings.agent_max_tool_result_chars),
}
BOOLEAN_SETTINGS = {"tts_auto_reply", "shares_enabled"}


LOGIN_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>fablai admin</title>
<style>body{margin:0;background:#f4f1eb;color:#24211d;font:16px system-ui;display:grid;min-height:100vh;place-items:center}.box{width:min(360px,calc(100% - 40px));background:#fff;border:1px solid #ded8ce;border-radius:16px;padding:28px;box-sizing:border-box}h1{font-size:22px;margin:0 0 20px}input,button{width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc4b8;border-radius:9px;font:inherit}button{margin-top:12px;background:#28231f;color:#fff;cursor:pointer}</style></head>
<body><form class="box" method="post" action="/admin/login"><h1>fablai · Admin</h1><input name="password" type="password" autocomplete="current-password" placeholder="Пароль администратора" required><button type="submit">Войти</button></form></body></html>"""


ADMIN_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>fablai · Admin</title>
<style>
:root{color-scheme:light;--bg:#f4f1eb;--panel:#fff;--ink:#24211d;--muted:#716b62;--line:#e2ddd4;--accent:#5143a6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,sans-serif}
header{display:flex;align-items:center;justify-content:space-between;padding:18px max(20px,calc((100% - 1120px)/2));border-bottom:1px solid var(--line);background:var(--panel)}
header strong{font-size:18px}a{color:var(--accent)}main{max-width:1120px;margin:24px auto;padding:0 18px;display:grid;gap:18px}
section{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px}
h2{font-size:17px;margin:0 0 14px}p.note{color:var(--muted);margin:0 0 16px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.field{display:grid;gap:5px}.field.wide{grid-column:1/-1}
label{color:var(--muted);font-size:12px}input,select{width:100%;padding:10px 11px;border:1px solid #d6d0c6;border-radius:8px;background:#fff;color:var(--ink);font:inherit}
button{border:0;border-radius:8px;padding:10px 14px;background:#29251f;color:white;font:inherit;cursor:pointer}button.secondary{background:#eeeae3;color:#27231f}button.danger{background:#8c3939}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.tablewrap{overflow-x:auto}table{width:100%;border-collapse:collapse;margin-top:8px}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);white-space:nowrap}th{font-size:12px;color:var(--muted);font-weight:600}.status{display:inline-block;padding:3px 8px;border-radius:20px;background:#eeeae3}.status.on{background:#e0efe4;color:#235c35}
#message{color:var(--muted);min-height:20px}.muted{color:var(--muted)}@media(max-width:700px){.grid{grid-template-columns:1fr}section{padding:16px}header{padding:14px 18px}}
</style></head><body>
<header><strong>fablai / Admin</strong><a href="/admin/logout">Выйти</a></header>
<main>
<section><h2>Основная модель генерации</h2><p class="note">Модель ниже применяется ко всем запросам агента через подключения generation. Значение DEFAULT_MODEL из Render используется по умолчанию; выбор администратора имеет приоритет. Пустое поле возвращает значение Render, а если оно не задано — используются модели самих подключений.</p>
<form id="generationModelForm"><div class="grid"><div class="field wide"><label>Модель агента</label><input id="generationModel" name="generation_model" list="generationModelOptions" maxlength="200" placeholder="DEFAULT_MODEL из Render"><datalist id="generationModelOptions"></datalist><p class="note" id="generationModelStatus"></p></div></div><div class="actions"><button type="submit">Сохранить выбор модели</button><button type="button" class="secondary" id="resetGenerationModel">Использовать DEFAULT_MODEL из Render</button></div></form>
<form id="generationForm"><input type="hidden" name="task_type" value="generation"><div class="grid">
<div class="field"><label>Название подключения</label><input name="label" value="Основная модель" required></div>
<div class="field"><label>Модель подключения (резервная)</label><input name="model" placeholder="model-id" required></div>
<div class="field wide"><label>Base URL</label><input name="base_url" placeholder="https://provider.example/v1" required></div>
<div class="field wide"><label>API key</label><input name="api_key" type="password" autocomplete="new-password" placeholder="Новый ключ; пустое поле не меняет сохранённый ключ"></div>
<div class="field"><label>Параллельные запросы</label><input name="max_parallel" type="number" min="1" max="50" value="2"></div>
<div class="field"><label>Приоритет (меньше — раньше)</label><input name="priority" type="number" value="1"></div>
</div><div class="actions"><button type="submit">Сохранить основную модель</button></div></form>
</section>

<section><h2>Дополнительные подключения</h2><p class="note">Отдельные пулы Fetch, Search, TTS, STT, Sandbox и Connector. В каждом пуле можно иметь несколько ключей и адресов, отключать отдельные подключения и задавать их лимиты.</p>
<form id="connectionForm"><input type="hidden" name="id" value=""><div class="grid">
<div class="field"><label>Тип задачи</label><select name="task_type"><option value="fetch">Fetch — отдельная модель извлечения страниц</option><option value="search">Search — SearXNG</option><option value="tts">TTS — синтез речи</option><option value="stt">STT — распознавание речи</option><option value="sandbox">Sandbox — внешний runner</option><option value="connector">Connector — дополнительный API</option><option value="generation">Generation — дополнительный ключ модели</option></select></div>
<div class="field"><label>Название</label><input name="label" required></div>
<div class="field wide"><label>Base URL</label><input name="base_url" required placeholder="https://provider.example/v1"></div>
<div class="field"><label>Модель / ID сервиса</label><input name="model" placeholder="model-id"></div>
<div class="field"><label>API key / Bearer secret</label><input name="api_key" type="password" autocomplete="new-password" placeholder="Пусто — оставить текущий ключ"></div>
<div class="field"><label>Параллельные запросы</label><input name="max_parallel" type="number" min="1" max="50" value="2"></div>
<div class="field"><label>Лимит запросов за 12 ч (0 — без лимита)</label><input name="request_limit_12h" type="number" min="0" value="0"></div>
<div class="field"><label>Приоритет</label><input name="priority" type="number" value="100"></div>
<div class="field"><label>Таймаут, секунд</label><input name="timeout_seconds" type="number" min="5" max="1800" value="120"></div>
<div class="field"><label>Включено</label><select name="enabled"><option value="true">Да</option><option value="false">Нет</option></select></div>
<div class="field wide"><label>Дополнительные параметры JSON (например, voice / response_format для TTS)</label><input name="options" placeholder='{"voice":"alloy","response_format":"mp3"}'></div>
</div><div class="actions"><button type="submit">Сохранить подключение</button><button type="button" class="secondary" id="clearForm">Очистить форму</button></div></form>
<div class="tablewrap"><table><thead><tr><th>Тип</th><th>Название</th><th>Модель</th><th>URL</th><th>Лимит / 12 ч</th><th>Статус</th><th>Действия</th></tr></thead><tbody id="connections"></tbody></table></div>
</section>

<section><h2>Лимиты и поведение агента</h2><p class="note">Стандартный лимит пользователя установлен на 1 000 000 токенов за скользящие 12 часов. Основные и Fetch-вызовы учитываются в одном пользовательском лимите.</p>
<form id="settingsForm"><div class="grid">
<div class="field"><label>Токены на пользователя</label><input name="user_token_limit" type="number" min="1000" max="100000000"></div>
<div class="field"><label>Окно лимита, секунд</label><input name="user_limit_window_seconds" type="number" min="60" max="604800"></div>
<div class="field"><label>Максимум шагов агента</label><input name="agent_max_steps" type="number" min="1" max="500"></div>
<div class="field"><label>Максимальное время задачи, секунд</label><input name="agent_max_runtime_seconds" type="number" min="10" max="7200"></div>
<div class="field"><label>Параллельные вызовы инструментов</label><input name="agent_max_parallel_tools" type="number" min="1" max="50"></div>
<div class="field"><label>Максимум символов на результат инструмента</label><input name="agent_max_tool_result_chars" type="number" min="1000" max="100000"></div>
<div class="field"><label>Автоматически отправлять TTS после ответа</label><select name="tts_auto_reply"><option value="false">Нет</option><option value="true">Да</option></select></div>
<div class="field"><label>Создание публичных ссылок</label><select name="shares_enabled"><option value="true">Включено</option><option value="false">Выключено</option></select></div>
</div><div class="actions"><button type="submit">Сохранить настройки</button></div></form>
</section>
<section><h2>Использование</h2><div class="tablewrap"><table><thead><tr><th>Telegram ID</th><th>Пользователь</th><th>Израсходовано</th><th>Лимит</th><th>Окончание окна</th></tr></thead><tbody id="usage"></tbody></table></div></section>
<div id="message"></div>
</main>
<script>
const $=s=>document.querySelector(s);
const msg=t=>{$('#message').textContent=t||''};
const json=async(url,options={})=>{const r=await fetch(url,{headers:{'Content-Type':'application/json',...(options.headers||{})},...options});if(!r.ok)throw new Error((await r.text()).slice(0,300));return r.json()};
const formObj=f=>Object.fromEntries(new FormData(f).entries());
async function load(){
 const data=await json('/admin/api/connections');
 const body=$('#connections');body.replaceChildren();
 for(const c of data.connections){
  const tr=document.createElement('tr');
  const cells=[c.task_type,c.label,c.model||'—',c.base_url,c.request_limit_12h||'без лимита',c.enabled?'Включено':'Выключено'];
  cells.forEach(v=>{const td=document.createElement('td');td.textContent=String(v);tr.appendChild(td)});
  const td=document.createElement('td');
  const edit=document.createElement('button');edit.className='secondary';edit.textContent='Изменить';edit.onclick=()=>editConnection(c);
  const toggle=document.createElement('button');toggle.className='secondary';toggle.textContent=c.enabled?'Выключить':'Включить';toggle.onclick=()=>toggleConnection(c);
  const del=document.createElement('button');del.className='danger';del.textContent='Удалить';del.onclick=()=>deleteConnection(c.id);
  td.append(edit,toggle,del);tr.appendChild(td);body.appendChild(tr);
 }
 const s=await json('/admin/api/settings');
 for(const [key,value] of Object.entries(s.settings)){const el=$('#settingsForm [name="'+key+'"]');if(el)el.value=String(value)}
 $('#generationModel').value=s.settings.generation_model||'';
 const choices=new Set([s.settings.default_model,...data.connections.filter(c=>c.task_type==='generation').map(c=>c.model)].filter(Boolean));
 const options=$('#generationModelOptions');options.replaceChildren();
 for(const model of choices){const option=document.createElement('option');option.value=model;options.appendChild(option)}
 const active=s.settings.effective_generation_model||'модель из подключения';
 $('#generationModelStatus').textContent='Сейчас используется: '+active+(s.settings.generation_model?' (выбор администратора)':(s.settings.default_model?' (Render DEFAULT_MODEL)':' (резервная модель подключения)'))+'.';
 const usage=await json('/admin/api/usage');const ub=$('#usage');ub.replaceChildren();
 for(const u of usage.users){const tr=document.createElement('tr');[u.telegram_id,u.username||u.first_name||'—',u.used_tokens,u.limit_tokens,u.window_ends_at||'—'].forEach(v=>{const td=document.createElement('td');td.textContent=String(v);tr.appendChild(td)});ub.appendChild(tr)}
}
function editConnection(c){
 const f=$('#connectionForm');for(const [k,v] of Object.entries(c)){const e=f.elements[k];if(e)e.value=typeof v==='boolean'?String(v):String(v??'')}
 f.elements.api_key.value='';f.scrollIntoView({behavior:'smooth'});
}
async function toggleConnection(c){try{await json('/admin/api/connections/'+c.id,{method:'PATCH',body:JSON.stringify({enabled:!c.enabled})});await load()}catch(e){msg(e.message)}}
async function deleteConnection(id){if(!confirm('Удалить подключение?'))return;try{await json('/admin/api/connections/'+id,{method:'DELETE'});await load()}catch(e){msg(e.message)}}
$('#clearForm').onclick=()=>$('#connectionForm').reset();
$('#connectionForm').onsubmit=async e=>{e.preventDefault();try{const f=e.target;const d=formObj(f);d.enabled=d.enabled==='true';d.options=d.options?JSON.parse(d.options):{};const id=d.id;delete d.id;await json('/admin/api/connections'+(id?'/'+id:''),{method:id?'PATCH':'POST',body:JSON.stringify(d)});f.reset();f.elements.max_parallel.value='2';f.elements.timeout_seconds.value='120';msg('Подключение сохранено.');await load()}catch(err){msg(err.message)}};
$('#generationForm').onsubmit=async e=>{e.preventDefault();try{const d=formObj(e.target);d.max_parallel=Number(d.max_parallel);d.priority=Number(d.priority);const current=await json('/admin/api/connections?task_type=generation');const primary=current.connections.find(x=>x.label===d.label)||current.connections.find(x=>x.task_type==='generation');if(primary)await json('/admin/api/connections/'+primary.id,{method:'PATCH',body:JSON.stringify(d)});else await json('/admin/api/connections',{method:'POST',body:JSON.stringify(d)});msg('Основная модель сохранена.');await load()}catch(err){msg(err.message)}};
$('#generationModelForm').onsubmit=async e=>{e.preventDefault();try{const d=formObj(e.target);await json('/admin/api/settings',{method:'PUT',body:JSON.stringify({generation_model:d.generation_model||''})});msg('Модель генерации сохранена.');await load()}catch(err){msg(err.message)}};
$('#resetGenerationModel').onclick=async()=>{try{await json('/admin/api/settings',{method:'PUT',body:JSON.stringify({generation_model:''})});msg('Используется DEFAULT_MODEL из Render.');await load()}catch(err){msg(err.message)}};
$('#settingsForm').onsubmit=async e=>{e.preventDefault();try{const d=formObj(e.target);for(const k of Object.keys(d)){if(['tts_auto_reply','shares_enabled'].includes(k))d[k]=d[k]==='true';else d[k]=Number(d[k])}await json('/admin/api/settings',{method:'PUT',body:JSON.stringify(d)});msg('Настройки сохранены.');await load()}catch(err){msg(err.message)}};
load().catch(e=>msg(e.message));
</script></body></html>"""


async def require_admin(request: Request) -> None:
    if not request.session.get("fablai_admin"):
        raise HTTPException(status_code=401, detail="Admin authentication required")
    origin = request.headers.get("origin")
    if origin and request.method not in {"GET", "HEAD", "OPTIONS"}:
        parsed = urlsplit(origin)
        if parsed.netloc and parsed.netloc != request.url.netloc:
            raise HTTPException(status_code=403, detail="Cross-origin request rejected")


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if request.session.get("fablai_admin"):
        return HTMLResponse(ADMIN_HTML)
    return HTMLResponse(LOGIN_HTML)


@router.post("/admin/login")
async def admin_login(request: Request):
    if not settings.admin_password:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD is not configured")
    form = await request.form()
    password = str(form.get("password") or "")
    if not secrets.compare_digest(password, settings.admin_password):
        return HTMLResponse(LOGIN_HTML, status_code=401)
    request.session["fablai_admin"] = True
    return RedirectResponse("/admin", status_code=303)


@router.get("/admin/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin", status_code=303)


def _connection_json(row: ServiceConnection) -> dict:
    try:
        options = json.loads(row.options_json or "{}")
    except (TypeError, ValueError):
        options = {}
    return {
        "id": row.id,
        "task_type": row.task_type,
        "label": row.label,
        "base_url": row.base_url,
        "model": row.model,
        "api_key_configured": bool(row.encrypted_api_key),
        "enabled": row.enabled,
        "priority": row.priority,
        "max_parallel": row.max_parallel,
        "request_limit_12h": row.request_limit_12h,
        "timeout_seconds": row.timeout_seconds,
        "options": options,
    }


def _clean_connection(data: dict, existing: ServiceConnection | None = None) -> dict:
    task_type = str(data.get("task_type", existing.task_type if existing else "")).strip().lower()
    if task_type not in TASK_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported task_type")
    label = str(data.get("label", existing.label if existing else "")).strip()[:100]
    base_url = str(data.get("base_url", existing.base_url if existing else "")).strip().rstrip("/")
    if not label or not base_url:
        raise HTTPException(status_code=422, detail="label and base_url are required")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="base_url must be a valid HTTP(S) URL without credentials")
    def integer(key, default, low, high):
        try:
            value = int(data.get(key, getattr(existing, key) if existing else default))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail=key + " must be an integer")
        if not low <= value <= high:
            raise HTTPException(status_code=422, detail=key + " is outside the supported range")
        return value
    options = data.get("options", None)
    if options is None and existing:
        options_json = existing.options_json
    else:
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise HTTPException(status_code=422, detail="options must be an object")
        options_json = json.dumps(options, ensure_ascii=False)[:4000]
    return {
        "task_type": task_type,
        "label": label,
        "base_url": base_url,
        "model": str(data.get("model", existing.model if existing else "") or "").strip()[:200],
        "enabled": bool(data.get("enabled", existing.enabled if existing else True)),
        "priority": integer("priority", 100, -10000, 10000),
        "max_parallel": integer("max_parallel", 2, 1, 50),
        "request_limit_12h": integer("request_limit_12h", 0, 0, 1_000_000),
        "timeout_seconds": integer("timeout_seconds", 120, 5, 1800),
        "options_json": options_json,
    }


@router.get("/admin/api/connections", dependencies=[Depends(require_admin)])
async def list_connections(task_type: str | None = None):
    async with SessionLocal() as db:
        stmt = select(ServiceConnection).order_by(ServiceConnection.task_type, ServiceConnection.priority, ServiceConnection.id)
        if task_type:
            if task_type not in TASK_TYPES:
                raise HTTPException(status_code=422, detail="Unsupported task_type")
            stmt = stmt.where(ServiceConnection.task_type == task_type)
        rows = (await db.execute(stmt)).scalars().all()
        return {"connections": [_connection_json(row) for row in rows]}


@router.post("/admin/api/connections", dependencies=[Depends(require_admin)])
async def create_connection(request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="JSON object expected")
    values = _clean_connection(data)
    key = str(data.get("api_key") or "").strip()
    if key:
        values["encrypted_api_key"] = encrypt_secret(key)
    else:
        values["encrypted_api_key"] = ""
    async with SessionLocal() as db:
        row = ServiceConnection(**values)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return {"connection": _connection_json(row)}


@router.patch("/admin/api/connections/{connection_id}", dependencies=[Depends(require_admin)])
async def update_connection(connection_id: int, request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="JSON object expected")
    async with SessionLocal() as db:
        row = await db.get(ServiceConnection, connection_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        values = _clean_connection(data, row)
        for key, value in values.items():
            setattr(row, key, value)
        api_key = str(data.get("api_key") or "").strip()
        if api_key:
            row.encrypted_api_key = encrypt_secret(api_key)
        await db.commit()
        await db.refresh(row)
        return {"connection": _connection_json(row)}


@router.delete("/admin/api/connections/{connection_id}", dependencies=[Depends(require_admin)])
async def delete_connection(connection_id: int):
    async with SessionLocal() as db:
        row = await db.get(ServiceConnection, connection_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        await db.delete(row)
        await db.commit()
    return {"ok": True}


@router.get("/admin/api/settings", dependencies=[Depends(require_admin)])
async def read_settings():
    async with SessionLocal() as db:
        rows = (await db.execute(select(AppSetting))).scalars().all()
    values = {
        "user_token_limit": settings.user_token_limit,
        "user_limit_window_seconds": settings.user_limit_window_seconds,
        "agent_max_steps": settings.agent_max_steps,
        "agent_max_runtime_seconds": settings.agent_max_runtime_seconds,
        "agent_max_parallel_tools": settings.agent_max_parallel_tools,
        "agent_max_tool_result_chars": settings.agent_max_tool_result_chars,
        "generation_model": "",
        "default_model": str(settings.default_model or "").strip()[:200],
        "effective_generation_model": "",
        "tts_auto_reply": False,
        "shares_enabled": True,
    }
    for row in rows:
        if row.key == "generation_model":
            values["generation_model"] = str(row.value or "").strip()[:200]
        elif row.key in NUMERIC_SETTINGS:
            try:
                values[row.key] = int(row.value)
            except ValueError:
                pass
        elif row.key in BOOLEAN_SETTINGS:
            values[row.key] = row.value.lower() in {"1", "true", "yes", "on"}
    values["effective_generation_model"] = values["generation_model"] or values["default_model"]
    return {"settings": values}


@router.put("/admin/api/settings", dependencies=[Depends(require_admin)])
async def write_settings(request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="JSON object expected")
    normalized: dict[str, str] = {}
    for key, (low, high, _default) in NUMERIC_SETTINGS.items():
        if key in data:
            try:
                value = int(data[key])
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail=key + " must be an integer")
            if not low <= value <= high:
                raise HTTPException(status_code=422, detail=key + " is outside the supported range")
            normalized[key] = str(value)
    for key in BOOLEAN_SETTINGS:
        if key in data:
            if type(data[key]) is not bool:
                raise HTTPException(status_code=422, detail=key + " must be a boolean")
            normalized[key] = "true" if data[key] else "false"
    if "generation_model" in data:
        if not isinstance(data["generation_model"], str):
            raise HTTPException(status_code=422, detail="generation_model must be a string")
        generation_model = data["generation_model"].strip()
        if len(generation_model) > 200 or any(ord(char) < 32 for char in generation_model):
            raise HTTPException(status_code=422, detail="generation_model must be at most 200 characters without control characters")
        normalized["generation_model"] = generation_model
    async with SessionLocal() as db:
        for key, value in normalized.items():
            row = await db.get(AppSetting, key)
            if row is None:
                db.add(AppSetting(key=key, value=value))
            else:
                row.value = value
                row.updated_at = utcnow()
        await db.commit()
    return {"ok": True}


@router.get("/admin/api/usage", dependencies=[Depends(require_admin)])
async def usage_overview():
    async with SessionLocal() as db:
        users = (await db.execute(
            select(User, QuotaWindow).outerjoin(QuotaWindow, QuotaWindow.user_id == User.telegram_id)
            .order_by(User.created_at.desc()).limit(100)
        )).all()
    return {"users": [
        {
            "telegram_id": user.telegram_id,
            "username": user.username,
            "first_name": user.first_name,
            "used_tokens": quota.used_tokens if quota else 0,
            "limit_tokens": quota.limit_tokens if quota else settings.user_token_limit,
            "window_ends_at": quota.window_ends_at.isoformat() if quota else None,
        }
        for user, quota in users
    ]}
