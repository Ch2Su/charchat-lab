'use strict';

/* charchat-lab 화면. 빌드 도구 없이 그대로 동작하는 순수 자바스크립트. */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const store = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(`charchat:${key}`);
      return raw === null ? fallback : JSON.parse(raw);
    } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(`charchat:${key}`, JSON.stringify(value)); } catch { /* 저장소를 못 써도 동작한다 */ }
  },
};

const state = {
  config: null,
  characters: [],
  characterId: null,
  chats: [],
  chatId: null,
  view: null,
  selectedTurnId: null,
  pendingMeta: null,
  streaming: null,
  tab: 'turn',
  editingCharacterId: null,
  summaryPoll: null,
};

const PROVIDER_LABELS = {
  mock: 'mock (키 없이 흉내)',
  anthropic: 'Anthropic Claude',
  gemini: 'Google Gemini',
  openai_compat: 'OpenAI 호환 (Ollama 등)',
};
const STRATEGY_LABELS = { anchored: '요약 고정점', sliding: '슬라이딩', full: '전체' };
const CACHE_LABELS = { explicit: '명시적 중단점', auto: '자동', off: '끔' };
const STAGE_LABELS = { input: '입력', output: '출력', card: '카드', provider: '제공사', edit: '직접 수정' };
const ACTION_LABELS = { blocked: '막음', logged: '기록만', crisis: '위기 안내', retracted: '가림' };
const FINISH_LABELS = {
  stop: '정상 종료', length: '길이 제한', safety: '안전 필터', refusal: '거절', error: '오류', aborted: '멈춤',
};

const SETTING_GROUPS = [
  { title: '모델', fields: ['provider', 'model'] },
  { title: '창과 요약', fields: ['window_strategy', 'input_token_budget', 'summary_trigger_ratio', 'summary_keep_ratio'] },
  { title: '캐시와 로어북', fields: ['cache_mode', 'lore_scan_depth', 'lore_token_budget'] },
  { title: '생성', fields: ['max_output_tokens', 'temperature'] },
  { title: '검수', fields: ['moderation_mode', 'moderation_backend', 'output_buffer'] },
];
const FIELD_INFO = {
  provider: { label: '제공사' },
  model: { label: '모델', help: '비우면 기본 모델을 써요.' },
  window_strategy: {
    label: '창 전략',
    options: {
      anchored: '요약 고정점: 예산에 가까워지면 오래된 부분을 덩어리로 요약',
      sliding: '슬라이딩: 매 턴 오래된 메시지를 버림(요약 없음)',
      full: '전체: 대화를 모두 보냄(예산 무시)',
    },
  },
  input_token_budget: { label: '입력 예산(토큰)', help: '요청 한 번에 넣을 최대 입력 토큰. 모델 한도가 아니라 비용 기준으로 정하는 값이에요.' },
  summary_trigger_ratio: { label: '요약 시작 비율', help: '요약 지점 이후 대화가 대화 예산의 이 비율을 넘으면 요약해요.', step: 0.05 },
  summary_keep_ratio: { label: '요약 후 남길 비율', help: '요약하고 나서 원문으로 남길 대화의 분량이에요.', step: 0.05 },
  cache_mode: {
    label: '캐시 방식',
    options: {
      explicit: '명시적 중단점: 고정, 요약, 직전 대화, 마지막 입력',
      auto: '자동: 최상위 하나. 이 구조에선 거의 적중하지 않아요',
      off: '끄기',
    },
    help: 'Anthropic과 mock에만 적용돼요. Gemini는 자동 캐시만 있어요.',
  },
  lore_scan_depth: { label: '로어 스캔 범위', help: '최근 메시지 몇 개에서 키워드를 찾을지 정해요.' },
  lore_token_budget: { label: '로어 예산(토큰)', help: '넘치면 우선순위가 낮은 항목부터 빠져요.' },
  max_output_tokens: { label: '최대 출력 토큰', help: 'Claude와 Gemini는 사고 토큰용 여유분이 따로 더해져요.' },
  temperature: { label: 'temperature', help: '비우면 기본값이에요. 현행 Claude 모델은 이 값을 받지 않아서 보내지 않아요.', step: 0.05 },
  moderation_mode: { label: '검수 모드', options: { block: '막기', log: '기록만', off: '끄기' } },
  moderation_backend: { label: '검수 방식', options: { rules: '규칙(정규식)', llm: '규칙 + LLM 판정' } },
  output_buffer: {
    label: '출력 버퍼',
    options: { sentence: '문장 단위로 검사한 뒤 표시', none: '바로 표시하고 끝난 뒤 검사' },
  },
};

/* ── 유틸 ─────────────────────────────────── */
const numberFormat = new Intl.NumberFormat('ko-KR');
const fmt = (value) => (value === null || value === undefined ? '–' : numberFormat.format(Math.round(value)));
const pct = (part, whole) => (whole ? `${Math.round((part / whole) * 100)}%` : '–');
const ms = (value) => {
  if (value === null || value === undefined) return '–';
  return value >= 1000 ? `${(value / 1000).toFixed(2)}초` : `${Math.round(value)}ms`;
};
const usd = (value) => {
  if (value === null || value === undefined) return '–';
  return `$${value < 0.01 ? value.toFixed(6) : value.toFixed(4)}`;
};
const sleep = (time) => new Promise((resolve) => setTimeout(resolve, time));

function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function rich(text) {
  return escapeHtml(text)
    .split(/\n{2,}/)
    .map((block) => `<p>${block
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*([^*\n]+?)\*/g, '<em class="act">$1</em>')
      .replace(/\n/g, '<br>')}</p>`)
    .join('');
}

function errorDetail(data, fallback) {
  if (!data || data.detail === undefined) return fallback;
  if (typeof data.detail === 'string') return data.detail;
  if (Array.isArray(data.detail)) return data.detail.map((d) => d.msg || JSON.stringify(d)).join(', ');
  return JSON.stringify(data.detail);
}

async function api(path, options = {}) {
  const init = { method: options.method || 'GET', headers: {} };
  if (options.json !== undefined) {
    init.method = options.method || 'POST';
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.json);
  } else if (options.body) {
    init.method = options.method || 'POST';
    init.body = options.body;
  }
  const response = await fetch(path, init);
  if (!response.ok) {
    let data = null;
    try { data = await response.json(); } catch { /* 본문이 JSON이 아님 */ }
    throw new Error(errorDetail(data, `${response.status} ${response.statusText}`));
  }
  const type = response.headers.get('content-type') || '';
  return type.includes('application/json') ? response.json() : response.text();
}

async function streamPost(path, body, handlers, signal) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    let data = null;
    try { data = await response.json(); } catch { /* 본문이 JSON이 아님 */ }
    throw new Error(errorDetail(data, `${response.status} ${response.statusText}`));
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index;
    while ((index = buffer.indexOf('\n\n')) !== -1) {
      const block = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      let name = 'message';
      let data = '';
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) name = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trimStart();
      }
      if (handlers[name]) handlers[name](data ? JSON.parse(data) : {});
    }
  }
}

let toastTimer = null;
function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 6000);
}

const isNarrow = () => window.matchMedia('(max-width: 760px)').matches;

/* ── 사이드바 ─────────────────────────────── */
async function loadCharacters() {
  state.characters = await api('/api/characters');
  renderCharacters();
}

function renderCharacters() {
  const list = $('#char-list');
  list.innerHTML = '';
  if (!state.characters.length) {
    list.innerHTML = '<li class="empty-note">캐릭터가 없어요. 새로 만들거나 카드 파일(JSON, PNG)을 가져오세요.</li>';
    return;
  }
  for (const character of state.characters) {
    const li = document.createElement('li');
    li.className = character.id === state.characterId ? 'active' : '';
    li.innerHTML = `
      <button class="list-item" type="button">
        <span class="grow">
          <span class="title">${escapeHtml(character.name)}</span>
          <span class="sub">대화 ${character.chat_count}개, 로어 ${character.lore_count}개</span>
        </span>
      </button>
      <button class="item-action" type="button" aria-label="${escapeHtml(character.name)} 편집">편집</button>`;
    li.querySelector('.list-item').addEventListener('click', () => selectCharacter(character.id));
    li.querySelector('.item-action').addEventListener('click', () => openCharacterEditor(character.id));
    list.appendChild(li);
  }
}

function relativeTime(epochSeconds) {
  const diff = Date.now() / 1000 - epochSeconds;
  if (diff < 60) return '방금';
  if (diff < 3600) return `${Math.floor(diff / 60)}분 전`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}시간 전`;
  return new Date(epochSeconds * 1000).toLocaleDateString('ko-KR');
}

function renderChats() {
  const list = $('#chat-list');
  list.innerHTML = '';
  if (!state.characterId) return;
  if (!state.chats.length) {
    list.innerHTML = '<li class="empty-note">아직 대화가 없어요. 새 대화를 시작해 보세요.</li>';
    return;
  }
  for (const chat of state.chats) {
    const li = document.createElement('li');
    li.className = chat.id === state.chatId ? 'active' : '';
    const title = chat.title || `대화 ${chat.id}`;
    li.innerHTML = `
      <button class="list-item" type="button">
        <span class="grow">
          <span class="title">${escapeHtml(title)}</span>
          <span class="sub">${escapeHtml(chat.persona_name || '이름 없음')}, 메시지 ${chat.message_count}개, ${relativeTime(chat.updated_at)}</span>
        </span>
      </button>
      <button class="item-action" type="button" aria-label="${escapeHtml(title)} 삭제">삭제</button>`;
    li.querySelector('.list-item').addEventListener('click', () => openChat(chat.id));
    li.querySelector('.item-action').addEventListener('click', () => deleteChat(chat.id));
    list.appendChild(li);
  }
}

async function selectCharacter(id, { restoreChat = false } = {}) {
  state.characterId = id;
  store.set('characterId', id);
  renderCharacters();
  state.chats = await api(`/api/chats?character_id=${id}`);
  const lastChatId = store.get('chatId');
  const target = (restoreChat && state.chats.find((c) => c.id === lastChatId)) || state.chats[0];
  if (target) {
    await openChat(target.id);
  } else {
    state.chatId = null;
    state.view = null;
    state.selectedTurnId = null;
    renderChats();
    renderStage();
    renderInspector();
  }
}

async function refreshChatList() {
  if (!state.characterId) return;
  state.chats = await api(`/api/chats?character_id=${state.characterId}`);
  renderChats();
}

async function openChat(id) {
  state.chatId = id;
  store.set('chatId', id);
  state.view = await api(`/api/chats/${id}`);
  state.selectedTurnId = latestAssistantId();
  state.pendingMeta = null;
  renderChats();
  renderStage();
  renderInspector();
  if (isNarrow()) $('#app').classList.remove('show-sidebar');
  if (state.view.summarizing) pollSummary();
}

async function deleteChat(id) {
  if (!window.confirm('이 대화를 삭제할까요? 되돌릴 수 없어요.')) return;
  await api(`/api/chats/${id}`, { method: 'DELETE' });
  if (state.chatId === id) {
    state.chatId = null;
    state.view = null;
  }
  await loadCharacters();
  await selectCharacter(state.characterId);
}

/* ── 무대 ─────────────────────────────────── */
function latestAssistantId() {
  const messages = state.view?.messages || [];
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === 'assistant' && messages[i].meta?.diagnostics) return messages[i].id;
  }
  return null;
}

function renderModelChip() {
  const config = state.config;
  if (!config) return;
  const settings = config.settings;
  $('#chip-model').innerHTML = `<strong>${escapeHtml(config.model || '모델 미지정')}</strong> ${STRATEGY_LABELS[settings.window_strategy]}`;
  const hint = $('#composer-hint');
  const parts = ['Enter로 보내고 Shift+Enter로 줄을 바꿔요.'];
  if (settings.provider === 'mock') parts.push('mock 테스트 명령: /long, /mod-test-output, /error, /slow');
  if (state.view?.summarizing) parts.push('이전 대화를 요약하는 중이에요.');
  hint.textContent = parts.join(' ');
}

function renderStage() {
  const view = state.view;
  const character = state.characters.find((c) => c.id === state.characterId);
  $('#stage-name').textContent = view ? view.character.name : (character ? character.name : 'charchat-lab');
  $('#stage-sub').textContent = view
    ? `AI 캐릭터와 나누는 대화예요. 상대는 사람이 아니에요. ${view.persona_name}(으)로 대화 중이에요.`
    : 'AI 캐릭터와 나누는 대화예요. 상대는 사람이 아니에요.';
  renderMessages();
  renderModelChip();
  setComposerBusy(Boolean(state.streaming));
}

function renderMessages() {
  const box = $('#messages');
  box.innerHTML = '';
  const view = state.view;
  if (!view) {
    const hasCharacter = Boolean(state.characterId);
    box.innerHTML = `
      <div class="welcome">
        <h2>${hasCharacter ? '새 대화를 시작해 보세요' : '캐릭터를 먼저 만들어 보세요'}</h2>
        <p>${hasCharacter
          ? '왼쪽의 대화를 고르거나 새 대화를 시작하세요. 오른쪽 패널에서 매 턴의 프롬프트 구성과 캐시, 지연, 비용을 볼 수 있어요.'
          : '왼쪽에서 새 캐릭터를 만들거나 캐릭터 카드(JSON, PNG)를 가져오세요.'}</p>
        ${hasCharacter ? '<p><button class="btn" type="button" id="btn-welcome-chat">새 대화 시작</button></p>' : ''}
      </div>`;
    $('#btn-welcome-chat')?.addEventListener('click', openNewChat);
    return;
  }
  if (!view.messages.length) {
    box.innerHTML = '<div class="welcome"><p>첫 메시지를 보내 대화를 시작하세요.</p></div>';
    return;
  }
  view.messages.forEach((message, index) => {
    box.appendChild(messageElement(message, index === view.messages.length - 1));
  });
  scrollToBottom();
}

function scrollToBottom(onlyIfNear = false) {
  const box = $('#messages');
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 160;
  if (!onlyIfNear || near) box.scrollTop = box.scrollHeight;
}

function toolButton(label, handler) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = label;
  button.addEventListener('click', handler);
  return button;
}

function flag(text) {
  const span = document.createElement('span');
  span.className = 'flag';
  span.textContent = text;
  return span;
}

function metricSpans(meta) {
  const usage = meta.usage;
  const items = [];
  if (usage) {
    items.push(`입력 <b>${fmt(usage.input_total)}</b>`);
    items.push(`캐시 <b>${pct(usage.cache_read, usage.input_total)}</b>`);
  }
  if (meta.latency?.ttft_ms !== null && meta.latency?.ttft_ms !== undefined) {
    items.push(`첫 토큰 <b>${ms(meta.latency.ttft_ms)}</b>`);
  }
  return items.map((html) => {
    const span = document.createElement('span');
    span.className = 'metric';
    span.innerHTML = html;
    return span;
  });
}

function messageElement(message, isLast) {
  const view = state.view;
  const el = document.createElement('article');
  el.className = `msg ${message.role}${message.excluded ? ' excluded' : ''}${message.id === state.selectedTurnId ? ' selected' : ''}`;
  el.dataset.id = message.id;
  const name = message.role === 'assistant' ? view.character.name : message.role === 'notice' ? '안내' : view.persona_name;
  const body = message.role === 'notice' ? escapeHtml(message.content) : rich(message.content);
  el.innerHTML = `
    <div class="msg-wrap">
      ${message.role !== 'user' ? `<div class="msg-name">${escapeHtml(name)}</div>` : ''}
      <div class="msg-body">${body}</div>
      <div class="msg-tools"></div>
    </div>`;
  const tools = el.querySelector('.msg-tools');
  const meta = message.meta || {};

  if (message.sibling_count > 1) {
    const sib = document.createElement('span');
    sib.className = 'sib';
    const prevId = message.sibling_ids[(message.sibling_index - 1 + message.sibling_count) % message.sibling_count];
    const nextId = message.sibling_ids[(message.sibling_index + 1) % message.sibling_count];
    const prev = toolButton('‹', () => switchTo(prevId));
    prev.setAttribute('aria-label', '이전 버전');
    const next = toolButton('›', () => switchTo(nextId));
    next.setAttribute('aria-label', '다음 버전');
    const label = document.createElement('span');
    label.textContent = `${message.sibling_index + 1}/${message.sibling_count}`;
    sib.append(prev, label, next);
    tools.appendChild(sib);
  }

  if (message.role === 'assistant') {
    tools.append(...metricSpans(meta));
    if (meta.status === 'aborted') tools.append(flag('멈춤으로 중단됨'));
    if (meta.status === 'error') tools.append(flag('오류로 중단됨'));
    if (message.excluded) tools.append(flag('검수로 가려짐'));
    if (meta.diagnostics) tools.append(toolButton('계측 보기', () => selectTurn(message.id)));
    if (isLast && message.parent_id !== null) tools.append(toolButton('다시 생성', () => regenerate(message.id)));
    if (!message.excluded) tools.append(toolButton('수정', () => startEdit(el, message)));
  } else if (message.role === 'user') {
    if (message.excluded) {
      tools.append(flag('검수로 막혀 모델에 보내지 않음'));
    } else {
      tools.append(toolButton('수정', () => startEdit(el, message)));
      if (isLast) tools.append(toolButton('답변 생성', () => regenerate(message.id)));
    }
  }
  return el;
}

function startEdit(el, message) {
  if (state.streaming) return;
  const body = el.querySelector('.msg-body');
  const tools = el.querySelector('.msg-tools');
  const original = body.innerHTML;
  tools.hidden = true;
  body.innerHTML = '';
  const box = document.createElement('div');
  box.className = 'edit-box';
  box.innerHTML = `
    <textarea aria-label="메시지 수정"></textarea>
    <div class="row">
      <button class="btn ghost small" type="button" data-cancel>취소</button>
      <button class="btn small" type="button" data-save>${message.role === 'user' ? '고쳐서 다시 보내기' : '고친 내용 저장'}</button>
    </div>`;
  const textarea = box.querySelector('textarea');
  textarea.value = message.content;
  body.appendChild(box);
  textarea.focus();
  box.querySelector('[data-cancel]').addEventListener('click', () => {
    body.innerHTML = original;
    tools.hidden = false;
  });
  box.querySelector('[data-save]').addEventListener('click', async () => {
    const content = textarea.value.trim();
    if (!content) return;
    if (message.role === 'user') {
      await runStream(`/api/messages/${message.id}/edit`, { content }, { userText: content, replaceFrom: message.id });
    } else {
      try {
        await api(`/api/messages/${message.id}/edit`, { json: { content } });
        await openChat(state.chatId);
      } catch (error) {
        toast(error.message);
      }
    }
  });
}

async function switchTo(messageId) {
  if (state.streaming) return;
  state.view = await api(`/api/chats/${state.chatId}/switch`, { json: { message_id: messageId } });
  state.selectedTurnId = latestAssistantId();
  renderStage();
  renderInspector();
}

function selectTurn(messageId) {
  state.selectedTurnId = messageId;
  state.pendingMeta = null;
  $$('.msg').forEach((el) => el.classList.toggle('selected', Number(el.dataset.id) === messageId));
  state.tab = 'turn';
  renderInspector();
  if (window.matchMedia('(max-width: 1180px)').matches) $('#app').classList.add('show-inspector');
}

/* ── 보내기와 스트리밍 ─────────────────────── */
function setComposerBusy(busy) {
  $('#btn-send').hidden = busy;
  $('#btn-stop').hidden = !busy;
  $('#btn-send').disabled = !state.chatId;
  $('#input').disabled = !state.chatId;
}

async function send() {
  const input = $('#input');
  const content = input.value.trim();
  if (!content || !state.chatId || state.streaming) return;
  input.value = '';
  autoResize();
  await runStream(`/api/chats/${state.chatId}/messages`, { content }, { userText: content });
}

async function regenerate(messageId) {
  if (state.streaming) return;
  await runStream(`/api/chats/${state.chatId}/regenerate`, { message_id: messageId }, { replaceFrom: messageId, keepTarget: true });
}

async function runStream(path, body, options = {}) {
  if (state.streaming) return;
  const box = $('#messages');
  const chatId = state.chatId;

  // 화면을 먼저 바꿔 둔다: 다시 생성/수정이면 대상 이후를 지우고, 보내기면 내 메시지를 붙인다
  if (options.replaceFrom) {
    const target = box.querySelector(`.msg[data-id="${options.replaceFrom}"]`);
    if (target) {
      let node = options.keepTarget && target.classList.contains('user') ? target.nextElementSibling : target;
      while (node) {
        const next = node.nextElementSibling;
        node.remove();
        node = next;
      }
    }
  }
  if (options.userText) {
    box.appendChild(messageElement({
      id: -1, role: 'user', content: options.userText, sibling_count: 1, meta: {}, excluded: false, parent_id: null,
    }, false));
    box.lastElementChild.querySelector('.msg-tools').innerHTML = '';
  }
  const live = document.createElement('article');
  live.className = 'msg assistant';
  live.innerHTML = `
    <div class="msg-wrap">
      <div class="msg-name">${escapeHtml(state.view.character.name)}</div>
      <div class="msg-body cursor"></div>
      <div class="msg-tools"><span class="metric">답변을 기다리는 중</span></div>
    </div>`;
  const liveBody = live.querySelector('.msg-body');
  let liveShown = false;
  const showLive = () => {
    if (!liveShown) {
      box.appendChild(live);
      liveShown = true;
    }
  };
  scrollToBottom();

  const controller = new AbortController();
  state.streaming = { controller };
  setComposerBusy(true);
  let text = '';
  let aborted = false;
  let summaryScheduled = false;
  try {
    await streamPost(path, body, {
      meta: (data) => {
        showLive();
        state.pendingMeta = data;
        state.tab = 'turn';
        renderInspector();
        scrollToBottom();
      },
      delta: (data) => {
        text += data.text;
        liveBody.innerHTML = rich(text);
        live.querySelector('.msg-tools').innerHTML = '';
        scrollToBottom(true);
      },
      retract: (data) => {
        text = data.text;
        liveBody.innerHTML = rich(text);
        live.classList.add('excluded');
      },
      notice: (data) => {
        box.appendChild(messageElement({
          id: -2, role: 'notice', content: data.text, sibling_count: 1, meta: {}, excluded: false,
        }, false));
        scrollToBottom();
      },
      error: (data) => toast(data.message),
      done: (data) => { summaryScheduled = Boolean(data.summary_scheduled); },
    }, controller.signal);
  } catch (error) {
    if (error.name === 'AbortError') aborted = true;
    else toast(error.message);
  } finally {
    liveBody.classList.remove('cursor');
    state.streaming = null;
    state.pendingMeta = null;
    setComposerBusy(false);
  }
  if (aborted) await sleep(500); // 서버가 끊김을 감지하고 받은 만큼 저장할 시간을 준다
  if (state.chatId !== chatId) return;
  await openChat(chatId);
  refreshChatList();
  loadCharacters();
  if (summaryScheduled) pollSummary();
  $('#input').focus();
}

function pollSummary() {
  clearTimeout(state.summaryPoll);
  let tries = 0;
  const tick = async () => {
    tries += 1;
    if (!state.chatId) return;
    const chatId = state.chatId;
    try {
      const view = await api(`/api/chats/${chatId}`);
      if (state.chatId !== chatId || state.streaming) return;
      state.view.summary = view.summary;
      state.view.summaries = view.summaries;
      state.view.summarizing = view.summarizing;
      state.view.summary_error = view.summary_error;
      renderModelChip();
      if (state.tab === 'summary') renderInspector();
      if (view.summarizing && tries < 60) state.summaryPoll = setTimeout(tick, 1000);
      else if (view.summary_error) toast(`요약 실패: ${view.summary_error}`);
    } catch { /* 다음 기회에 */ }
  };
  state.summaryPoll = setTimeout(tick, 600);
}

/* ── 계측 패널 ─────────────────────────────── */
function renderInspector() {
  $$('.tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.tab === state.tab));
  for (const id of ['turn', 'compare', 'summary', 'mod']) $(`#tab-${id}`).hidden = id !== state.tab;
  if (state.tab === 'turn') renderTurnTab();
  else if (state.tab === 'compare') renderCompareTab();
  else if (state.tab === 'summary') renderSummaryTab();
  else renderModTab();
}

const note = (text) => `<p class="panel-note">${text}</p>`;

function renderTurnTab() {
  const box = $('#tab-turn');
  if (state.pendingMeta) {
    box.innerHTML = turnHtml({ diagnostics: state.pendingMeta.diagnostics, run: state.pendingMeta.run }, null, true);
    return;
  }
  const message = state.view?.messages.find((m) => m.id === state.selectedTurnId);
  if (!message) {
    box.innerHTML = note('답변을 하나 받으면 그 턴에 보낸 프롬프트의 구성, 캐시 적중, 지연 시간, 비용이 여기 나와요. 예전 답변은 아래의 "계측 보기"로 고를 수 있어요.');
    return;
  }
  box.innerHTML = turnHtml(message.meta, message.id, false);
  const raw = box.querySelector('details.raw');
  raw?.addEventListener('toggle', async () => {
    if (!raw.open || raw.dataset.loaded) return;
    try {
      const log = await api(`/api/messages/${message.id}/prompt`);
      raw.querySelector('pre').textContent = JSON.stringify(log.payload, null, 2);
      raw.dataset.loaded = '1';
    } catch (error) {
      raw.querySelector('pre').textContent = error.message;
    }
  });
}

function cacheNote(usage, run) {
  if (!usage || !run) return '';
  if (run.provider === 'gemini') {
    return note('Gemini는 자동(암묵적) 캐시만 있어요. 앞부분이 같고 일정 길이(3.8 Flash 기준 4,096토큰) 이상일 때 적중해요.');
  }
  if (run.provider === 'openai_compat') {
    return note('로컬 서버는 캐시 토큰을 보고하지 않는 경우가 많아요. 대신 첫 토큰 시간으로 KV 캐시 재사용 효과를 볼 수 있어요.');
  }
  if (run.cache_mode === 'off') return note('캐시를 끈 상태예요.');
  if (!usage.cache_read && !usage.cache_write) {
    return note('캐시를 전혀 쓰지 못했어요. 중단점까지의 앞부분이 최소 길이(현행 Claude 모델 512토큰)보다 짧으면 캐시되지 않아요.');
  }
  if (run.cache_mode === 'auto' && !usage.cache_read) {
    return note('자동 캐시는 마지막 블록에만 중단점을 둬요. 이 앱은 마지막 메시지에 매 턴 다른 메모를 붙이기 때문에 다음 턴에 거의 적중하지 않아요.');
  }
  return note('캐시 읽기는 기본 입력 가격의 0.1배(Opus·Sonnet 5.5는 0.05배), 캐시 쓰기는 1.25배예요. 캐시는 5분 동안 쓰이지 않으면 사라져요.');
}

function turnHtml(meta, messageId, pending) {
  if (!meta || !meta.diagnostics) {
    return note('이 메시지에는 계측 정보가 없어요. 인사말이거나 직접 고친 답변이에요.');
  }
  const d = meta.diagnostics;
  const est = d.est;
  const usage = meta.usage;
  const run = meta.run || {};
  const total = est.total || 1;
  const segments = [
    ['fixed', '고정 프롬프트', est.fixed],
    ['summary', '요약', est.summary],
    ['history', '대화', est.history],
    ['final', '마지막 입력(로어·지시 포함)', est.final],
  ];
  let html = `
    <section class="panel-block">
      <h3>프롬프트 구성 <span class="muted small-text">추정 토큰</span></h3>
      <div class="stack">${segments.filter((s) => s[2] > 0).map(([key, label, value]) =>
        `<span class="seg-${key}" style="width:${(value / total) * 100}%" title="${label} ${fmt(value)}"></span>`).join('')}</div>
      <div class="legend">
        ${segments.map(([key, label, value]) => `<i class="seg-${key}"></i><span>${label}</span><span class="num">${fmt(value)}</span>`).join('')}
        <i></i><b>합계</b><b class="num">${fmt(est.total)}</b>
      </div>
      ${note('앞쪽(고정, 요약)은 잘 바뀌지 않아 캐시되기 쉽고, 마지막 입력은 매 턴 바뀌어요.')}
    </section>`;

  if (pending) html += note('답변을 받는 중이에요. 끝나면 실제 사용량과 지연 시간이 나와요.');

  if (usage) {
    const miss = Math.max(0, usage.input_total - usage.cache_read - usage.cache_write);
    const whole = usage.input_total || 1;
    const thinkingLabel = run.provider === 'gemini' ? '사고 토큰(출력과 별도)' : '출력 중 사고 토큰';
    html += `
      <section class="panel-block">
        <h3>실제 입력 토큰</h3>
        <div class="big-number">${fmt(usage.input_total)}</div>
        <div class="stack thin">
          <span class="seg-read" style="width:${(usage.cache_read / whole) * 100}%"></span>
          <span class="seg-write" style="width:${(usage.cache_write / whole) * 100}%"></span>
          <span class="seg-miss" style="width:${(miss / whole) * 100}%"></span>
        </div>
        <div class="legend">
          <i class="seg-read"></i><span>캐시에서 읽음</span><span class="num">${fmt(usage.cache_read)} (${pct(usage.cache_read, usage.input_total)})</span>
          <i class="seg-write"></i><span>캐시에 씀</span><span class="num">${fmt(usage.cache_write)}</span>
          <i class="seg-miss"></i><span>캐시 안 됨</span><span class="num">${fmt(miss)}</span>
        </div>
        <dl class="kv" style="margin-top:10px">
          <dt>추정 대비 실측</dt><dd>×${(usage.input_total / (est.total || 1)).toFixed(2)}</dd>
          <dt>출력 토큰</dt><dd>${fmt(usage.output)}</dd>
          ${usage.thinking ? `<dt>${thinkingLabel}</dt><dd>${fmt(usage.thinking)}</dd>` : ''}
        </dl>
        ${cacheNote(usage, run)}
      </section>`;
  } else if (!pending) {
    html += `<section class="panel-block"><h3>실제 입력 토큰</h3>${note('사용량 정보가 없어요. 중간에 멈췄거나 검수로 스트림을 끊은 경우예요.')}</section>`;
  }

  if (meta.cost) {
    const saved = meta.cost.no_cache_usd ? 1 - meta.cost.usd / meta.cost.no_cache_usd : 0;
    html += `
      <section class="panel-block">
        <h3>비용</h3>
        <dl class="kv">
          <dt>이번 턴</dt><dd>${usd(meta.cost.usd)}</dd>
          <dt>캐시가 없었다면</dt><dd>${usd(meta.cost.no_cache_usd)}</dd>
          <dt>캐시 효과</dt><dd>${saved >= 0 ? `${Math.round(saved * 100)}% 절약` : `${Math.round(-saved * 100)}% 더 듦`}</dd>
        </dl>
        ${saved < 0 ? note('처음 캐시에 쓰는 턴은 쓰기 비용(1.25배) 때문에 오히려 비싸요. 다음 턴부터 읽기(0.1배)로 돌려받아요.') : ''}
        ${run.provider === 'mock' ? note('mock은 Claude Haiku 5.5 가격으로 흉내 낸 값이에요.') : ''}
      </section>`;
  }

  if (meta.latency) {
    const lat = meta.latency;
    html += `
      <section class="panel-block">
        <h3>지연 시간</h3>
        <dl class="kv">
          <dt>첫 토큰 (제공사)</dt><dd>${ms(lat.ttft_ms)}</dd>
          <dt>첫 표시 (검수 버퍼 뒤)</dt><dd>${ms(lat.first_visible_ms)}</dd>
          <dt>전체</dt><dd>${ms(lat.total_ms)}</dd>
          <dt>입력 검수</dt><dd>${ms(lat.input_moderation_ms)}</dd>
          <dt>출력 사후 검수</dt><dd>${ms(lat.output_moderation_ms)}</dd>
        </dl>
      </section>`;
  }

  const w = d.window;
  html += `
    <section class="panel-block">
      <h3>대화 창</h3>
      <dl class="kv">
        <dt>전략</dt><dd>${STRATEGY_LABELS[w.strategy] || w.strategy}</dd>
        <dt>보낸 대화</dt><dd>${fmt(w.included)}개</dd>
        <dt>잘린 대화</dt><dd>${fmt(w.dropped)}개 (${fmt(w.dropped_tokens)}토큰)</dd>
        <dt>대화 예산</dt><dd>${fmt(w.history_budget)}</dd>
        <dt>요약 지점 이후</dt><dd>${fmt(w.since_tokens)}토큰</dd>
        <dt>요약</dt><dd>${d.summary ? `#${d.summary.id} 사용 (${fmt(d.summary.tokens)}토큰)` : '없음'}</dd>
      </dl>
      ${w.overflow ? '<p class="panel-warn">예산을 넘어서 오래된 메시지를 잘랐어요. 답변이 끝나면 백그라운드에서 요약이 예약돼요.</p>' : ''}
      ${(d.warnings || []).map((text) => `<p class="panel-warn">${escapeHtml(text)}</p>`).join('')}
    </section>`;

  const lore = d.lore;
  const chips = [
    ...lore.constant.map((name) => `<span class="lore-chip" title="항상 들어가는 항목(고정 프롬프트)">${escapeHtml(name)} <small>항상</small></span>`),
    ...lore.hits.map((hit) => `<span class="lore-chip hit" title="키워드 '${escapeHtml(hit.key)}'에 걸림">${escapeHtml(hit.name)} <small>${escapeHtml(hit.key)}</small></span>`),
    ...lore.dropped.map((hit) => `<span class="lore-chip dropped" title="예산 초과로 빠짐">${escapeHtml(hit.name)}</span>`),
  ];
  html += `
    <section class="panel-block">
      <h3>로어북</h3>
      ${chips.length ? `<div class="lore-chips">${chips.join('')}</div>` : note('이번 턴에 들어간 로어가 없어요.')}
      ${lore.dropped.length ? note('줄 그은 항목은 로어 예산을 넘어서 빠졌어요.') : ''}
    </section>`;

  if (run.provider) {
    html += `
      <section class="panel-block">
        <h3>실행 설정</h3>
        <dl class="kv">
          <dt>제공사</dt><dd>${escapeHtml(run.provider)}</dd>
          <dt>모델</dt><dd>${escapeHtml(run.model)}</dd>
          <dt>캐시 방식</dt><dd>${CACHE_LABELS[run.cache_mode] || run.cache_mode}</dd>
          <dt>검수</dt><dd>${escapeHtml(run.moderation_mode)}, ${escapeHtml(run.moderation_backend)}</dd>
          <dt>출력 버퍼</dt><dd>${escapeHtml(run.output_buffer)}</dd>
          ${meta.finish_reason ? `<dt>종료 이유</dt><dd>${FINISH_LABELS[meta.finish_reason] || escapeHtml(meta.finish_reason)}</dd>` : ''}
        </dl>
        ${meta.finish_detail ? note(escapeHtml(meta.finish_detail)) : ''}
        ${meta.error ? `<p class="panel-warn">${escapeHtml(meta.error)}</p>` : ''}
      </section>`;
  }

  if (messageId) {
    html += `
      <details class="raw">
        <summary>보낸 요청 원문 보기</summary>
        <pre>불러오는 중이에요…</pre>
      </details>`;
  }
  return html;
}

async function renderCompareTab() {
  const box = $('#tab-compare');
  if (!state.chatId) {
    box.innerHTML = note('대화를 고르면 설정별 결과를 나란히 비교해 보여 줘요.');
    return;
  }
  box.innerHTML = note('불러오는 중이에요…');
  try {
    const data = await api(`/api/chats/${state.chatId}/stats`);
    if (!data.groups.length) {
      box.innerHTML = note('아직 사용량이 기록된 답변이 없어요.');
      return;
    }
    const rows = data.groups.map((g) => `
      <tr>
        <td>${escapeHtml(g.model)}<br><span class="muted small-text">${STRATEGY_LABELS[g.strategy] || g.strategy}, 캐시 ${CACHE_LABELS[g.cache_mode] || g.cache_mode}</span></td>
        <td>${g.turns}</td>
        <td>${fmt(g.avg_input)}</td>
        <td>${Math.round(g.cache_hit_ratio * 100)}%</td>
        <td>${g.has_cost ? usd(g.usd) : '–'}</td>
        <td>${ms(g.avg_ttft_ms)}</td>
      </tr>`).join('');
    box.innerHTML = `
      <section class="panel-block">
        <h3>설정별 비교 (이 대화의 모든 가지)</h3>
        <table class="compare">
          <thead><tr><th>설정</th><th>턴</th><th>입력</th><th>캐시</th><th>비용</th><th>첫 토큰</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
        ${note('입력은 턴당 평균 입력 토큰, 캐시는 입력 중 캐시에서 읽은 비율, 비용은 합계, 첫 토큰은 평균이에요.')}
        ${note('같은 대화에서 설정(창 전략, 캐시 방식, 모델)을 바꿔 가며 보내면 행이 나뉘어요. 비용은 가격표가 있는 모델만 계산해요(app/pricing.py, pricing.json).')}
      </section>
      ${state.config?.settings.provider === 'mock' ? `
      <section class="panel-block">
        <h3>mock 캐시</h3>
        ${note('mock의 흉내 캐시는 서버가 켜져 있는 동안 모든 대화가 함께 써요(실제 Anthropic도 앞부분이 같으면 다른 대화끼리 캐시를 공유해요). 설정을 비교하기 전에 비우면 앞선 실험의 영향을 없앨 수 있어요.')}
        <button class="btn ghost small" type="button" id="btn-clear-mock-cache">mock 캐시 비우기</button>
      </section>` : ''}`;
    $('#btn-clear-mock-cache')?.addEventListener('click', async () => {
      const result = await api('/api/mock/cache/clear', { method: 'POST' });
      toast(`mock 캐시 항목 ${result.cleared}개를 비웠어요.`);
    });
  } catch (error) {
    box.innerHTML = note(escapeHtml(error.message));
  }
}

function renderSummaryTab() {
  const box = $('#tab-summary');
  const view = state.view;
  if (!view) {
    box.innerHTML = note('대화를 고르면 요약 메모리 상태가 여기 나와요.');
    return;
  }
  const summary = view.summary;
  const strategy = state.config?.settings.window_strategy;
  box.innerHTML = `
    <section class="panel-block">
      <h3>지금 쓰이는 요약</h3>
      ${view.summarizing ? note('백그라운드에서 요약하는 중이에요.') : ''}
      ${view.summary_error ? `<p class="panel-warn">마지막 요약 실패: ${escapeHtml(view.summary_error)}</p>` : ''}
      ${summary ? `
        ${note(`메시지 #${summary.covers_until_msg_id}까지를 덮는 요약이에요. ${escapeHtml(summary.meta?.model || '')}로 메시지 ${summary.meta?.messages ?? '?'}개를 요약했어요.`)}
        <div class="summary-text">${escapeHtml(summary.content)}</div>` : note('아직 요약이 없어요. 창 전략이 요약 고정점일 때, 요약 지점 이후 대화가 예산의 일정 비율을 넘으면 답변이 끝난 뒤 자동으로 만들어져요.')}
    </section>
    <section class="panel-block">
      <button class="btn ghost small" type="button" id="btn-summarize" ${strategy !== 'anchored' || view.summarizing ? 'disabled' : ''}>지금 요약하기</button>
      ${note(strategy !== 'anchored'
        ? '지금 창 전략에서는 요약을 쓰지 않아요. 설정에서 요약 고정점으로 바꾸면 쓸 수 있어요.'
        : '마지막 두 번의 주고받기만 남기고 그 앞을 요약해요. 요약이 바뀌면 요약 블록부터 뒤쪽 캐시가 깨져요.')}
      ${note(`이 대화의 요약은 모두 ${view.summaries}개예요. 대화 가지마다 다른 요약이 쓰일 수 있어요.`)}
    </section>`;
  $('#btn-summarize')?.addEventListener('click', async (event) => {
    event.target.disabled = true;
    try {
      await api(`/api/chats/${state.chatId}/summarize`, { method: 'POST' });
      state.view = await api(`/api/chats/${state.chatId}`);
      renderSummaryTab();
    } catch (error) {
      toast(error.message);
      event.target.disabled = false;
    }
  });
}

async function renderModTab() {
  const box = $('#tab-mod');
  if (!state.chatId) {
    box.innerHTML = note('대화를 고르면 그 대화의 검수 기록이 나와요.');
    return;
  }
  try {
    const events = await api(`/api/chats/${state.chatId}/mod-events`);
    const items = events.map((ev) => `
      <div class="mod-item">
        <div class="row1">
          <span><b>${STAGE_LABELS[ev.stage] || ev.stage}</b> ${escapeHtml(ev.category)}, ${ACTION_LABELS[ev.action] || ev.action}</span>
          <span class="muted small-text">${escapeHtml(ev.backend)} ${relativeTime(ev.created_at)}</span>
        </div>
        <div class="excerpt">${escapeHtml(ev.detail?.matched ? `걸린 표현: ${ev.detail.matched}` : (ev.detail?.reason || ''))}</div>
        <div class="excerpt">${escapeHtml((ev.excerpt || '').slice(0, 140))}</div>
      </div>`).join('');
    box.innerHTML = `
      <section class="panel-block">
        <h3>검수 기록</h3>
        ${items || note('아직 검수에 걸린 일이 없어요.')}
      </section>
      ${note('규칙은 moderation_rules.json에 있어요. 저장하면 다음 검사부터 바로 반영돼요. 입력에 [[검수테스트]]를 넣거나 mock에서 /mod-test-output을 보내 흐름을 확인할 수 있어요.')}`;
  } catch (error) {
    box.innerHTML = note(escapeHtml(error.message));
  }
}

/* ── 새 대화 ──────────────────────────────── */
function openNewChat() {
  if (!state.characterId) {
    toast('먼저 캐릭터를 고르세요.');
    return;
  }
  const character = state.characters.find((c) => c.id === state.characterId);
  const form = $('#form-chat');
  $('#chat-dlg-char').textContent = `${character.name}와(과) 새 대화를 시작해요.`;
  form.elements.persona_name.value = store.get('personaName', '지우');
  form.elements.persona_desc.value = store.get('personaDesc', '');
  form.elements.title.value = '';
  $('#dlg-chat').showModal();
  form.elements.persona_name.focus();
}

async function submitNewChat(event) {
  event.preventDefault();
  const form = event.target;
  const body = {
    character_id: state.characterId,
    persona_name: form.elements.persona_name.value.trim(),
    persona_desc: form.elements.persona_desc.value.trim(),
    title: form.elements.title.value.trim(),
  };
  store.set('personaName', body.persona_name);
  store.set('personaDesc', body.persona_desc);
  try {
    const view = await api('/api/chats', { json: body });
    $('#dlg-chat').close();
    await refreshChatList();
    await openChat(view.chat.id);
    loadCharacters();
    $('#input').focus();
  } catch (error) {
    toast(error.message);
  }
}

/* ── 설정 ─────────────────────────────────── */
function settingField(key, settings, config) {
  const info = FIELD_INFO[key] || { label: key };
  const help = info.help ? `<span class="help">${escapeHtml(info.help)}</span>` : '';
  if (key === 'provider') {
    const options = Object.keys(PROVIDER_LABELS).map((p) => {
      const ok = config.providers[p];
      return `<option value="${p}" ${p === settings.provider ? 'selected' : ''} ${ok ? '' : 'disabled'}>${PROVIDER_LABELS[p]}${ok ? '' : ' (.env 필요)'}</option>`;
    }).join('');
    return `<label class="field"><span>${info.label}</span><select name="provider">${options}</select>${help}</label>`;
  }
  if (key === 'model') {
    const provider = settings.provider;
    const value = settings.models?.[provider] || '';
    return `<label class="field"><span>${info.label}</span><input name="model" value="${escapeHtml(value)}" placeholder="${escapeHtml(config.default_models[provider] || '모델 이름')}">${help}</label>`;
  }
  const spec = config.spec[key];
  if (spec.type === 'choice') {
    const options = spec.choices.map((choice) =>
      `<option value="${choice}" ${choice === settings[key] ? 'selected' : ''}>${escapeHtml(info.options?.[choice] || choice)}</option>`).join('');
    return `<label class="field"><span>${info.label}</span><select name="${key}">${options}</select>${help}</label>`;
  }
  const step = info.step || (spec.type === 'int' ? 1 : 0.01);
  const value = settings[key] === null || settings[key] === undefined ? '' : settings[key];
  return `<label class="field"><span>${info.label}</span><input name="${key}" type="number" step="${step}" min="${spec.min}" max="${spec.max}" value="${value}" ${spec.type === 'float_or_none' ? 'placeholder="기본값"' : 'required'}>${help}</label>`;
}

function renderSettingsForm() {
  const config = state.config;
  const settings = config.settings;
  $('#settings-fields').innerHTML = SETTING_GROUPS.map((group) => `
    <section class="settings-group">
      <h3>${group.title}</h3>
      <div class="settings-grid">${group.fields.map((key) => settingField(key, settings, config)).join('')}</div>
    </section>`).join('') + `
    <p class="panel-note">API 키와 엔드포인트, Claude의 effort·thinking, Gemini의 thinking level과 안전 필터는 .env에서 바꿔요(서버 재시작 필요).
    지금 값: Claude effort ${escapeHtml(config.anthropic.effort || '기본')}, thinking ${escapeHtml(config.anthropic.thinking || '기본')} /
    Gemini thinking ${escapeHtml(config.gemini.thinking_level || '기본')}, 안전 필터 ${escapeHtml(config.gemini.safety_threshold || '기본')}</p>`;
  const form = $('#form-settings');
  form.elements.provider.addEventListener('change', () => {
    const provider = form.elements.provider.value;
    form.elements.model.value = settings.models?.[provider] || '';
    form.elements.model.placeholder = config.default_models[provider] || '모델 이름';
  });
}

function openSettings() {
  if (!state.config) return;
  $('#settings-error').hidden = true;
  renderSettingsForm();
  $('#dlg-settings').showModal();
}

async function submitSettings(event) {
  event.preventDefault();
  const form = event.target;
  const config = state.config;
  const body = {};
  const provider = form.elements.provider.value;
  body.provider = provider;
  body.models = { [provider]: form.elements.model.value.trim() };
  for (const [key, spec] of Object.entries(config.spec)) {
    if (key === 'provider' || key === 'models' || !form.elements[key]) continue;
    const raw = form.elements[key].value;
    if (spec.type === 'choice') body[key] = raw;
    else if (spec.type === 'int') body[key] = parseInt(raw, 10);
    else if (spec.type === 'float') body[key] = parseFloat(raw);
    else if (spec.type === 'float_or_none') body[key] = raw === '' ? null : parseFloat(raw);
  }
  try {
    await api('/api/settings', { method: 'PUT', json: body });
    state.config = await api('/api/config');
    $('#dlg-settings').close();
    renderModelChip();
    renderInspector();
  } catch (error) {
    $('#settings-error').textContent = error.message;
    $('#settings-error').hidden = false;
  }
}

async function resetSettings() {
  if (!window.confirm('설정을 .env 기본값으로 되돌릴까요?')) return;
  await api('/api/settings/reset', { method: 'POST' });
  state.config = await api('/api/config');
  renderSettingsForm();
  renderModelChip();
}

/* ── 캐릭터 편집 ───────────────────────────── */
const TEXT_FIELDS = ['name', 'description', 'personality', 'scenario', 'first_mes', 'mes_example',
  'system_prompt', 'post_history_instructions', 'creator_notes'];

function addLoreEntry(entry = {}) {
  const node = $('#tpl-lore').content.firstElementChild.cloneNode(true);
  const values = {
    name: '', keys: [], secondary_keys: [], content: '', enabled: true, constant: false, selective: false,
    case_sensitive: false, priority: 10, insertion_order: 100, ...entry,
  };
  for (const input of $$('[data-k]', node)) {
    const key = input.dataset.k;
    if (input.type === 'checkbox') input.checked = Boolean(values[key]);
    else if (Array.isArray(values[key])) input.value = values[key].join(', ');
    else input.value = values[key];
  }
  node.querySelector('[data-remove]').addEventListener('click', () => node.remove());
  $('#lore-list').appendChild(node);
  return node;
}

function collectLore() {
  const splitKeys = (text) => text.split(',').map((k) => k.trim()).filter(Boolean);
  return $$('#lore-list .lore-entry').map((node) => {
    const get = (key) => node.querySelector(`[data-k="${key}"]`);
    return {
      name: get('name').value.trim(),
      keys: splitKeys(get('keys').value),
      secondary_keys: splitKeys(get('secondary_keys').value),
      content: get('content').value,
      enabled: get('enabled').checked,
      constant: get('constant').checked,
      selective: get('selective').checked,
      case_sensitive: get('case_sensitive').checked,
      priority: parseInt(get('priority').value || '10', 10),
      insertion_order: parseInt(get('insertion_order').value || '100', 10),
    };
  });
}

async function openCharacterEditor(id = null) {
  state.editingCharacterId = id;
  const form = $('#form-character');
  form.reset();
  $('#lore-list').innerHTML = '';
  $('#character-error').hidden = true;
  let character = { alternate_greetings: [], tags: [], lore: [] };
  if (id) {
    try {
      character = await api(`/api/characters/${id}`);
    } catch (error) {
      toast(error.message);
      return;
    }
  }
  for (const key of TEXT_FIELDS) form.elements[key].value = character[key] || '';
  form.elements.alternate_greetings.value = (character.alternate_greetings || []).join('\n---\n');
  form.elements.tags.value = (character.tags || []).join(', ');
  (character.lore || []).forEach((entry) => addLoreEntry(entry));
  $('#character-dlg-title').textContent = id ? `캐릭터 편집: ${character.name}` : '새 캐릭터';
  $('#btn-delete-character').hidden = !id;
  const exportLink = $('#btn-export-character');
  exportLink.hidden = !id;
  if (id) exportLink.href = `/api/characters/${id}/export`;
  $('#dlg-character').showModal();
}

async function submitCharacter(event) {
  event.preventDefault();
  const form = event.target;
  const body = {};
  for (const key of TEXT_FIELDS) body[key] = form.elements[key].value;
  body.name = body.name.trim();
  body.alternate_greetings = form.elements.alternate_greetings.value
    .split(/^\s*---\s*$/m).map((text) => text.trim()).filter(Boolean);
  body.tags = form.elements.tags.value.split(',').map((t) => t.trim()).filter(Boolean);
  body.lore = collectLore();
  const id = state.editingCharacterId;
  try {
    const saved = await api(id ? `/api/characters/${id}` : '/api/characters', { method: id ? 'PUT' : 'POST', json: body });
    $('#dlg-character').close();
    await loadCharacters();
    if (!id || id === state.characterId) await selectCharacter(saved.id, { restoreChat: true });
  } catch (error) {
    $('#character-error').textContent = error.message;
    $('#character-error').hidden = false;
  }
}

async function deleteCharacter() {
  const id = state.editingCharacterId;
  if (!id || !window.confirm('이 캐릭터와 모든 대화를 삭제할까요? 되돌릴 수 없어요.')) return;
  await api(`/api/characters/${id}`, { method: 'DELETE' });
  $('#dlg-character').close();
  await loadCharacters();
  const next = state.characters[0];
  if (next) await selectCharacter(next.id);
  else {
    state.characterId = null;
    state.chatId = null;
    state.view = null;
    renderChats();
    renderStage();
    renderInspector();
  }
}

async function importCard(event) {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  const data = new FormData();
  data.append('file', file);
  try {
    const character = await api('/api/characters/import', { body: data });
    await loadCharacters();
    await selectCharacter(character.id);
    toast(`카드를 가져왔어요: ${character.name} (로어 ${character.lore.length}개)`);
  } catch (error) {
    toast(`가져오지 못했어요: ${error.message}`);
  }
}

/* ── 이벤트 연결 ───────────────────────────── */
function autoResize() {
  const input = $('#input');
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
}

function bindEvents() {
  $('#composer').addEventListener('submit', (event) => {
    event.preventDefault();
    send();
  });
  $('#input').addEventListener('keydown', (event) => {
    // 한글 조합 중 Enter는 무시한다(조합이 끝나지 않은 글자가 보내지는 문제 방지)
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      send();
    }
  });
  $('#input').addEventListener('input', autoResize);
  $('#btn-stop').addEventListener('click', () => state.streaming?.controller.abort());
  $('#btn-new-chat').addEventListener('click', openNewChat);
  $('#btn-new-character').addEventListener('click', () => openCharacterEditor(null));
  $('#input-import').addEventListener('change', importCard);
  $('#btn-settings').addEventListener('click', openSettings);
  $('#chip-model').addEventListener('click', openSettings);
  $('#form-chat').addEventListener('submit', submitNewChat);
  $('#form-settings').addEventListener('submit', submitSettings);
  $('#btn-settings-reset').addEventListener('click', resetSettings);
  $('#form-character').addEventListener('submit', submitCharacter);
  $('#btn-add-lore').addEventListener('click', () => addLoreEntry().querySelector('[data-k="name"]').focus());
  $('#btn-delete-character').addEventListener('click', deleteCharacter);
  $$('[data-close]').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
  $$('.tab').forEach((tab) => tab.addEventListener('click', () => {
    state.tab = tab.dataset.tab;
    renderInspector();
  }));
  $('#btn-toggle-sidebar').addEventListener('click', () => $('#app').classList.toggle('show-sidebar'));
  $('#btn-toggle-inspector').addEventListener('click', () => $('#app').classList.toggle('show-inspector'));
  $('#btn-close-inspector').addEventListener('click', () => $('#app').classList.remove('show-inspector'));
  $('#messages').addEventListener('click', () => $('#app').classList.remove('show-sidebar', 'show-inspector'));
}

async function init() {
  bindEvents();
  setComposerBusy(false);
  try {
    state.config = await api('/api/config');
    renderModelChip();
    await loadCharacters();
    const lastCharacterId = store.get('characterId');
    const first = state.characters.find((c) => c.id === lastCharacterId) || state.characters[0];
    if (first) await selectCharacter(first.id, { restoreChat: true });
    else {
      renderStage();
      renderInspector();
    }
  } catch (error) {
    toast(`서버와 통신하지 못했어요: ${error.message}`);
  }
}

init();
