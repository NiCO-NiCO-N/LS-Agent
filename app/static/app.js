'use strict';

/* ============================================================
 *  ls agent 前端（无构建、无依赖）
 * ============================================================ */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  config: null,
  tools: [],
  skills: [],
  sessions: [],
  session: { id: null, title: '新对话', messages: [] },
  providerId: null,
  streaming: false,
  controller: null,
  tab: 'models',
  skillEditing: null,
  skillCreating: false,
  logLevel: '',
  logAuto: false,
};

let live = null;
let logTimer = null;

function stopLogAuto() {
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
}

/* ------------------------------ 基础工具 ------------------------------ */
function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
  if (!res.ok) {
    let msg = res.statusText;
    if (data && data.detail) {
      msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    } else if (typeof data === 'string' && data) {
      msg = data;
    }
    throw new Error(msg);
  }
  return data;
}

let toastTimer = null;
function toast(message, kind) {
  const el = $('#toast');
  el.textContent = message;
  el.className = 'toast' + (kind === 'error' ? ' error' : '');
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === 'error' ? 6000 : 2800);
}

/* ------------------------------ Markdown ------------------------------ */
function inlineMd(source) {
  const codes = [];
  let text = String(source).replace(/`([^`\n]+)`/g, (m, code) => {
    codes.push('<code>' + esc(code) + '</code>');
    return '\u0001' + (codes.length - 1) + '\u0001';
  });
  text = esc(text);
  text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/(^|[^*\w])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  text = text.replace(/\u0001(\d+)\u0001/g, (m, i) => codes[Number(i)] || '');
  return text;
}

function mdToHtml(source) {
  if (!source) return '';
  const text = String(source).replace(/\r\n/g, '\n');

  const blocks = [];
  const withFences = text.replace(/```([\w+-]*)\n([\s\S]*?)```/g, (m, lang, code) => {
    blocks.push('<pre class="code"><code>' + esc(code.replace(/\n$/, '')) + '</code></pre>');
    return '\n\u0000' + (blocks.length - 1) + '\u0000\n';
  });

  const lines = withFences.split('\n');
  let html = '';
  let listType = null;
  let para = [];

  const flushPara = () => {
    if (para.length) {
      html += '<p>' + para.map(inlineMd).join('<br>') + '</p>';
      para = [];
    }
  };
  const closeList = () => {
    if (listType) { html += '</' + listType + '>'; listType = null; }
  };

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) { flushPara(); closeList(); continue; }

    const codeMatch = line.match(/^\u0000(\d+)\u0000$/);
    if (codeMatch) {
      flushPara(); closeList();
      html += blocks[Number(codeMatch[1])] || '';
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      flushPara(); closeList();
      const level = heading[1].length;
      html += '<h' + level + '>' + inlineMd(heading[2]) + '</h' + level + '>';
      continue;
    }

    if (/^(---+|\*\*\*+|___+)$/.test(line)) {
      flushPara(); closeList(); html += '<hr>'; continue;
    }

    if (line.startsWith('> ')) {
      flushPara(); closeList();
      html += '<blockquote>' + inlineMd(line.slice(2)) + '</blockquote>';
      continue;
    }

    const bullet = line.match(/^[-*+]\s+(.*)$/);
    if (bullet) {
      flushPara();
      if (listType !== 'ul') { closeList(); html += '<ul>'; listType = 'ul'; }
      html += '<li>' + inlineMd(bullet[1]) + '</li>';
      continue;
    }

    const ordered = line.match(/^\d+[.)]\s+(.*)$/);
    if (ordered) {
      flushPara();
      if (listType !== 'ol') { closeList(); html += '<ol>'; listType = 'ol'; }
      html += '<li>' + inlineMd(ordered[1]) + '</li>';
      continue;
    }

    para.push(line);
  }

  flushPara();
  closeList();
  return html;
}

function prettyJson(raw) {
  if (raw == null || raw === '') return '{}';
  if (typeof raw === 'object') return JSON.stringify(raw, null, 2);
  try { return JSON.stringify(JSON.parse(raw), null, 2); } catch (e) { return String(raw); }
}

/* ------------------------------ 启动 ------------------------------ */
async function boot() {
  bindStaticEvents();
  try {
    await loadConfig();
    await Promise.all([loadTools(), loadSkills(), loadSessions()]);
  } catch (e) {
    toast('初始化失败：' + e.message, 'error');
  }
  renderProviderSelect();
  renderSessions();
  renderCounts();
  updateHeader();
  if (state.sessions.length) {
    await openSession(state.sessions[0].id);
  } else {
    newChat();
  }
  autosize();
  ensureDraft();

  // 深链接：?tab=logs 或 #logs 可直接打开对应面板，方便贴给同事「直接看日志」
  const initialTab = (new URLSearchParams(location.search).get('tab') || location.hash || '')
    .replace('#', '')
    .toLowerCase();
  if (['models', 'general', 'tools', 'skills', 'logs'].includes(initialTab)) openModal(initialTab);
}

async function loadConfig() {
  state.config = await api('GET', '/api/config');
  state.providerId = state.providerId || state.config.active_provider;
  const hint = $('#workspaceHint');
  hint.textContent = '工作区 ' + (state.config.workspace || '');
  hint.title = state.config.workspace || '';
  applyBrand();
}

async function refreshConfig() {
  await loadConfig();
  applyBrand();
  renderProviderSelect();
  renderCounts();
  updateHeader();
}

/* 让品牌名跟着配置走：标题、侧栏文字、Logo 字母 */
function applyBrand() {
  const name = (state.config && state.config.assistant_name) || 'ls agent';
  const initials = (name.replace(/\s+/g, '').slice(0, 2) || 'ls').toLowerCase();
  document.title = name;
  const brand = $('.brand-text strong');
  if (brand) brand.textContent = name;
  const logo = $('.brand .logo');
  if (logo) logo.textContent = initials;
  const emptyLogo = $('.empty-logo');
  if (emptyLogo) emptyLogo.textContent = initials;
}

async function loadTools() { state.tools = await api('GET', '/api/tools'); }
async function loadSkills() { state.skills = await api('GET', '/api/skills'); }
async function loadSessions() { state.sessions = await api('GET', '/api/sessions'); }

function renderCounts() {
  const enabled = (state.skills || []).filter((s) => s.enabled).length;
  $('#skillCount').textContent = String(enabled);
  if (state.config) $('#providerCount').textContent = String(state.config.providers.length);
  paintEmpty();
}

/* 空状态里的三格读数：当前模型 / 工具数 / 启用技能数 */
function paintEmpty() {
  const cfg = state.config || {};
  const provs = cfg.providers || [];
  const prov = provs.find((p) => p.id === state.providerId) || provs[0];
  const model = $('#bootModel');
  if (model) {
    if (!prov) {
      model.textContent = '未配置';
    } else if (prov.type === 'mock') {
      model.textContent = 'demo · ' + (prov.model || 'mock');
    } else {
      model.textContent = prov.model || prov.name || '—';
    }
    model.title = prov ? prov.name + ' · ' + (prov.model || '未设模型') : '';
  }
  const tools = $('#bootTools');
  if (tools) tools.textContent = state.tools ? String(state.tools.length) : '—';
  const skills = $('#bootSkills');
  if (skills) {
    const all = state.skills || [];
    skills.textContent = all.length ? all.filter((s) => s.enabled).length + ' / ' + all.length : '0';
  }
}

function renderProviderSelect() {
  const select = $('#providerSelect');
  if (!state.config) return;
  const provs = state.config.providers || [];
  select.innerHTML = provs.map((p) =>
    '<option value="' + esc(p.id) + '">' + esc(p.name) + ' · ' + esc(p.model || '未设模型') + '</option>'
  ).join('');
  if (!provs.some((p) => p.id === state.providerId)) {
    state.providerId = provs.length ? provs[0].id : null;
  }
  if (state.providerId) select.value = state.providerId;
  paintEmpty();
}

function updateHeader() {
  $('#chatTitle').textContent = (state.session && state.session.title) || '新对话';
  const provider = (state.config?.providers || []).find((p) => p.id === state.providerId);
  const enabled = (state.skills || []).filter((s) => s.enabled).length;
  if (!provider) {
    $('#chatMeta').textContent = '未选择模型，请在设置中配置';
    return;
  }
  const mode = provider.type === 'mock' ? '演示模式，未接入真实模型' : (provider.model || '未设置模型');
  $('#chatMeta').textContent = provider.name + ' · ' + mode + ' · ' + enabled + ' 个技能启用';
}

/* ------------------------------ 会话 ------------------------------ */
function renderSessions() {
  const box = $('#sessionList');
  if (!state.sessions.length) {
    box.innerHTML = '<div class="hint" style="padding:8px">还没有对话记录</div>';
    return;
  }
  box.innerHTML = state.sessions.map((s) => {
    const active = state.session && state.session.id === s.id ? ' active' : '';
    const time = s.updated_at ? new Date(s.updated_at * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
    return '<div class="session-item' + active + '" data-id="' + esc(s.id) + '">' +
      '<div class="st">' + esc(s.title || '未命名对话') + '</div>' +
      '<div class="sp">' + esc(time + (s.preview ? ' · ' + s.preview : '')) + '</div>' +
      '<button class="sdel" data-del="' + esc(s.id) + '" title="删除">✕</button>' +
      '</div>';
  }).join('');
}

async function openSession(id) {
  try {
    const session = await api('GET', '/api/sessions/' + id);
    state.session = session;
    if (session.provider_id) state.providerId = session.provider_id;
    renderProviderSelect();
    renderSessions();
    updateHeader();
    renderMessageList();
  } catch (e) {
    toast('打开会话失败：' + e.message, 'error');
  }
}

function newChat() {
  state.session = { id: null, title: '新对话', messages: [] };
  live = null;
  renderMessageList();
  renderSessions();
  updateHeader();
  $('#input').focus();
}

async function deleteSession(id) {
  if (!window.confirm('删除这个对话？')) return;
  try {
    await api('DELETE', '/api/sessions/' + id);
    if (state.session && state.session.id === id) newChat();
    await loadSessions();
    renderSessions();
    toast('已删除');
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function clearCurrentChat() {
  if (!state.session || !state.session.id) { newChat(); return; }
  if (!window.confirm('清空当前对话的所有消息？')) return;
  try {
    await api('POST', '/api/sessions/' + state.session.id + '/clear');
    await openSession(state.session.id);
    toast('已清空');
  } catch (e) {
    toast(e.message, 'error');
  }
}

/* ------------------------------ 消息渲染 ------------------------------ */
/* 头像：方框 + 两条反向圆弧 + 中间状态字 + 框外正上方的名字 */
const AVATAR_META = {
  user: { name: '你', state: '已发送' },
  assistant: { name: 'AI', state: '就绪' },
  error: { name: '系统', state: '异常' },
};

function avatarHtml(role, state) {
  const meta = AVATAR_META[role] || AVATAR_META.assistant;
  return '<div class="avatar">' +
    '<span class="ava-name">' + meta.name + '</span>' +
    '<span class="ava-arc a"></span><span class="ava-arc b"></span>' +
    '<span class="ava-state">' + (state || meta.state) + '</span>' +
    '</div>';
}

/* 更新某条消息头像上的状态（思考中 / 输出中 / 调用中 …） */
function setAvatarState(msgEl, state, active) {
  if (!msgEl) return;
  const av = msgEl.querySelector('.avatar');
  if (!av) return;
  const st = av.querySelector('.ava-state');
  if (st && state) st.textContent = state;
  av.classList.toggle('active', !!active);
}

/* 回车呼出确认框：核对要发出去的内容 */
function openSendDialog() {
  const input = $('#input');
  const text = (input.value || '').trim();
  if (!text || state.streaming) return;
  const wrap = document.createElement('div');
  wrap.className = 'send-dialog';
  wrap.innerHTML = '<div class="sd-card">' +
    '<div class="sd-head">UPLINK // 确认发送</div>' +
    '<div class="sd-body"></div>' +
    '<div class="sd-foot"><span class="sd-hint">Enter 发送 · Esc 取消</span>' +
    '<button class="btn ghost" data-act="cancel">取消</button>' +
    '<button class="btn primary" data-act="send">发送</button></div></div>';
  wrap.querySelector('.sd-body').textContent = text;
  document.body.appendChild(wrap);
  const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey); };
  const doSend = () => { close(); sendMessage(); };
  wrap.addEventListener('click', (e) => {
    if (e.target === wrap || e.target.closest('[data-act="cancel"]')) { close(); input.focus(); return; }
    if (e.target.closest('[data-act="send"]')) doSend();
  });
  const onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); close(); input.focus(); }
    else if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); doSend(); }
  };
  document.addEventListener('keydown', onKey);
  wrap.querySelector('[data-act="send"]').focus();
}

/* 对话区里直接写：草稿气泡本身就是可编辑区域（contenteditable），输入法正常 */
function ensureDraft() {
  if (state.streaming) return null;
  const box = $('#messages');
  if (!box) return null;
  let el = box.querySelector('.msg.draft');
  if (el) return el;
  el = document.createElement('div');
  el.className = 'msg user draft';
  el.innerHTML = avatarHtml('user', '输入中') +
    '<div class="bubble"><div class="content" contenteditable="true" spellcheck="false"></div></div>';
  box.appendChild(el);
  setAvatarState(el, '输入中', true);
  const ed = el.querySelector('.content');
  ed.addEventListener('input', syncDraft);
  ed.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      syncDraft();
      if ($('#input').value.trim()) sendMessage();
    }
  });
  return el;
}

function focusDraft() {
  ensureDraft();
  const ed = document.querySelector('.msg.draft .content');
  if (ed) { ed.focus(); const r = document.createRange(); r.selectNodeContents(ed); r.collapse(false);
    const s = window.getSelection(); s.removeAllRanges(); s.addRange(r); }
}

/* 可编辑区 → 隐藏输入框（sendMessage 仍从 #input 取值） */
function syncDraft() {
  const ed = document.querySelector('.msg.draft .content');
  if (!ed) return;
  $('#input').value = ed.innerText.replace(/\n$/, '');
  autosize();
  scrollBottom();
}

/* 发送/清空后同步清掉草稿里的字 */
function paintDraft() {
  const ed = document.querySelector('.msg.draft .content');
  if (!ed) return;
  if (!$('#input').value.trim()) ed.textContent = '';
}

function messageEl(role, content) {  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role;
  wrap.innerHTML = avatarHtml(role) +
    '<div class="bubble"><div class="content"></div></div>';
  wrap.querySelector('.content').innerHTML = mdToHtml(content);
  return wrap;
}

/* 把输出切成带行号的行：每行两列，长行换行后仍对齐在文字列 */
function numberLines(text, emptyText) {
  const body = String(text == null ? '' : text).replace(/\s+$/, '');
  if (!body) return '<div class="ln-empty">' + esc(emptyText || '（无输出）') + '</div>';
  const pad = String(body.split('\n').length).length;
  return body.split('\n').map((line, i) => {
    const no = String(i + 1).padStart(pad, ' ');
    const tx = esc(line);
    return '<div class="ln"><span class="no">' + no + '</span><span class="tx">' + (tx || '&#8203;') + '</span></div>';
  }).join('');
}

function toolCardEl(name, args, result) {
  const el = document.createElement('details');
  const okClass = result ? (result.ok === false ? ' err' : ' done') : ' running';
  const status = result ? (result.ok === false ? '失败' : '完成') : '运行中…';
  el.className = 'tool-card' + okClass;
  el.innerHTML =
    '<summary><span class="dot"></span><span class="tool-name">' + esc(name) + '</span>' +
    '<span class="tool-act"></span>' +
    '<span class="tool-state">' + status + '</span></summary>' +
    '<div class="tool-body">' +
    '<div class="tool-label">参数</div><pre class="args">' + esc(prettyJson(args)) + '</pre>' +
    '<div class="tool-label">结果</div><div class="out">' +
    (result ? numberLines(result.content) : '<div class="ln-empty">等待返回…</div>') +
    '</div>' +
    '</div>';
  return el;
}

function assistantEl(message, results) {
  const wrap = document.createElement('div');
  wrap.className = 'msg assistant';
  wrap.innerHTML = avatarHtml('assistant') + '<div class="bubble">' +
    '<div class="reason" hidden></div><div class="tools"></div><div class="content"></div></div>';
  const bubble = wrap.querySelector('.bubble');

  if (message.reasoning) {
    const reason = wrap.querySelector('.reason');
    reason.hidden = false;
    reason.innerHTML = '<details><summary>思考过程</summary><div class="hint">' + esc(message.reasoning) + '</div></details>';
  }

  const toolsBox = wrap.querySelector('.tools');
  (message.tool_calls || []).forEach((call) => {
    const name = (call.function && call.function.name) || 'tool';
    const args = (call.function && call.function.arguments) || '{}';
    toolsBox.appendChild(toolCardEl(name, args, results.get(call.id)));
  });

  const content = wrap.querySelector('.content');
  content.innerHTML = mdToHtml(message.content || '');
  if (!message.content && !(message.tool_calls || []).length) {
    content.innerHTML = '<p class="hint">（模型没有返回内容）</p>';
  }
  if (!bubble) return wrap;
  return wrap;
}

function renderMessageList() {
  const box = $('#messages');
  $$('.msg, .notice', box).forEach((el) => el.remove());

  const messages = (state.session && state.session.messages) || [];
  const results = new Map();
  messages.forEach((m) => {
    if (m.role === 'tool') results.set(m.tool_call_id, { ok: m.ok !== false, content: m.content });
  });

  messages.forEach((m) => {
    if (m.role === 'user') box.appendChild(messageEl('user', m.content));
    else if (m.role === 'assistant') box.appendChild(assistantEl(m, results));
  });

  $('#emptyState').hidden = messages.length > 0;
  scrollBottom();
}

function scrollBottom() {
  const box = $('#messages');
  if (!box) return;
  box.scrollTo({ top: box.scrollHeight, behavior: 'instant' });
}

function appendNotice(text) {
  const el = document.createElement('div');
  el.className = 'notice';
  el.textContent = text;
  $('#messages').appendChild(el);
  scrollBottom();
}

function appendError(text) {
  const el = document.createElement('div');
  el.className = 'msg error';
  el.innerHTML = avatarHtml('error') + '<div class="bubble"><div class="content">' + esc(text) + '</div></div>';
  $('#messages').appendChild(el);
  scrollBottom();
}

/* ------------------------------ 流式气泡 ------------------------------ */
function startLive() {
  const el = document.createElement('div');
  el.className = 'msg assistant';
  el.innerHTML = avatarHtml('assistant', '思考中') + '<div class="bubble">' +
    '<div class="reason" hidden></div><div class="tools"></div><div class="content"></div></div>';
  $('#messages').appendChild(el);
  setAvatarState(el, '思考中', true);
  live = { el, text: '', reasoning: '', tools: {}, streaming: true, finished: false };
  paintLive();
  scrollBottom();
  return live;
}

function ensureLive() {
  if (!live || live.finished) startLive();
  return live;
}

function paintLive() {
  if (!live) return;
  const content = live.el.querySelector('.content');
  content.innerHTML = mdToHtml(live.text) + (live.streaming ? '<span class="cursor"></span>' : '');
  const reason = live.el.querySelector('.reason');
  if (live.reasoning) {
    reason.hidden = false;
    reason.innerHTML = '<details><summary>思考过程</summary><div class="hint" style="white-space:pre-wrap">' +
      esc(live.reasoning) + '</div></details>';
  }
  scrollBottom();
}

function setStreaming(on) {
  state.streaming = on;
  $('#btnSend').disabled = on;
  $('#btnStop').hidden = !on;
  $('#input').disabled = on;
  // 响应期间收起输入草稿，等这一轮结束再出现
  if (on) {
    const d = document.querySelector('.msg.draft');
    if (d) d.remove();
  } else {
    ensureDraft(); focusDraft();
  }
}

/* ------------------------------ 事件分发 ------------------------------ */
function handleEvent(event) {
  switch (event.type) {
    case 'session':
      if (state.session) state.session.id = event.session_id;
      break;

    case 'step':
      if (live && !live.finished) {
        live.finished = true;
        live.streaming = false;
        setAvatarState(live.el, '就绪', false);
        paintLive();
      }
      startLive();
      break;

    case 'delta':
      ensureLive();
      live.text += event.text || '';
      setAvatarState(live.el, '输出中', true);
      paintLive();
      break;

    case 'reasoning':
      ensureLive();
      live.reasoning += event.text || '';
      setAvatarState(live.el, '思考中', true);
      paintLive();
      break;

    case 'assistant': {
      const message = event.message || {};
      ensureLive();
      live.text = message.content || live.text;
      live.streaming = false;
      live.finished = false;
      setAvatarState(live.el, '思考中', true);
      paintLive();
      break;
    }

    case 'tool_call': {
      const cur = ensureLive();
      setAvatarState(cur.el, '调用中', true);
      const box = cur.el.querySelector('.tools');
      const card = toolCardEl(event.name, event.arguments, null);
      card.dataset.id = event.id;
      box.appendChild(card);
      live.tools[event.id] = card;
      scrollBottom();
      break;
    }

    case 'tool_result': {
      const card = live && live.tools[event.id];
      if (!card) break;
      card.classList.remove('running');
      card.classList.add(event.ok ? 'done' : 'err');
      card.querySelector('.tool-state').textContent = event.ok ? '完成' : '失败';
      if (event.action) {
        const act = card.querySelector('.tool-act');
        if (act) act.textContent = event.action;
      }
      card.querySelector('.out').innerHTML = numberLines(event.result || '');
      setAvatarState(live.el, '思考中', true);
      scrollBottom();
      break;
    }

    case 'ticket': {
      upsertTicket(event);
      break;
    }
    case 'notice':
      appendNotice(event.message || '');
      break;

    case 'error':
      appendError(event.message || '未知错误');
      toast(event.message || '调用失败', 'error');
      break;

    default:
      break;
  }
}

/* ------------------------------ 发送 ------------------------------ */
async function sendMessage() {
  if (state.streaming) return;
  const input = $('#input');
  let text = input.value.trim();
  if (!text && !ATTACH.length) return;
  if (ATTACH.length) {
    const refs = ATTACH.map((a) => '- ' + a.path).join('\n');
    text = (text ? text + '\n\n' : '') + '附件（已存入工作区，可用 read_file 读取）：\n' + refs;
    ATTACH = [];
    renderAtts();
  }

  if (!state.config) { toast('配置还没加载完成', 'error'); return; }

  input.value = '';
  autosize();
  paintDraft();

  if (!state.session || !state.session.id) {
    try {
      state.session = await api('POST', '/api/sessions', {
        title: '新对话',
        provider_id: state.providerId,
      });
      await loadSessions();
    } catch (e) {
      toast('新建会话失败：' + e.message, 'error');
      return;
    }
  }

  state.session.messages = state.session.messages || [];
  state.session.messages.push({ role: 'user', content: text });
  renderMessageList();
  $('#emptyState').hidden = true;
  ensureDraft(); focusDraft();

  setStreaming(true);
  live = null;
  const controller = new AbortController();
  state.controller = controller;

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: state.session.id,
        message: text,
        provider_id: state.providerId,
      }),
      signal: controller.signal,
    });

    if (!res.ok) {
      const raw = await res.text();
      throw new Error(raw || ('HTTP ' + res.status));
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let index;
      while ((index = buffer.indexOf('\n\n')) >= 0) {
        const chunk = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data:')) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          try {
            handleEvent(JSON.parse(payload));
          } catch (e) {
            /* 忽略无法解析的片段 */
          }
        }
      }
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      toast('已停止生成');
      if (live) { live.streaming = false; paintLive(); }
    } else {
      toast(err.message || String(err), 'error');
    }
  } finally {
    setStreaming(false);
    state.controller = null;
    if (live) { live.streaming = false; setAvatarState(live.el, '就绪', false); paintLive(); }
    if (state.session && state.session.id) {
      try {
        state.session = await api('GET', '/api/sessions/' + state.session.id);
        renderMessageList();
      } catch (e) { /* 忽略刷新失败 */ }
      await loadSessions();
      renderSessions();
    }
    updateHeader();
    input.focus();
  }
}

/* ------------------------------ 弹窗 ------------------------------ */
function openModal(tab) {
  state.tab = tab || 'models';
  state.skillEditing = null;
  state.skillCreating = false;
  $('#modal').hidden = false;
  renderModal();
}

function closeModal() {
  stopLogAuto();
  $('#modal').hidden = true;
  state.skillEditing = null;
  state.skillCreating = false;
}

async function renderModal() {
  const tabs = [['models', '模型'], ['general', '通用'], ['tools', '工具'], ['skills', '技能'], ['logs', '日志']];
  $('#tabs').innerHTML = tabs.map(([id, label]) =>
    '<button class="tab ' + (state.tab === id ? 'active' : '') + '" data-tab="' + id + '">' + label + '</button>'
  ).join('');

  if (state.tab !== 'logs') stopLogAuto();

  const body = $('#modalBody');
  if (state.tab === 'models') renderModels(body);
  else if (state.tab === 'general') renderGeneral(body);
  else if (state.tab === 'tools') renderTools(body);
  else if (state.tab === 'logs') renderLogs(body);
  else await renderSkills(body);
}

/* ---------- 模型 ---------- */
function providerCard(p, isNew) {
  const active = state.config && p.id === state.config.active_provider;
  return '<div class="provider-card' + (active ? ' active' : '') + '"' +
    (isNew ? ' data-new="1"' : ' data-id="' + esc(p.id) + '"') + '>' +
    '<div class="pc-head">' +
      '<input type="radio" name="activeProvider" value="' + esc(p.id) + '" ' + (active ? 'checked' : '') + (isNew ? ' disabled' : '') + ' title="设为当前模型">' +
      '<input type="text" class="f-name" value="' + esc(p.name) + '" placeholder="显示名称">' +
      (active ? '<span class="tag">使用中</span>' : '') +
      '<button class="icon-btn" data-act="del" title="' + (isNew ? '取消' : '删除') + '">✕</button>' +
    '</div>' +
    '<div class="grid3">' +
      '<label class="field">类型<select class="f-type">' +
        ['openai', 'anthropic', 'mock'].map((t) =>
          '<option value="' + t + '"' + ((p.type || 'openai') === t ? ' selected' : '') + '>' + t + '</option>'
        ).join('') +
      '</select></label>' +
      '<label class="field">模型<input class="f-model" value="' + esc(p.model || '') + '" placeholder="gpt-4o-mini"></label>' +
      '<label class="field">温度<input class="f-temp" type="number" step="0.1" min="0" max="2" value="' + (p.temperature ?? 0.7) + '"></label>' +
    '</div>' +
    '<div class="grid3" style="margin-top:10px">' +
      '<label class="field" style="grid-column:span 2">Base URL<input class="f-base" value="' + esc(p.base_url || '') + '" placeholder="https://api.openai.com/v1"></label>' +
      '<label class="field">max_tokens<input class="f-tokens" type="number" min="64" value="' + (p.max_tokens || 2048) + '"></label>' +
    '</div>' +
    '<div class="grid2" style="margin-top:10px">' +
      '<label class="field">API Key<input class="f-key" type="password" placeholder="' + (p.has_key ? '已保存，留空则不变' : 'sk-...') + '"></label>' +
      '<label class="field">Key 环境变量（可选）<input class="f-env" value="' + esc(p.api_key_env || '') + '" placeholder="OPENAI_API_KEY"></label>' +
    '</div>' +
    '<div class="pc-actions">' +
      '<button class="btn small" data-act="models">预览模型</button>' +
      '<button class="btn small" data-act="test">测试连接</button>' +
      '<button class="btn primary small" data-act="save">保存</button>' +
    '</div>' +
  '</div>';
}

function readProviderCard(card) {
  return {
    name: $('.f-name', card).value.trim(),
    type: $('.f-type', card).value,
    model: $('.f-model', card).value.trim(),
    base_url: $('.f-base', card).value.trim(),
    api_key: $('.f-key', card).value.trim(),
    api_key_env: $('.f-env', card).value.trim(),
    temperature: Number($('.f-temp', card).value) || 0.7,
    max_tokens: Number($('.f-tokens', card).value) || 2048,
  };
}

function renderModels(body) {
  const provs = state.config.providers || [];
  body.innerHTML =
    '<div class="panel-title"><h3>模型提供商</h3>' +
      '<div style="display:flex;align-items:center;gap:10px">' +
        '<span class="hint">OpenAI 兼容 / Claude / Ollama</span>' +
        '<button class="btn small" id="addProvider">+ 新增</button>' +
      '</div>' +
    '</div>' +
    provs.map((p) => providerCard(p, false)).join('') +
    '<div class="hint">API Key 仅保存在本机 <code>data/config.json</code>，接口不会回显明文。' +
    'Ollama 填 <code>http://127.0.0.1:11434/v1</code>，Key 随便填。</div>';

  $('#addProvider').onclick = () => {
    const wrap = document.createElement('div');
    wrap.innerHTML = providerCard({
      id: '', name: '新模型', type: 'openai', base_url: 'https://api.openai.com/v1',
      model: 'gpt-4o-mini', temperature: 0.7, max_tokens: 2048, has_key: false, api_key_env: '',
    }, true);
    const card = wrap.firstElementChild;
    body.insertBefore(card, body.lastElementChild);
    bindProviderCard(card);
    $('.f-name', card).focus();
  };

  $$('.provider-card', body).forEach((card) => {
    if (!card.dataset.new) bindProviderCard(card);
  });

  $$('input[name="activeProvider"]', body).forEach((radio) => {
    radio.onchange = async () => {
      try {
        await api('PUT', '/api/config', { active_provider: radio.value });
        state.providerId = radio.value;
        if (state.session && state.session.id) {
          await api('PATCH', '/api/sessions/' + state.session.id, { provider_id: radio.value });
        }
        await refreshConfig();
        renderModal();
        toast('已切换模型');
      } catch (e) { toast(e.message, 'error'); }
    };
  });
}

function bindProviderCard(card) {
  const isNew = card.dataset.new === '1';
  const id = card.dataset.id;

  $('[data-act="del"]', card).onclick = async () => {
    if (isNew) { card.remove(); return; }
    if (!window.confirm('删除该模型提供商？')) return;
    try {
      await api('DELETE', '/api/providers/' + id);
      await refreshConfig();
      renderModal();
      toast('已删除');
    } catch (e) { toast(e.message, 'error'); }
  };

  $('[data-act="models"]', card).onclick = async () => {
    const btn = $('[data-act="models"]', card);
    const payload = readProviderCard(card);
    if (!payload.base_url) { toast('请先填写 Base URL', 'error'); return; }
    const old = card.querySelector('.model-list');
    if (old) old.remove();
    btn.disabled = true;
    btn.textContent = '拉取中…';
    try {
      const res = await api('POST', '/api/providers/models', {
        base_url: payload.base_url,
        type: payload.type,
        api_key: payload.api_key,
        api_key_env: payload.api_key_env,
        provider_id: isNew ? '' : id,
      });
      const box = document.createElement('div');
      box.className = 'model-list';
      box.innerHTML = res.ok
        ? '<span class="ml-tip">' + esc(res.message || '') + ' · 点一下填入模型</span>' +
          res.models.map((m) =>
            '<button class="ml-item" data-model="' + esc(m) + '">' + esc(m) + '</button>').join('')
        : '<span class="ml-err">' + esc(res.message || '拉取失败') + '</span>';
      card.querySelector('.pc-actions').before(box);
      $$('.ml-item', box).forEach((b) => {
        b.onclick = () => {
          $('.f-model', card).value = b.dataset.model;
          toast('已填入 ' + b.dataset.model);
        };
      });
      if (!res.ok) toast(res.message || '拉取失败', 'error');
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = '预览模型';
    }
  };

  $('[data-act="test"]', card).onclick = async () => {
    if (isNew) { toast('请先保存再测试', 'error'); return; }
    const btn = $('[data-act="test"]', card);
    btn.disabled = true;
    btn.textContent = '测试中…';
    try {
      const result = await api('POST', '/api/providers/' + id + '/test');
      toast((result.ok ? '连接成功：' : '连接失败：') + (result.message || ''), result.ok ? '' : 'error');
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = '测试连接';
    }
  };

  $('[data-act="save"]', card).onclick = async () => {
    const payload = readProviderCard(card);
    try {
      if (isNew) {
        if (!payload.name) { toast('请填写名称', 'error'); return; }
        await api('POST', '/api/providers', payload);
      } else {
        await api('PUT', '/api/providers/' + id, payload);
      }
      await refreshConfig();
      renderModal();
      toast('已保存');
    } catch (e) { toast(e.message, 'error'); }
  };
}

/* ---------- 通用 ---------- */
function renderGeneral(body) {
  body.innerHTML =
    '<div class="panel-title"><h3>通用设置</h3></div>' +
    '<div class="grid2">' +
      '<label class="field">助手名称<input id="assistantName" value="' + esc(state.config.assistant_name || 'ls agent') + '"></label>' +
      '<label class="field">工作区（只读）<input value="' + esc(state.config.workspace || '') + '" readonly></label>' +
    '</div>' +
    '<label class="field" style="margin-top:12px">系统提示词<textarea id="sysPrompt" rows="10">' + esc(state.config.system_prompt || '') + '</textarea></label>' +
    '<div class="grid2" style="margin-top:12px">' +
      '<label class="field">最大工具步数<input id="maxSteps" type="number" min="1" max="30" value="' + (state.config.max_steps || 8) + '"></label>' +
      '<label class="field">日志级别<select id="logLevelCfg">' +
        ['DEBUG', 'INFO', 'WARNING', 'ERROR'].map((l) =>
          '<option value="' + l + '"' + ((state.config.log_level || 'INFO') === l ? ' selected' : '') + '>' + l + '</option>'
        ).join('') +
      '</select></label>' +
    '</div>' +
    '<div class="grid2" style="margin-top:10px">' +
      '<label class="field">日志文件（相对项目根，留空=不写文件）<input id="logFile" value="' + esc(state.config.log_file || '') + '" placeholder="data/logs/ls-agent.log"></label>' +
      '<label class="field">单文件上限（MB，超出后轮转）<input id="logMaxMb" type="number" min="1" max="50" value="' + (state.config.log_max_mb || 2) + '"></label>' +
    '</div>' +
    '<div class="grid2" style="margin-top:10px">' +
      '<label class="field">保留历史份数<input id="logBackups" type="number" min="0" max="20" value="' + (state.config.log_backups ?? 5) + '"></label>' +
      '<div class="field" style="justify-content:flex-end;gap:2px">' +
        '<label class="log-check"><input type="checkbox" id="logConsole"' + (state.config.log_console === false ? '' : ' checked') + '> 同时输出到控制台</label>' +
        '<label class="log-check"><input type="checkbox" id="logPrompts"' + (state.config.log_prompts ? ' checked' : '') + '> 日志中记录对话原文（默认只记长度）</label>' +
      '</div>' +
    '</div>' +
    '<div class="row-actions" style="margin-top:14px"><button class="btn primary" id="saveGeneral">保存设置</button></div>';

  $('#saveGeneral').onclick = async () => {
    try {
      await api('PUT', '/api/config', {
        assistant_name: $('#assistantName').value.trim() || 'ls agent',
        system_prompt: $('#sysPrompt').value,
        max_steps: Number($('#maxSteps').value) || 8,
        log_level: $('#logLevelCfg').value,
        log_file: $('#logFile').value.trim(),
        log_max_mb: Number($('#logMaxMb').value) || 2,
        log_backups: Number($('#logBackups').value) || 0,
        log_console: $('#logConsole').checked,
        log_prompts: $('#logPrompts').checked,
      });
      await refreshConfig();
      toast('已保存');
    } catch (e) { toast(e.message, 'error'); }
  };
}

/* ---------- 日志 ---------- */
const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR'];

function logRow(entry) {
  const lvl = String(entry.level || 'INFO').toLowerCase();
  return '<div class="log-row lvl-' + lvl + '">' +
    '<span class="log-time">' + esc(entry.time) + '</span>' +
    '<span class="log-level">' + esc(entry.level) + '</span>' +
    '<span class="log-rid">' + esc(entry.rid) + '</span>' +
    '<span class="log-src">' + esc(entry.source) + '</span>' +
    '<span class="log-msg">' + esc(entry.message) + '</span>' +
  '</div>';
}

function renderLogs(body) {
  const levelOptions = [['', '全部级别']].concat(LOG_LEVELS.map((l) => [l, l + ' 及以上']));
  body.innerHTML =
    '<div class="panel-title"><h3>运行日志</h3><span class="hint" id="logMeta">加载中…</span></div>' +
    '<div class="log-toolbar">' +
      '<select class="select" id="logLevelSel">' +
        levelOptions.map(([v, label]) =>
          '<option value="' + v + '"' + (state.logLevel === v ? ' selected' : '') + '>' + label + '</option>'
        ).join('') +
      '</select>' +
      '<input id="logSearch" placeholder="搜索关键字">' +
      '<label class="log-check"><input type="checkbox" id="logAuto"' + (state.logAuto ? ' checked' : '') + '> 自动刷新</label>' +
      '<button class="btn small" id="logRefresh">刷新</button>' +
      '<button class="btn small" id="logClear">清空</button>' +
      '<a class="btn small" id="logDownload" href="/api/logs/download">下载文件</a>' +
    '</div>' +
    '<div class="log-list" id="logList"><div class="hint">加载中…</div></div>';

  const list = $('#logList', body);

  const refresh = async () => {
    if (!document.body.contains(list)) { stopLogAuto(); return; }
    try {
      const params = new URLSearchParams({ limit: '300' });
      if (state.logLevel) params.set('level', state.logLevel);
      const keyword = ($('#logSearch', body) || {}).value || '';
      if (keyword.trim()) params.set('search', keyword.trim());
      const data = await api('GET', '/api/logs?' + params.toString());
      const meta = $('#logMeta');
      if (meta) {
        meta.textContent = (data.entries || []).length + ' 条 · 级别 ' + (data.level || 'INFO') +
          ' · ' + (data.file ? data.file + '（' + Math.round((data.file_size || 0) / 1024) + ' KB）' : '未开启文件日志');
      }
      list.innerHTML = (data.entries || []).length
        ? data.entries.map(logRow).join('')
        : '<div class="hint">这里还没有日志。发一条消息，或把级别调到 DEBUG 再看。</div>';
      list.scrollTop = list.scrollHeight;
    } catch (e) {
      list.innerHTML = '<div class="hint">读取失败：' + esc(e.message) + '</div>';
    }
  };

  $('#logLevelSel', body).onchange = (e) => { state.logLevel = e.target.value; refresh(); };
  $('#logSearch', body).oninput = () => { clearTimeout(state.logSearchTimer); state.logSearchTimer = setTimeout(refresh, 250); };
  $('#logRefresh', body).onclick = refresh;
  $('#logDownload', body).onclick = () => toast('开始下载日志文件');
  $('#logClear', body).onclick = async () => {
    if (!confirm('清空内存里的日志并截断日志文件？此操作不可恢复。')) return;
    try {
      await api('DELETE', '/api/logs');
      toast('日志已清空');
      refresh();
    } catch (e) { toast(e.message, 'error'); }
  };
  $('#logAuto', body).onchange = (e) => {
    state.logAuto = e.target.checked;
    stopLogAuto();
    if (state.logAuto) logTimer = setInterval(refresh, 3000);
  };

  stopLogAuto();
  if (state.logAuto) logTimer = setInterval(refresh, 3000);
  refresh();
}

/* ---------- 工具 ---------- */
function renderTools(body) {
  const disabled = new Set(state.config.disabled_tools || []);
  body.innerHTML =
    '<div class="panel-title"><h3>工具</h3><span class="hint">取消勾选即禁用该工具</span></div>' +
    state.tools.map((t) =>
      '<label class="skill-row" style="cursor:pointer">' +
        '<input type="checkbox" data-tool="' + esc(t.name) + '" ' + (disabled.has(t.name) ? '' : 'checked') + '>' +
        '<div class="s-main">' +
          '<div class="s-name">' + esc(t.label || t.name) + ' <span class="pill">' + esc(t.name) + '</span></div>' +
          '<div class="s-desc">' + esc(t.description) + '</div>' +
        '</div>' +
      '</label>'
    ).join('') +
    '<div class="row-actions" style="margin-top:12px"><button class="btn primary" id="saveTools">保存</button></div>';

  $('#saveTools').onclick = async () => {
    const off = $$('input[data-tool]', body).filter((el) => !el.checked).map((el) => el.dataset.tool);
    try {
      await api('PUT', '/api/config', { disabled_tools: off });
      await refreshConfig();
      toast('已保存工具开关');
    } catch (e) { toast(e.message, 'error'); }
  };
}

/* ---------- 技能 ---------- */
async function renderSkills(body) {
  body.innerHTML = '<div class="hint">加载中…</div>';
  try {
    state.skills = await api('GET', '/api/skills');
  } catch (e) {
    body.innerHTML = '<div class="hint">加载失败：' + esc(e.message) + '</div>';
    return;
  }

  if (state.skillEditing) { renderSkillEditor(body); return; }
  if (state.skillCreating) { renderSkillCreator(body); return; }

  const enabledCount = state.skills.filter((s) => s.enabled).length;
  const rows = state.skills.length
    ? state.skills.map((s) =>
        '<div class="skill-row" data-slug="' + esc(s.slug) + '">' +
          '<label class="switch"><input type="checkbox" data-toggle="' + esc(s.slug) + '" ' + (s.enabled ? 'checked' : '') + '><span></span></label>' +
          '<div class="s-main">' +
            '<div class="s-name">' + esc(s.name) + ' <span class="pill">' + esc(s.slug) + '</span></div>' +
            '<div class="s-desc">' + esc(s.description || '（无描述）') + '</div>' +
            '<div class="s-meta">' + (s.tags?.length ? '# ' + esc(s.tags.join(' # ')) + ' · ' : '') +
              (s.scripts?.length ? '脚本：' + esc(s.scripts.join(', ')) : '无脚本') +
              (s.always ? ' · 始终注入' : '') + '</div>' +
          '</div>' +
          '<div class="s-actions">' +
            '<button class="btn small" data-edit="' + esc(s.slug) + '">编辑</button>' +
            '<button class="btn small" data-remove="' + esc(s.slug) + '">删除</button>' +
          '</div>' +
        '</div>'
      ).join('')
    : '<div class="hint" style="padding:10px">还没有技能。新建一个，或在 <code>skills/</code> 目录下放入 SKILL.md。</div>';

  body.innerHTML =
    '<div class="panel-title"><h3>技能（' + enabledCount + '/' + state.skills.length + ' 已启用）</h3>' +
      '<button class="btn small primary" id="newSkill">+ 新建技能</button></div>' +
    rows +
    '<div class="hint" style="margin-top:10px">技能 = <code>skills/&lt;slug&gt;/SKILL.md</code>。启用后描述会进入系统提示词，模型按需调用 <code>load_skill</code> 读取正文。</div>';

  $('#newSkill').onclick = () => { state.skillCreating = true; renderSkills(body); };

  $$('input[data-toggle]', body).forEach((input) => {
    input.onchange = async () => {
      try {
        await api('POST', '/api/skills/' + input.dataset.toggle + '/toggle', { enabled: input.checked });
        await loadSkills();
        renderCounts();
        updateHeader();
        toast(input.checked ? '已启用技能' : '已停用技能');
      } catch (e) {
        toast(e.message, 'error');
        input.checked = !input.checked;
      }
    };
  });

  $$('[data-edit]', body).forEach((btn) => {
    btn.onclick = () => { state.skillEditing = btn.dataset.edit; renderSkills(body); };
  });

  $$('[data-remove]', body).forEach((btn) => {
    btn.onclick = async () => {
      if (!window.confirm('删除技能 ' + btn.dataset.remove + '？')) return;
      try {
        await api('DELETE', '/api/skills/' + btn.dataset.remove);
        await loadSkills();
        renderCounts();
        renderSkills(body);
        toast('已删除');
      } catch (e) { toast(e.message, 'error'); }
    };
  });
}

function renderSkillEditor(body) {
  const skill = state.skills.find((s) => s.slug === state.skillEditing);
  if (!skill) { state.skillEditing = null; renderSkills(body); return; }

  body.innerHTML =
    '<div class="panel-title"><h3>' + esc(skill.name) + ' <span class="pill">' + esc(skill.slug) + '</span></h3>' +
      '<span class="hint">编辑 SKILL.md（含 frontmatter）</span></div>' +
    '<div class="editor">' +
      '<textarea id="skillContent" spellcheck="false">' + esc(skill.content) + '</textarea>' +
      '<div class="row-actions">' +
        '<button class="btn" id="cancelEdit">返回</button>' +
        '<button class="btn primary" id="saveSkill">保存</button>' +
      '</div>' +
    '</div>';

  $('#cancelEdit').onclick = () => { state.skillEditing = null; renderSkills(body); };
  $('#saveSkill').onclick = async () => {
    try {
      await api('PUT', '/api/skills/' + skill.slug, { content: $('#skillContent').value });
      state.skillEditing = null;
      await loadSkills();
      await refreshConfig();
      await renderSkills(body);
      toast('技能已保存');
    } catch (e) { toast(e.message, 'error'); }
  };
}

function renderSkillCreator(body) {
  body.innerHTML =
    '<div class="panel-title"><h3>新建技能</h3><span class="hint">会生成 skills/&lt;slug&gt;/SKILL.md</span></div>' +
    '<div class="grid2">' +
      '<label class="field">slug（英文标识）<input id="newSlug" placeholder="my-skill"></label>' +
      '<label class="field">名称<input id="newName" placeholder="我的技能"></label>' +
    '</div>' +
    '<label class="field" style="margin-top:10px">描述（给模型看的触发说明）' +
      '<input id="newDesc" placeholder="什么时候应该使用这个技能"></label>' +
    '<label class="field" style="margin-top:10px">正文（Markdown，可留空自动生成模板）' +
      '<textarea id="newBody" rows="8" placeholder="# 适用场景…"></textarea></label>' +
    '<div class="row-actions" style="margin-top:12px">' +
      '<button class="btn" id="cancelCreate">取消</button>' +
      '<button class="btn primary" id="createSkill">创建</button>' +
    '</div>';

  $('#cancelCreate').onclick = () => { state.skillCreating = false; renderSkills(body); };
  $('#createSkill').onclick = async () => {
    const slug = $('#newSlug').value.trim();
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(slug)) {
      toast('slug 只能是字母、数字、- 和 _', 'error');
      return;
    }
    try {
      await api('POST', '/api/skills', {
        slug,
        name: $('#newName').value.trim() || slug,
        description: $('#newDesc').value.trim(),
        body: $('#newBody').value,
        enable: true,
      });
      state.skillCreating = false;
      await loadSkills();
      await refreshConfig();
      await renderSkills(body);
      toast('技能已创建并启用');
    } catch (e) { toast(e.message, 'error'); }
  };
}

/* ------------------------------ 事件绑定 ------------------------------ */
function autosize() {
  const input = $('#input');
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 200) + 'px';
}

function bindStaticEvents() {
  $('#btnNew').onclick = newChat;
  $('#btnSettings').onclick = () => openModal('models');
  $('#btnSkills').onclick = () => openModal('skills');
  $('#btnCloseModal').onclick = closeModal;
  $('#btnClear').onclick = clearCurrentChat;
  $('#btnSend').onclick = sendMessage;
  $('#btnStop').onclick = () => { if (state.controller) state.controller.abort(); };

  /* 设置弹窗：可拖动（拖标题栏），点遮罩不再关闭，双击标题复位 */
  (function enableModalDrag() {
    const card = document.querySelector('.modal-card');
    const modal = $('#modal');
    if (!card || !modal) return;
    let sx = 0, sy = 0, ox = 0, oy = 0, dragging = false;
    card.addEventListener('mousedown', (e) => {
      if (!e.target.closest('.modal-head') || e.target.closest('button')) return;
      dragging = true;
      sx = e.clientX; sy = e.clientY;
      ox = Number(card.dataset.ox || 0); oy = Number(card.dataset.oy || 0);
      card.style.transition = 'none';
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const nx = ox + (e.clientX - sx);
      const ny = oy + (e.clientY - sy);
      card.dataset.ox = nx; card.dataset.oy = ny;
      card.style.transform = 'translate(' + nx + 'px,' + ny + 'px)';
    });
    window.addEventListener('mouseup', () => { dragging = false; });
    modal.addEventListener('dblclick', (e) => {
      if (!e.target.closest('.modal-head')) return;
      card.dataset.ox = 0; card.dataset.oy = 0;
      card.style.transform = '';
    });
  })();

  $('#tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.tab');
    if (!btn) return;
    state.tab = btn.dataset.tab;
    state.skillEditing = null;
    state.skillCreating = false;
    renderModal();
  });

  $('#sessionList').addEventListener('click', (e) => {
    const del = e.target.closest('[data-del]');
    if (del) { e.stopPropagation(); deleteSession(del.dataset.del); return; }
    const item = e.target.closest('.session-item');
    if (item) openSession(item.dataset.id);
  });

  $('#providerSelect').onchange = async (e) => {
    state.providerId = e.target.value;
    try {
      await api('PUT', '/api/config', { active_provider: state.providerId });
      if (state.session && state.session.id) {
        await api('PATCH', '/api/sessions/' + state.session.id, { provider_id: state.providerId });
      }
      await loadConfig();
      updateHeader();
      paintEmpty();
    } catch (err) { toast(err.message, 'error'); }
  };

  $('#input').addEventListener('input', () => { autosize(); paintDraft(); });
  $('#input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendMessage();
    }
  });

  /* 输入走真实输入框（隐藏但可聚焦），中文输入法不会被锁成英文 */
  const focusInput = () => { ensureDraft(); focusDraft(); };
  document.addEventListener('click', (e) => {
    if (e.target.closest('#messages') && !e.target.closest('.send-dialog')) focusInput();
  });
  document.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    const modal = $('#modal');
    if (modal && !modal.hidden) return;
    if (document.querySelector('.send-dialog')) return;
    if (document.activeElement === $('#input')) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'Tab') return;
    focusInput();
  });

  document.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (chip) {
      $('#input').value = chip.dataset.prompt || '';
      autosize();
      paintDraft();
      sendMessage();
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#modal').hidden) closeModal();
  });
}

boot();


/* ---------------- 工单（Ticket）：只显示需要人的那些 ---------------- */
const TICKETS = new Map();
const STAGE_LABEL = {
  'person/answer': '等你回答', 'person/review': '等你验收',
  'person/blocked': '受阻', 'person/failed': '失败',
  'working/queued': '排队中', 'working': '执行中', 'idle': '空闲', 'done': '已结束',
};

function escHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function upsertTicket(ev) {
  if (!ev || !ev.id) return;
  const old = TICKETS.get(ev.id) || { id: ev.id, request: '', session: '' };
  TICKETS.set(ev.id, Object.assign(old, {
    stage: ev.stage || old.stage,
    session: ev.session || old.session,
    request: ev.request || old.request,
  }));
  renderRail();
}

async function loadTickets() {
  try {
    const r = await fetch('/api/tickets', { cache: 'no-store' });
    const data = await r.json();
    (data.tickets || []).forEach((t) => {
      const old = TICKETS.get(t.id) || {};
      TICKETS.set(t.id, Object.assign(old, {
        id: t.id, stage: t.stage, session: t.session, request: t.request,
      }));
    });
    renderRail();
  } catch (e) { /* 服务未就绪时静默 */ }
}

function renderRail() {
  const rail = document.getElementById('rail');
  if (!rail) return;
  const list = [...TICKETS.values()]
    .filter((t) => String(t.stage || '').indexOf('person/') === 0)
    .sort((a, b) => String(a.stage).localeCompare(String(b.stage)));
  rail.hidden = list.length === 0;
  if (!list.length) { rail.innerHTML = ''; return; }
  rail.innerHTML = '<div class="rail-head"><span class="rail-dot"></span>待人工 · ' + list.length + '</div>' +
    list.map((t) => '<div class="tcard" data-ticket="' + escHtml(t.id) + '">' +
      '<div class="tcard-top">#' + escHtml(t.id) + '<span class="tcard-stage">' + escHtml(STAGE_LABEL[t.stage] || t.stage) + '</span></div>' +
      '<div class="tcard-req">' + escHtml((t.request || '').slice(0, 60)) + '</div>' +
      '<div class="tcard-meta">执行会话 ' + escHtml(t.session || '—') + '</div></div>').join('');
  rail.querySelectorAll('.tcard').forEach((el) => {
    el.onclick = () => {
      const t = TICKETS.get(el.dataset.ticket) || {};
      if (t.session) location.href = '/?session=' + encodeURIComponent(t.session);
    };
  });
}

loadTickets();
setInterval(loadTickets, 15000);


/* ---------------- 附件：拖进来 / 右键上传 ---------------- */
let ATTACH = [];

function fmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result).split(',')[1] || '');
    fr.onerror = () => reject(fr.error);
    fr.readAsDataURL(file);
  });
}

async function uploadFiles(list) {
  const files = [...list];
  if (!files.length) return;
  if (state.streaming) { toast('等这一轮回答结束再上传', 'error'); return; }
  ensureDraft(); focusDraft();
  for (const f of files) {
    try {
      const data = await readAsBase64(f);
      const res = await fetch('/api/uploads', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: f.name, data }),
      });
      const out = await res.json();
      if (!res.ok || !out.ok) throw new Error(out.detail || out.message || ('HTTP ' + res.status));
      ATTACH.push({ name: out.name || f.name, path: out.path, size: out.size || f.size });
    } catch (e) {
      toast('上传失败：' + f.name + ' · ' + e.message, 'error');
    }
  }
  renderAtts();
  focusDraft();
}

function renderAtts() {
  const bubble = document.querySelector('.msg.draft .bubble');
  if (!bubble) return;
  let box = bubble.querySelector('.atts');
  if (!ATTACH.length) { if (box) box.remove(); return; }
  if (!box) {
    box = document.createElement('div');
    box.className = 'atts';
    bubble.insertBefore(box, bubble.firstChild);
  }
  box.innerHTML = ATTACH.map((a, i) => '<span class="att" data-i="' + i + '">' +
    '<span class="att-ico">▤</span>' + escHtml(a.name) +
    '<span class="att-size">' + fmtSize(a.size) + '</span>' +
    '<span class="att-x" title="移除">x</span></span>').join('');
  box.querySelectorAll('.att-x').forEach((x) => {
    x.onclick = (e) => {
      e.stopPropagation();
      ATTACH.splice(Number(x.parentElement.dataset.i), 1);
      renderAtts();
      focusDraft();
    };
  });
}

/* 拖拽：整块对话区都能接；右键：输入区弹出上传菜单 */
(function enableDrop() {
  const overlay = document.createElement('div');
  overlay.className = 'dropzone';
  overlay.hidden = true;
  overlay.innerHTML = '<div class="dropzone-inner">松手即上传 · 支持多文件</div>';
  document.body.appendChild(overlay);
  let depth = 0;
  const hasFiles = (e) => e.dataTransfer && [...(e.dataTransfer.types || [])].includes('Files');
  const show = () => { if (!state.streaming) overlay.hidden = false; };
  const hide = () => { overlay.hidden = true; depth = 0; };
  document.addEventListener('dragenter', (e) => { if (!hasFiles(e)) return; depth += 1; show(); });
  document.addEventListener('dragover', (e) => { if (!hasFiles(e)) return; e.preventDefault(); show(); });
  document.addEventListener('dragleave', () => { depth -= 1; if (depth <= 0) hide(); });
  document.addEventListener('drop', (e) => {
    if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
    e.preventDefault();
    hide();
    uploadFiles(e.dataTransfer.files);
  });

  const picker = document.createElement('input');
  picker.type = 'file';
  picker.multiple = true;
  picker.hidden = true;
  picker.onchange = () => { if (picker.files && picker.files.length) uploadFiles(picker.files); picker.value = ''; };
  document.body.appendChild(picker);

  const menu = document.createElement('div');
  menu.className = 'ctxmenu';
  menu.hidden = true;
  menu.innerHTML = '<div class="ctx-item" data-act="upload"><span>▤</span>上传文档</div>' +
    '<div class="ctx-hint">也可直接把文件拖进对话区</div>';
  document.body.appendChild(menu);
  menu.querySelector('[data-act="upload"]').onclick = () => { menu.hidden = true; picker.click(); };
  document.addEventListener('click', () => { menu.hidden = true; });
  document.addEventListener('scroll', () => { menu.hidden = true; }, true);

  document.addEventListener('contextmenu', (e) => {
    if (!e.target.closest('#messages')) return;
    e.preventDefault();
    ensureDraft();
    menu.style.left = Math.min(e.clientX, window.innerWidth - 190) + 'px';
    menu.style.top = Math.min(e.clientY, window.innerHeight - 90) + 'px';
    menu.hidden = false;
  });
})();
