/* Диалоги используют отдельные эндпоинты; время и переходы проверяет сервер. */
(() => {
  'use strict';
  const root = document.querySelector('#dialogue-app');
  if (!root) return;
  const el = id => document.getElementById('dialogue-' + id);
  const input = el('answer'), form = el('form');
  let state = JSON.parse(el('initial').textContent), currentId = null, revision = 0;
  let dirty = false, blocked = false, busy = false, autoOpening = false, timerExpired = false;
  let lastServerTime = 0;
  let offset = 0, chain = Promise.resolve(), debounce, resultShown = false;
  const shown = new Set();
  const error = text => { el('error').textContent = text; el('error').hidden = !text; };
  async function api(action, data) {
    const response = await fetch(root.dataset.base + action + '/', {
      method: data === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: data === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRFToken': form.querySelector('[name=csrfmiddlewaretoken]').value},
      body: data === undefined ? undefined : JSON.stringify(data)
    });
    if (response.redirected) throw new Error('Сессия закончилась. Скопируйте ответ и войдите заново.');
    let payload;
    try { payload = await response.json(); } catch (_) { throw new Error('Нет связи с сервером. Ответ пока остаётся в этой вкладке.'); }
    if (!response.ok) {
      const failure = new Error(payload.error || 'Запрос не выполнен. Попробуйте ещё раз.');
      failure.conflict = response.status === 409 && /другой вкладке/.test(failure.message);
      throw failure;
    }
    return payload;
  }
  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  }
  function message(key, label, text, employee, timedOut) {
    if (shown.has(key)) return;
    shown.add(key);
    const bubble = node('article', undefined, 'dialogue-message' + (employee ? ' employee' : ''));
    bubble.append(node('strong', label), node('p', text || 'Ответ не заполнен.'));
    if (timedOut) bubble.append(node('small', 'Отправлено по таймеру'));
    el('messages').append(bubble);
    return bubble;
  }
  function feedback(result) {
    if (!result || resultShown) return;
    resultShown = true;
    const section = el('result');
    section.hidden = false;
    const card = node('article', undefined, 'card dialogue-feedback');
    card.append(node('h2', 'Разбор диалога'), node('p', result.score === null ? 'Нужна проверка hard-части' : result.score + ' / 100', 'dialogue-score'), node('p', result.summary));
    for (const [key, title] of [['strengths', 'Что получилось'], ['improvements', 'Что улучшить']]) {
      card.append(node('h3', title, 'spaced'));
      const list = node('ul');
      for (const item of result[key]) list.append(node('li', item));
      card.append(list);
    }
    section.append(card);
    const names = {clarity:'Ясность', tone:'Тон', empathy:'Эмпатия', expectations:'Ожидания', initiative:'Инициатива'};
    for (const row of result.turns) {
      const block = node('article', undefined, 'card dialogue-feedback');
      block.append(node('h3', 'Ответ ' + row.number + ' · ' + (row.score === null ? 'Нужна проверка' : row.score + ' / 100')));
      block.append(node('p', 'Hard: ' + ({passed:'сохранён', violated:'нарушен', uncertain:'неоднозначно'})[row.hard_verdict], 'small muted'));
      block.append(node('p', row.explanation));
      const list = node('ul');
      for (const [key, value] of Object.entries(row.skills)) list.append(node('li', names[key] + ': ' + value + ' / 100'));
      block.append(list);
      if (row.improved_answer) {
        const details = node('details');
        details.append(node('summary', 'Возможный вариант ответа'), node('p', row.improved_answer));
        block.append(details);
      }
      section.append(block);
    }
  }
  function render(next) {
    const serverTime = Date.parse(next.server_time);
    if (serverTime < lastServerTime) return;
    lastServerTime = serverTime;
    state = next;
    offset = Date.parse(state.server_time) - Date.now();
    for (const turn of state.turns) {
      const bubble = message('c'+turn.id, 'Клиент · ход ' + turn.number, turn.client_message, false, false);
      if (bubble) {
        const details = node('details');
        details.append(node('summary', 'Hard: ' + turn.topic_title), node('p', turn.hard_answer));
        bubble.append(details);
      }
      if (turn.submitted) message('a'+turn.id, (state.can_edit ? 'Вы' : 'Сотрудник') + ' · ответ ' + turn.number, turn.answer, true, turn.timed_out);
    }
    if (state.closing_message) message('closing', 'Клиент', state.closing_message, false, false);
    el('empty').hidden = state.turns.length > 0;
    el('progress').textContent = state.turns.length + ' / ' + state.max_turns;
    const labels = {
      ready: state.auto_advance ? 'Следующая реплика готова. Открываем ход…' : 'Время прошлого ответа вышло. Нажмите «Начать следующий ход», когда будете готовы.',
      writing: 'Таймер идёт только для текущего ответа. Черновик сохраняется автоматически.',
      generating: 'Клиент печатает… Таймер остановлен.',
      evaluating: 'Готовим разбор всей беседы… Таймер остановлен.',
      failed: 'Ответ сохранён. Запрос к нейросети не завершён; можно повторить.',
      completed: 'Диалог завершён. Эти результаты не влияют на рейтинг.'
    };
    el('status').textContent = labels[state.status] || state.status_label;
    el('open').hidden = state.status !== 'ready' || !state.can_edit;
    el('retry').hidden = state.status !== 'failed' || !state.can_edit;
    if (state.error) error(state.error);
    const current = state.current;
    el('compose').hidden = !current;
    el('timer').hidden = !current;
    if (current) {
      if (currentId !== current.id) {
        currentId = current.id; revision = current.revision;
        input.value = current.answer; dirty = false; blocked = false; timerExpired = false;
        error('');
      } else if (current.revision > revision) {
        blocked = true;
        error('Черновик изменён в другой вкладке. Скопируйте свой текст и обновите страницу.');
      }
      el('hard').textContent = current.hard_answer;
      el('topic').textContent = current.topic_title;
      input.readOnly = !state.can_edit || blocked;
      el('send').disabled = busy || blocked || !state.can_edit;
      el('fill').disabled = busy || blocked || !state.can_edit;
      el('count').textContent = input.value.length + ' / 6000';
    }
    feedback(state.result);
    tick();
    if (state.status === 'ready' && state.auto_advance && state.can_edit && !document.hidden && !autoOpening) open();
  }
  async function open() {
    if (autoOpening) return;
    autoOpening = true;
    try { error(''); render(await api('open', {})); }
    catch (failure) { error(failure.message); }
    finally { autoOpening = false; }
  }
  function tick() {
    if (!state.current) return;
    const remaining = Math.max(0, Math.ceil((Date.parse(state.current.expires_at) - Date.now() - offset)/1000));
    el('timer').querySelector('strong').textContent = String(Math.floor(remaining/60)).padStart(2,'0') + ':' + String(remaining%60).padStart(2,'0');
    if (!remaining) {
      input.readOnly = true; el('send').disabled = true; el('fill').disabled = true;
      if (!timerExpired) { timerExpired = true; poll(); }
    }
  }
  async function save(submit) {
    if (!state.current || blocked || !state.can_edit || (!dirty && !submit)) return;
    const turnId = currentId, text = input.value, nextRevision = ++revision;
    el('save').textContent = 'Сохраняем…';
    try {
      const next = await api(submit ? 'submit' : 'draft', {turn_id:turnId, answer:text, revision:nextRevision});
      dirty = input.value !== text;
      render(next);
      el('save').textContent = dirty ? 'Есть несохранённые изменения' : 'Черновик сохранён';
    } catch (failure) {
      if (failure.conflict) blocked = true;
      el('save').textContent = 'Черновик не сохранён';
      error(failure.message);
      throw failure;
    }
  }
  function queueSave(submit=false) {
    chain = chain.catch(()=>{}).then(()=>save(submit));
    return chain;
  }
  input.addEventListener('input', () => {
    dirty = true;
    el('count').textContent = input.value.length + ' / 6000';
    el('save').textContent = 'Есть несохранённые изменения';
    clearTimeout(debounce);
    debounce = setTimeout(()=>queueSave().catch(()=>{}), 400);
  });
  el('fill').addEventListener('click', () => {
    if (!state.current || input.readOnly) return;
    input.value = state.current.hard_answer;
    input.dispatchEvent(new Event('input', {bubbles:true}));
    input.focus();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || blocked || !state.current) return;
    busy = true; el('send').disabled = true; clearTimeout(debounce);
    try { await queueSave(true); } catch (_) {} finally { busy = false; if(state.current) el('send').disabled = blocked || timerExpired || !state.can_edit; }
  });
  el('open').addEventListener('click', open);
  el('retry').addEventListener('click', async () => {
    el('retry').disabled = true;
    try { error(''); render(await api('retry', {})); } catch(failure) { error(failure.message); }
    finally { el('retry').disabled = false; }
  });
  let polling = false;
  async function poll() {
    if (polling) return;
    polling = true;
    try { render(await api('status')); if (dirty && !busy && !blocked && state.current && !timerExpired) await queueSave(); } catch(failure) { error(failure.message); }
    finally { polling = false; }
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) poll();
    else if(dirty) queueSave().catch(()=>{});
  });
  window.addEventListener('beforeunload', event => { if(dirty) { event.preventDefault(); event.returnValue=''; } });
  render(state);
  setInterval(tick, 250);
  async function refreshLoop() {
    await new Promise(resolve=>setTimeout(resolve, state.status === 'writing' ? 2500 : 1000));
    if(state.status !== 'completed') await poll();
    if(state.status !== 'completed') refreshLoop();
  }
  refreshLoop();
})();
