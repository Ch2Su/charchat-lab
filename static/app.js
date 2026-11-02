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
  experiments: [],
  experimentId: null,
  experimentPoll: null,
  experimentRender: 0,
  experimentSnapshot: '',
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
  { title: '모델', fields: ['provider', 'model', 'run_label'] },
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
  run_label: { label: '실험 라벨', help: '비교 탭에서 이 이름으로 행을 나눠요. 비우면 라벨 없이 기록해요.' },
};

// 비교 표: 행을 나누는 실행 조건(서버의 GROUP_FIELDS)과, 조건 이름·값을 짧게 보여 줄 때 쓰는 표
const GROUP_FIELDS = ['label', 'provider', 'model', 'strategy', 'cache_mode', 'input_token_budget',
  'max_output_tokens', 'temperature', 'moderation_backend', 'output_buffer', 'options', 'prompt_version'];
const GROUP_FIELD_INFO_KEYS = { label: 'run_label', strategy: 'window_strategy' };
const GROUP_VALUE_LABELS = {
  strategy: STRATEGY_LABELS,
  cache_mode: CACHE_LABELS,
  moderation_backend: { rules: '규칙', llm: 'LLM 판정' },
  output_buffer: { sentence: '문장 단위', none: '바로 표시' },
};
const AUX_LABELS = { judge_input: '입력', judge_output: '출력' };
// 답변 품질 어림 지표(app/quality.py)의 깃발과 말투
const QUALITY_FLAG_LABELS = {
  foreign: '외국 문자', latin: '영문 섞임', speech_shift: '말투 변화', repeat_prev: '반복', repeat_self: '반복',
};
const SPEECH_LABELS = { polite: '존댓말', plain: '반말', mixed: '섞임' };

const EXPERIMENT_STATUS = {
  running: '실행 중', done: '끝남', error: '오류', cancelled: '취소됨', interrupted: '서버가 꺼져 끊김', pending: '대기',
};
// '실험 2 예시 채우기'가 넣는 값. README 실험 2(창 전략 비교)를 하람과 실행기로 다시 해 볼 때 쓴다
const SAMPLE_SCRIPT = [
  '안녕하세요, 비 좀 피해도 될까요?', '고마워요. 수건 잘 쓸게요.', '차 한 잔 주시면 감사하죠.', '서점 이름이 물푸레예요?',
  '저 고양이 이름이 뭐예요?', '책 구경해도 돼요?', '이 책은 얼마나 오래된 거예요?', '안쪽 서가에 있는 책들은 뭐예요?',
  '누가 맡겨 둔 책인가요?', '이 서점은 언제 생겼어요?', '할머니 얘기도 듣고 싶어요.', '밤이 되면 책 속 글자가 움직인다는 소문이 있던데요.',
  '정말이에요? 문장이 종이 위를 걸어 다닌다고요?', '2층에는 뭐가 있어요?', '비가 그친 것 같아요.', '다음에 또 와도 돼요?',
];
const SAMPLE_VARIANTS = ['full', 'sliding', 'anchored'].map((strategy) => ({
  label: strategy, settings: { window_strategy: strategy, input_token_budget: 3000 },
}));

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
// 0~1 비율을 퍼센트로. 외국 문자처럼 작은 값은 소수 한 자리까지
const ratio = (value) => {
  if (value === null || value === undefined) return '–';
  const percent = value * 100;
  return `${percent < 10 && percent > 0 ? percent.toFixed(1) : Math.round(percent)}%`;
};
const speechText = (level) => SPEECH_LABELS[level] || '판단 못 함';
// 사고 분량과 시간. 내용을 주지 않는 제공사(Claude의 생략된 사고 등)는 글자 수 없이 시간만
const thinkingText = (t) => [t.chars ? `${fmt(t.chars)}자` : '', ms(t.ms)].filter(Boolean).join(' · ');

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
  // 라벨을 켜 둔 채 잊으면 다음 턴들도 그 라벨로 묶이므로 늘 보이게 둔다
  const label = settings.run_label ? ` · 라벨 ${escapeHtml(settings.run_label)}` : '';
  $('#chip-model').innerHTML = `<strong>${escapeHtml(config.model || '모델 미지정')}</strong> ${STRATEGY_LABELS[settings.window_strategy]}${label}`;
  const hint = $('#composer-hint');
  const parts = ['Enter로 보내고 Shift+Enter로 줄을 바꿔요.'];
  if (settings.provider === 'mock') parts.push('mock 테스트 명령: /long, /mod-test-output, /error, /slow, /think, /think-leak, /quality-test');
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
  if (meta.thinking) items.push(`사고 <b>${ms(meta.thinking.ms)}</b>`);
  if (meta.latency?.ttft_ms !== null && meta.latency?.ttft_ms !== undefined) {
    items.push(`첫 토큰 <b>${ms(meta.latency.ttft_ms)}</b>`);
  }
  const spans = items.map((html) => {
    const span = document.createElement('span');
    span.className = 'metric';
    span.innerHTML = html;
    return span;
  });
  return [...spans, ...qualityFlags(meta.quality)];
}

/* 답변 품질 깃발. 반복은 직전 답과 겹침, 답 안 반복 둘 중 하나만 걸려도 한 번만 붙인다 */
function qualityFlags(quality) {
  if (!quality?.flags?.length) return [];
  const titles = {
    '외국 문자': `한자·가나 등 외국 문자 ${ratio(quality.foreign_ratio)}`,
    '영문 섞임': `로마자 ${ratio(quality.latin_ratio)}`,
    '말투 변화': `직전 답들은 ${speechText(quality.speech_prev)}, 이번 답은 ${speechText(quality.speech?.level)}`,
    '반복': `직전 답과 겹침 ${ratio(quality.repeat_prev)}, 답 안 반복 ${ratio(quality.repeat_self)}`,
  };
  return [...new Set(quality.flags.map((f) => QUALITY_FLAG_LABELS[f] || f))].map((text) => {
    const span = flag(text);
    span.title = `${titles[text] || text} (글자 모양과 어미만 보는 어림 지표)`;
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
      thinking: (data) => {
        if (!text) live.querySelector('.msg-tools').innerHTML = `<span class="metric">생각하는 중 · ${thinkingText(data)}</span>`;
      },
      reset: (data) => {
        text = '';
        liveBody.innerHTML = '';
        live.querySelector('.msg-tools').innerHTML = `<span class="metric">답변에 섞여 나온 사고를 지웠어요 · ${thinkingText(data)}</span>`;
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
  for (const id of ['turn', 'compare', 'summary', 'mod', 'experiments']) $(`#tab-${id}`).hidden = id !== state.tab;
  if (state.tab === 'turn') renderTurnTab();
  else if (state.tab === 'compare') renderCompareTab();
  else if (state.tab === 'summary') renderSummaryTab();
  else if (state.tab === 'experiments') renderExperimentsTab();
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
          ${usage.thinking ? `<dt>${thinkingLabel}${usage.raw?.thinking_estimated ? ' (추정)' : ''}</dt><dd>${fmt(usage.thinking)}</dd>` : ''}
          ${meta.thinking?.chars ? `<dt>사고 분량</dt><dd>${fmt(meta.thinking.chars)}자</dd>` : ''}
        </dl>
        ${cacheNote(usage, run)}
      </section>`;
  } else if (!pending) {
    html += `<section class="panel-block"><h3>실제 입력 토큰</h3>${note('사용량 정보가 없어요. 중간에 멈췄거나 검수로 스트림을 끊은 경우예요.')}</section>`;
  }

  const aux = meta.aux || [];
  const unpriced = Boolean(usage) && !meta.cost && run.provider !== 'mock'; // 사용량은 있는데 가격표에 없는 모델
  if (meta.cost || aux.length || unpriced) {
    const saved = meta.cost?.no_cache_usd ? 1 - meta.cost.usd / meta.cost.no_cache_usd : 0;
    html += `
      <section class="panel-block">
        <h3>비용</h3>
        <dl class="kv">
          ${meta.cost ? `
          <dt>이번 턴</dt><dd>${usd(meta.cost.usd)}</dd>
          <dt>캐시가 없었다면</dt><dd>${usd(meta.cost.no_cache_usd)}</dd>
          <dt>캐시 효과</dt><dd>${saved >= 0 ? `${Math.round(saved * 100)}% 절약` : `${Math.round(-saved * 100)}% 더 듦`}</dd>` : ''}
          ${aux.map((a) => `<dt>검수 판정 (${AUX_LABELS[a.purpose] || escapeHtml(a.purpose)})</dt><dd>${a.cost ? usd(a.cost.usd) : '–'} · ${ms(a.latency_ms)}</dd>`).join('')}
        </dl>
        ${saved < 0 ? note('처음 캐시에 쓰는 턴은 쓰기 비용(1.25배) 때문에 오히려 비싸요. 다음 턴부터 읽기(0.1배)로 돌려받아요.') : ''}
        ${aux.length ? note('검수 판정은 대화와 따로 부르는 LLM 호출이에요. 이번 턴 비용에는 들어가지 않고, 비교 탭의 비용 합계에는 들어가요.') : ''}
        ${unpriced ? note(`${escapeHtml(run.provider || '')} · ${escapeHtml(run.model || '')}은 가격표에 없어서 대화 비용을 계산하지 않았어요. 로컬 모델은 0으로 두면 되고, 다른 모델은 pricing.json에 가격을 추가하세요(app/pricing.py).`) : ''}
        ${run.provider === 'mock' ? note('mock은 Claude Haiku 5.5 가격으로 흉내 낸 값이에요.') : ''}
      </section>`;
  }

  if (meta.latency) {
    const lat = meta.latency;
    const think = meta.thinking;
    const mostlyThinking = think && lat.total_ms && think.ms / lat.total_ms > 0.5;
    html += `
      <section class="panel-block">
        <h3>지연 시간</h3>
        <dl class="kv">
          ${think ? `<dt>사고</dt><dd>${ms(think.ms)}</dd>` : ''}
          <dt>첫 토큰 (제공사)</dt><dd>${ms(lat.ttft_ms)}</dd>
          <dt>첫 표시 (검수 버퍼 뒤)</dt><dd>${ms(lat.first_visible_ms)}</dd>
          <dt>전체</dt><dd>${ms(lat.total_ms)}</dd>
          <dt>입력 검수</dt><dd>${ms(lat.input_moderation_ms)}</dd>
          <dt>출력 사후 검수</dt><dd>${ms(lat.output_moderation_ms)}</dd>
        </dl>
        ${mostlyThinking ? note(run.provider === 'openai_compat'
          ? '답변 시간의 절반 넘게 사고에 썼어요. 작은 로컬 모델이라면 .env에 OPENAI_COMPAT_REASONING_EFFORT=none을 넣어 사고를 꺼 보세요.'
          : '답변 시간의 절반 넘게 사고에 썼어요. 첫 토큰을 빠르게 하려면 .env에서 사고 수준(effort, thinking level)을 낮춰 보세요.') : ''}
      </section>`;
  }

  const q = meta.quality;
  if (q) {
    const flagged = [...new Set((q.flags || []).map((f) => QUALITY_FLAG_LABELS[f] || f))];
    html += `
      <section class="panel-block">
        <h3>답변 품질 <span class="muted small-text">어림</span></h3>
        <dl class="kv">
          <dt>외국 문자 (한자·가나 등)</dt><dd>${ratio(q.foreign_ratio)}</dd>
          <dt>로마자</dt><dd>${ratio(q.latin_ratio)}</dd>
          <dt>말투 (이번 / 직전)</dt><dd title="존댓말 어미 ${q.speech.polite}개, 반말 어미 ${q.speech.plain}개">${speechText(q.speech.level)} / ${speechText(q.speech_prev)}</dd>
          <dt>직전 답과 겹침</dt><dd>${ratio(q.repeat_prev)}</dd>
          <dt>답 안 반복</dt><dd>${ratio(q.repeat_self)}</dd>
        </dl>
        ${flagged.length ? `<p class="panel-warn">깃발: ${flagged.map(escapeHtml).join(', ')}</p>` : ''}
        ${note('글자 모양과 어미만 보는 어림 지표예요. 판단은 직접 읽고 하세요.')}
        ${run.provider === 'mock' ? note('mock 답변은 정해진 문장 조각을 이어 붙여서 직전 답과 겹침이 높게 나오기 쉬워요. /quality-test로 깃발을 확인할 수 있어요.') : ''}
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
    const options = Object.entries(run.options || {}).map(([k, v]) => `${k} ${v ?? '기본'}`).join(', ');
    html += `
      <section class="panel-block">
        <h3>실행 설정</h3>
        <dl class="kv">
          ${run.label ? `<dt>실험 라벨</dt><dd>${escapeHtml(run.label)}</dd>` : ''}
          <dt>제공사</dt><dd>${escapeHtml(run.provider)}</dd>
          <dt>모델</dt><dd>${escapeHtml(run.model)}</dd>
          ${run.input_token_budget ? `<dt>입력 예산</dt><dd>${fmt(run.input_token_budget)}</dd>` : ''}
          ${run.max_output_tokens ? `<dt>최대 출력</dt><dd>${fmt(run.max_output_tokens)}</dd>` : ''}
          <dt>캐시 방식</dt><dd>${CACHE_LABELS[run.cache_mode] || run.cache_mode}</dd>
          <dt>검수</dt><dd>${escapeHtml(run.moderation_mode)}, ${escapeHtml(run.moderation_backend)}</dd>
          <dt>출력 버퍼</dt><dd>${escapeHtml(run.output_buffer)}</dd>
          ${options ? `<dt>.env 옵션</dt><dd>${escapeHtml(options)}</dd>` : ''}
          ${run.prompt_version ? `<dt>프롬프트 버전</dt><dd>${escapeHtml(run.prompt_version)}</dd>` : ''}
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

/* 실행 조건 하나를 '이름 값'으로. 옛 기록에 없는 값은 '기록 없음'으로 보여 준다 */
function conditionText(field, value) {
  if (field === 'prompt_version') return value ? `프롬프트 ${value}` : '프롬프트 기록 없음';
  const name = FIELD_INFO[GROUP_FIELD_INFO_KEYS[field] || field]?.label || field;
  if (value === null || value === undefined) return `${name} ${field === 'temperature' ? '기본' : '기록 없음'}`;
  const text = GROUP_VALUE_LABELS[field]?.[value] ?? (typeof value === 'number' ? numberFormat.format(value) : value);
  return `${name} ${text}`;
}

const optionTexts = (options, keys) => (options
  ? keys.map((key) => `${key} ${options[key] ?? '기본'}`)
  : ['.env 옵션 기록 없음']);

/* 비교 표. 비교 탭(대화 하나)과 실험 탭(여러 대화)이 함께 쓴다.
   설정 칸에는 라벨과 모델, 그리고 행마다 다른 조건(varying)만 보여 주고, 같은 조건은 표 아래에 한 번만 적는다. */
function compareTable(groups, varying) {
  // .env 옵션은 묶음째 다르다고 나오므로, 그 안에서도 실제로 값이 다른 키만 고른다
  const optionKeys = [...new Set(groups.flatMap((g) => Object.keys(g.options || {})))]
    .filter((key) => new Set(groups.map((g) => JSON.stringify(g.options?.[key] ?? null))).size > 1);
  const rows = groups.map((g) => {
    const conditions = varying.filter((f) => f !== 'label' && f !== 'model')
      .flatMap((f) => (f === 'options' ? optionTexts(g.options, optionKeys) : [conditionText(f, g[f])]));
    const costParts = [`대화 ${usd(g.usd)}`];
    if (g.summary_calls) costParts.push(`요약 ${g.summary_calls}회 ${usd(g.summary_usd)}`);
    if (g.judge_calls) costParts.push(`검수 ${g.judge_calls}회 ${usd(g.judge_usd)}`);
    // 좁은 패널에서 아무 데서나 줄이 바뀌지 않게 항목마다 한 줄씩 쓴다
    const costDetail = g.has_cost && costParts.length > 1 ? `<span class="cell-sub">${costParts.join('<br>')}</span>` : '';
    const measured = g.quality_turns; // 품질 지표를 남기기 전의 턴뿐이면 0
    return `
      <tr>
        <td>${g.label ? `<b>${escapeHtml(g.label)}</b><br>` : ''}${escapeHtml(g.model ?? '모델 기록 없음')}${conditions.length
          ? `<span class="cell-sub">${conditions.map(escapeHtml).join(' · ')}</span>` : ''}</td>
        <td>${g.turns}</td>
        <td>${g.turns ? fmt(g.avg_input) : '–'}</td>
        <td>${g.input_total ? `${Math.round(g.cache_hit_ratio * 100)}%` : '–'}</td>
        <td>${g.has_cost ? usd(g.total_usd) : (g.turns ? '가격표 없음' : '–')}${costDetail}</td>
        <td>${ms(g.avg_ttft_ms)}${g.turns > 1 ? `<span class="cell-sub">중앙값 ${ms(g.median_ttft_ms)}</span>` : ''}</td>
        <td>${ms(g.avg_first_visible_ms)}${g.turns > 1 ? `<span class="cell-sub">중앙값 ${ms(g.median_first_visible_ms)}</span>` : ''}</td>
        <td>${measured ? `${ratio(g.avg_foreign_ratio)}<span class="cell-sub">낀 턴 ${g.foreign_turns}/${measured}<br>로마자 ${ratio(g.avg_latin_ratio)}</span>` : '–'}</td>
        <td>${measured ? ratio(g.avg_repeat_prev) : '–'}</td>
        <td>${measured ? `${g.speech_shift_turns}/${measured}<span class="cell-sub">깃발 ${g.flagged_turns}턴</span>` : '–'}</td>
      </tr>`;
  }).join('');
  const first = groups[0] || {};
  const common = GROUP_FIELDS
    .filter((f) => !varying.includes(f) && f !== 'label' && f !== 'model' && first[f] !== null && first[f] !== undefined)
    .flatMap((f) => (f === 'options' ? optionTexts(first.options, Object.keys(first.options)) : [conditionText(f, first[f])]));
  return `
    <div class="table-scroll">
      <table class="compare">
        <thead><tr><th>설정</th><th>턴</th><th>입력</th><th>캐시</th><th>비용</th><th>첫 토큰</th><th>첫 표시</th>
          <th>외국 문자</th><th>반복</th><th>말투 변화</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    ${common.length ? note(`모든 행에 같은 조건: ${common.map(escapeHtml).join(' · ')}`) : ''}
    ${groups.some((g) => g.quality_turns) ? note('외국 문자는 한자·가나 등의 평균 비율(아래는 로마자), 반복은 직전 답과 겹친 글자 5-gram의 평균 비율, 말투 변화는 존댓말과 반말이 뒤바뀐 턴 수예요(아래는 깃발이 붙은 턴). 글자 모양과 어미만 보는 어림 지표예요.') : ''}`;
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
    box.innerHTML = `
      <section class="panel-block">
        <h3>설정별 비교 (이 대화의 모든 가지)</h3>
        ${compareTable(data.groups, data.varying)}
        ${note('입력은 턴당 평균 입력 토큰, 캐시는 입력 중 캐시에서 읽은 비율, 비용은 대화에 요약과 LLM 검수 판정을 더한 합계, 첫 토큰과 첫 표시는 평균이에요.')}
        ${note('실험 라벨, 모델, 창 전략, 입력 예산, 캐시 방식, 출력 버퍼, .env의 사고 설정, 프롬프트 파일 중 하나라도 다르면 행이 나뉘어요. 설정 칸에는 행마다 다른 조건만 보여요. 비용은 가격표가 있는 모델만 계산해요(app/pricing.py, pricing.json).')}
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

/* ── 실험(대본 재생) 탭 ─────────────────────── */
const experimentStatus = (status) => EXPERIMENT_STATUS[status] || escapeHtml(status);

function experimentProgress(e) {
  const p = e.progress || {};
  return e.status === 'running'
    ? `변형 ${p.variant}/${p.variants} · 턴 ${p.turn}/${p.turns}`
    : `입력 ${p.turns ?? '?'}개 × 변형 ${p.variants ?? '?'}개`;
}

function experimentDetailHtml(e) {
  const p = e.progress || {};
  const done = Math.max(0, (p.variant - 1) * p.turns + p.turn);
  const share = p.variants && p.turns ? done / (p.variants * p.turns) : 0;
  const runs = e.runs.map((run) => `
    <tr>
      <td><b>${escapeHtml(run.label)}</b>${run.error ? `<span class="cell-sub">${escapeHtml(run.error)}</span>` : ''}</td>
      <td>${experimentStatus(run.status)}</td>
      <td>${run.turns_done}/${p.turns ?? '?'}</td>
      <td>${run.blocked}</td>
      <td>${run.chat_id ? `<button class="link-btn" type="button" data-open-chat="${run.chat_id}">대화 열기</button>` : '–'}</td>
    </tr>`).join('');
  const groups = e.stats?.groups || [];
  return `
    <section class="panel-block">
      <h3>${escapeHtml(e.name)} <span class="status-chip ${escapeHtml(e.status)}">${experimentStatus(e.status)}</span></h3>
      ${note(`${escapeHtml(e.character_name || '지워진 캐릭터')} · ${experimentProgress(e)}${e.spec?.reset_mock ? ' · 변형마다 mock 초기화' : ''}${e.spec?.wait_summary ? ' · 요약 기다림' : ''}`)}
      ${e.status === 'running' ? `<div class="stack thin"><span class="seg-history" style="width:${Math.round(share * 100)}%"></span></div>` : ''}
      ${e.error ? `<p class="panel-warn">${escapeHtml(e.error)}</p>` : ''}
      <table class="compare runs">
        <thead><tr><th>변형</th><th>상태</th><th>턴</th><th>막힘</th><th></th></tr></thead>
        <tbody>${runs}</tbody>
      </table>
      <div class="row-actions">
        ${e.status === 'running'
          ? '<button class="btn ghost small" type="button" id="btn-cancel-experiment">취소</button>'
          : '<button class="btn ghost small danger" type="button" id="btn-delete-experiment">기록 삭제</button>'}
        ${groups.length ? `<a class="btn ghost small" href="/api/experiments/${e.id}/export.csv" download>CSV로 내려받기</a>` : ''}
      </div>
      ${e.status === 'running' ? '' : note('기록을 지워도 실험이 만든 대화는 남아요.')}
    </section>
    ${groups.length ? `
    <section class="panel-block">
      <h3>결과 비교${e.status === 'running' ? ' (진행 중)' : ''}</h3>
      ${compareTable(groups, e.stats.varying)}
      ${note('변형마다 새 대화에서 같은 대본을 처음부터 보낸 결과예요. 변형의 라벨이 실험 라벨이라 행이 변형별로 나뉘어요.')}
    </section>` : ''}`;
}

function experimentsHtml(list, detail) {
  const items = list.map((e) => `
    <li class="${e.id === state.experimentId ? 'active' : ''}">
      <button class="list-item" type="button" data-experiment="${e.id}">
        <span class="grow">
          <span class="title">${escapeHtml(e.name)}</span>
          <span class="sub">${experimentStatus(e.status)} · ${experimentProgress(e)} · ${relativeTime(e.created_at)}</span>
        </span>
      </button>
    </li>`).join('');
  return `
    <section class="panel-block">
      <div class="panel-head">
        <h3>대본 재생 실험</h3>
        <button class="btn small" type="button" id="btn-new-experiment">새 실험</button>
      </div>
      ${note('입력 대본 하나를 여러 설정(변형)으로 새 대화에서 처음부터 자동으로 보내고, 결과를 비교 표로 보여 줘요. 한 번에 하나만 돌아요.')}
      ${list.length ? `<ul class="exp-list">${items}</ul>` : note('아직 실험이 없어요.')}
    </section>
    ${detail ? experimentDetailHtml(detail) : ''}`;
}

async function openExperimentChat(experiment, chatId) {
  if (experiment.character_id && experiment.character_id !== state.characterId) await selectCharacter(experiment.character_id);
  else await refreshChatList();
  await openChat(chatId);
  if (window.matchMedia('(max-width: 1180px)').matches) $('#app').classList.remove('show-inspector');
}

function bindExperimentsTab(box, detail) {
  $('#btn-new-experiment', box).addEventListener('click', openExperimentDialog);
  $$('[data-experiment]', box).forEach((button) => button.addEventListener('click', () => {
    state.experimentId = Number(button.dataset.experiment);
    renderExperimentsTab();
  }));
  if (!detail) return;
  $$('[data-open-chat]', box).forEach((button) => button.addEventListener('click', () =>
    openExperimentChat(detail, Number(button.dataset.openChat)).catch((error) => toast(error.message))));
  $('#btn-cancel-experiment', box)?.addEventListener('click', async (event) => {
    event.target.disabled = true;
    try {
      await api(`/api/experiments/${detail.id}/cancel`, { method: 'POST' });
      toast('실험을 취소했어요. 보내던 턴은 받은 만큼 저장돼요.');
    } catch (error) {
      toast(error.message);
    }
    renderExperimentsTab();
  });
  $('#btn-delete-experiment', box)?.addEventListener('click', async () => {
    if (!window.confirm('이 실험 기록을 지울까요? 실험이 만든 대화는 남아요.')) return;
    try {
      await api(`/api/experiments/${detail.id}`, { method: 'DELETE' });
      state.experimentId = null;
    } catch (error) {
      toast(error.message);
    }
    renderExperimentsTab();
  });
}

async function renderExperimentsTab({ force = true } = {}) {
  clearTimeout(state.experimentPoll);
  const box = $('#tab-experiments');
  const token = ++state.experimentRender; // 늦게 도착한 이전 응답이 화면을 덮지 않게 한다
  let list;
  let detail = null;
  try {
    list = await api('/api/experiments');
    if (state.experimentId) detail = await api(`/api/experiments/${state.experimentId}`).catch(() => null);
  } catch (error) {
    if (token === state.experimentRender) box.innerHTML = note(escapeHtml(error.message));
    return;
  }
  if (token !== state.experimentRender || state.tab !== 'experiments') return;
  if (state.experimentId && !detail) state.experimentId = null;
  const wasRunning = state.experiments.some((e) => e.status === 'running');
  const running = list.some((e) => e.status === 'running');
  state.experiments = list;
  // 진행 중에는 1초마다 다시 불러온다. 바뀐 것이 없으면 다시 그리지 않아 스크롤과 포커스를 지킨다
  const snapshot = JSON.stringify([list, detail]);
  if (force || snapshot !== state.experimentSnapshot) {
    state.experimentSnapshot = snapshot;
    box.innerHTML = experimentsHtml(list, detail);
    bindExperimentsTab(box, detail);
  }
  if (running) state.experimentPoll = setTimeout(() => renderExperimentsTab({ force: false }), 1000);
  else if (wasRunning) {
    refreshChatList(); // 실험이 만든 대화를 왼쪽 목록에 반영한다
    loadCharacters();
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

/* ── 새 실험 ──────────────────────────────── */
const scriptLines = () => $('#form-experiment').elements.inputs.value.split('\n').map((line) => line.trim()).filter(Boolean);

function updateExperimentCount() {
  const inputs = scriptLines().length;
  const variants = $$('#variant-list .variant-entry').length;
  $('#experiment-count').textContent = `입력 ${inputs}개 × 변형 ${variants}개 = 턴 ${inputs * variants}개`;
}

function addVariant(variant = { label: '', settings: {} }) {
  const node = $('#tpl-variant').content.firstElementChild.cloneNode(true);
  node.querySelector('[data-v="label"]').value = variant.label;
  node.querySelector('[data-v="settings"]').value = JSON.stringify(variant.settings);
  node.querySelector('[data-remove]').addEventListener('click', () => {
    node.remove();
    updateExperimentCount();
  });
  $('#variant-list').appendChild(node);
  updateExperimentCount();
  return node;
}

function openExperimentDialog() {
  if (!state.characters.length) {
    toast('먼저 캐릭터를 만들어 주세요.');
    return;
  }
  const form = $('#form-experiment');
  form.reset();
  form.elements.character_id.innerHTML = state.characters.map((c) =>
    `<option value="${c.id}" ${c.id === state.characterId ? 'selected' : ''}>${escapeHtml(c.name)}</option>`).join('');
  form.elements.persona_name.value = store.get('personaName', '지우');
  $('#variant-list').innerHTML = '';
  addVariant({ label: '기본', settings: {} }); // 지금 설정 그대로 보내는 변형
  $('#experiment-error').hidden = true;
  $('#dlg-experiment').showModal();
  form.elements.name.focus();
}

function fillExperimentExample() {
  const form = $('#form-experiment');
  const haram = state.characters.find((c) => c.name === '하람');
  if (haram) form.elements.character_id.value = haram.id;
  form.elements.name.value = '창 전략 비교 (실험 2)';
  form.elements.inputs.value = SAMPLE_SCRIPT.join('\n');
  $('#variant-list').innerHTML = '';
  SAMPLE_VARIANTS.forEach((variant) => addVariant(variant));
}

/* 변형 줄을 읽는다. JSON을 읽지 못한 줄은 그 아래에 이유를 보여 주고 null을 돌려준다 */
function collectVariants() {
  let ok = true;
  const variants = $$('#variant-list .variant-entry').map((node) => {
    const error = node.querySelector('[data-error]');
    error.hidden = true;
    const label = node.querySelector('[data-v="label"]').value.trim();
    const raw = node.querySelector('[data-v="settings"]').value.trim();
    let settings = {};
    try {
      settings = raw ? JSON.parse(raw) : {};
      if (!settings || typeof settings !== 'object' || Array.isArray(settings)) {
        throw new TypeError('설정은 {"이름": 값} 모양의 JSON 객체여야 해요.');
      }
    } catch (err) {
      error.textContent = err instanceof SyntaxError ? `JSON을 읽지 못했어요: ${err.message}` : err.message;
      error.hidden = false;
      ok = false;
    }
    if (!label) {
      error.textContent = '라벨을 입력하세요. 결과 표의 행 이름이 돼요.';
      error.hidden = false;
      ok = false;
    }
    return { label, settings };
  });
  return ok ? variants : null;
}

// 실제 제공사로 보내는 실험을 시작하기 전에 묻는 글. 비용은 입력 예산과 최대 출력을 다 쓴다고 본 상한이다
function estimateText(estimate) {
  const lines = ['실제 API를 부르는 실험이에요.', ''];
  for (const v of estimate.variants.filter((row) => row.provider !== 'mock')) {
    const calls = `대화 ${v.chat_calls}회${v.judge_calls ? ` + 검수 판정 ${v.judge_calls}회` : ''}${v.summary_possible ? ' + 요약(필요할 때)' : ''}`;
    lines.push(`· ${v.label} (${v.provider} · ${v.model}): ${calls}, 비용 상한 ${v.max_usd === null ? '가격표 없음' : usd(v.max_usd)}`);
  }
  const unpriced = estimate.unpriced.length ? ` (가격표 없는 변형은 빠져 있어요: ${estimate.unpriced.join(', ')})` : '';
  lines.push('', `호출 ${estimate.paid_calls}회 이상, 비용 상한 합계 ${usd(estimate.paid_max_usd)}${unpriced}`);
  lines.push('상한은 매 턴 입력 예산과 최대 출력을 모두 쓴다고 본 값이에요. 캐시 할인과 요약 비용은 빠져 있어요.');
  if (estimate.cache_overlap) {
    lines.push('', `⚠ 변형 사이 대기가 ${estimate.cooldown_seconds}초예요. 캐시 수명(${estimate.cache_ttl_seconds}초)보다 짧아서 앞 변형의 캐시가 뒤 변형에 섞이고, 캐시 적중률과 첫 토큰 시간 비교가 틀어질 수 있어요.`);
  }
  lines.push('', '계속할까요?');
  return lines.join('\n');
}

async function submitExperiment(event) {
  event.preventDefault();
  const form = event.target;
  const errorBox = $('#experiment-error');
  errorBox.hidden = true;
  const variants = collectVariants();
  if (!variants) return;
  const inputs = scriptLines();
  if (!inputs.length || !variants.length) {
    errorBox.textContent = '입력 대본과 변형을 하나 이상 넣으세요.';
    errorBox.hidden = false;
    return;
  }
  const seed = form.elements.seed.value.trim();
  const body = {
    name: form.elements.name.value.trim(),
    character_id: Number(form.elements.character_id.value),
    persona_name: form.elements.persona_name.value.trim(),
    inputs,
    variants,
    reset_mock: form.elements.reset_mock.checked,
    seed: seed === '' ? null : Number(seed),
    wait_summary: form.elements.wait_summary.checked,
    cooldown_seconds: Number(form.elements.cooldown_seconds.value || 0),
  };
  try {
    // mock이 아닌 제공사로 보내는 변형이 있으면 호출 수와 비용 상한을 보여 주고 한 번 묻는다
    const current = state.config?.settings.provider;
    if (variants.some((v) => (v.settings.provider || current) !== 'mock')) {
      const estimate = await api('/api/experiments/estimate', { json: body });
      if (!window.confirm(estimateText(estimate))) return;
    }
    const { id } = await api('/api/experiments', { json: body });
    $('#dlg-experiment').close();
    state.experimentId = id;
    state.tab = 'experiments';
    renderInspector();
    if (window.matchMedia('(max-width: 1180px)').matches) $('#app').classList.add('show-inspector');
    toast('실험을 시작했어요. 실험 탭에서 진행 상황을 볼 수 있어요.');
  } catch (error) {
    // 모달이 열려 있는 동안 토스트는 배경 뒤에 가려지므로 대화상자 안에 보여 준다
    errorBox.textContent = error.message;
    errorBox.hidden = false;
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
  if (spec.type === 'text') {
    return `<label class="field"><span>${info.label}</span><input name="${key}" type="text" maxlength="${spec.max_len}" value="${escapeHtml(settings[key] ?? '')}" placeholder="비우면 라벨 없음">${help}</label>`;
  }
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
    Gemini thinking ${escapeHtml(config.gemini.thinking_level || '기본')}, 안전 필터 ${escapeHtml(config.gemini.safety_threshold || '기본')} /
    OpenAI 호환 reasoning_effort ${escapeHtml(config.openai_compat.reasoning_effort || '기본')}, top_p ${escapeHtml(String(config.openai_compat.top_p ?? '기본'))}</p>`;
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
    else if (spec.type === 'text') body[key] = raw.trim(); // 빈 문자열도 보내야 라벨을 지울 수 있다
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
  $('#form-experiment').addEventListener('submit', submitExperiment);
  $('#form-experiment').elements.inputs.addEventListener('input', updateExperimentCount);
  $('#btn-add-variant').addEventListener('click', () => {
    const n = $$('#variant-list .variant-entry').length + 1;
    addVariant({ label: `변형 ${n}`, settings: {} }).querySelector('[data-v="settings"]').focus();
  });
  $('#btn-experiment-example').addEventListener('click', fillExperimentExample);
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
