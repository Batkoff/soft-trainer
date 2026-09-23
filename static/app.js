/* Только поведение редактора. Правила времени и баллов всегда проверяет сервер. */
"use strict";
const csrfToken = () => document.querySelector('[name=csrfmiddlewaretoken]')?.value;
async function requestJSON(url, body) {
  const response = await fetch(url, {method: body === undefined ? "GET" : "POST", credentials: "same-origin",
    headers: body === undefined ? {} : {"Content-Type": "application/json", "X-CSRFToken": csrfToken()},
    body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store"});
  if (response.redirected) throw new Error("Сессия закончилась. Скопируйте ответ и войдите заново.");
  let data;
  try { data = await response.json(); } catch (_) { throw new Error("Нет связи с сервером. Попробуйте ещё раз."); }
  if (!response.ok) throw new Error(data.error || "Не удалось сохранить ответ. Попробуйте ещё раз.");
  return data;
}
for (const select of document.querySelectorAll("[data-auto-submit]")) {
  select.addEventListener("change", () => select.form.requestSubmit());
}
const editor = document.querySelector("#editor");
if (editor && editor.dataset.editable === "true") {
  const id = editor.dataset.attempt, base = `/attempts/${id}/`;
  const textarea = document.querySelector("#answer"), button = document.querySelector("#submit-answer");
  const saveStatus = document.querySelector("#save-status"), errorBox = document.querySelector("#editor-error");
  const timer = document.querySelector("[data-timer]"), counter = document.querySelector("#char-count");
  let revision = Number(editor.dataset.revision), dirty = false, submitting = false, expired = false, conflict = false;
  let deadline = Date.parse(editor.dataset.deadline), clockOffset = Date.parse(editor.dataset.serverNow)-Date.now(), lastSavedText = textarea.value;
  let saveChain = Promise.resolve(), debounce;
  const showError = message => {errorBox.textContent = message; errorBox.hidden = false;};
  const showConflict = () => {
    conflict = true; button.disabled = true;
    saveStatus.textContent = "Конфликт двух вкладок";
    showError("Черновик изменён в другой вкладке. Скопируйте нужный текст, закройте лишнюю вкладку и обновите страницу.");
  };
  const reconcile = data => {
    if (data.status !== "writing") {
      // Сервер мог завершить конкурс досрочно. Итог содержит сохранённый
      // черновик; больше не предлагаем дописывать уже закрытую попытку.
      submitting = true; button.disabled = true; textarea.readOnly = true;
      window.location.assign(data.url); return;
    }
    clockOffset = Date.parse(data.server_time) - Date.now();
    deadline = Date.parse(data.deadline);
    // Обновление статуса не должно разрешать старой вкладке перезаписать новый текст.
    if (data.revision > revision) showConflict();
  };
  async function persist() {
    if (!dirty || expired || submitting || conflict) return;
    const text = textarea.value, currentRevision = ++revision;
    saveStatus.textContent = "Сохраняем…";
    try {
      const data = await requestJSON(base+"draft/", {answer:text, revision:currentRevision});
      reconcile(data);
      if (data.status === "writing" && !data.draft_saved) {
        showConflict();
        return;
      }
      lastSavedText = text;
      dirty = textarea.value !== text;
      saveStatus.textContent = dirty ? "Есть несохранённые изменения" : "Черновик сохранён";
      if (!dirty) errorBox.hidden = true;
    } catch (error) {
      saveStatus.textContent = "Черновик не сохранён";
      showError(error.message + " Текст пока остаётся в открытой вкладке.");
    }
  }
  function queueSave() {saveChain = saveChain.then(persist).catch(() => {});}
  textarea.addEventListener("input", () => {
    dirty = textarea.value !== lastSavedText;
    counter.textContent = textarea.value.length;
    saveStatus.textContent = "Есть несохранённые изменения";
    clearTimeout(debounce); debounce = setTimeout(queueSave, 650);
  });
  // Повтор автосохранения восстанавливает связь, даже если пользователь перестал печатать.
  const autoSave = setInterval(queueSave, 4000);
  // Замечаем досрочное закрытие даже когда сотрудник перестал печатать.
  setInterval(() => {
    if (!document.hidden && !submitting && !expired) {
      requestJSON(base+"status/").then(reconcile).catch(() => {});
    }
  }, 5000);
  window.addEventListener("beforeunload", event => {
    if (dirty && !submitting && !expired) {event.preventDefault(); event.returnValue = "";}
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") requestJSON(base+"status/").then(reconcile).catch(() => {});
  });
  document.querySelector("#answer-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (submitting || conflict) return;
    if (!textarea.value.trim() && !expired) {showError("Напишите ответ клиенту."); textarea.focus(); return;}
    submitting = true; button.disabled = true; textarea.readOnly = true;
    clearTimeout(debounce);
    try {
      // Дожидаемся текущего сохранения, чтобы отправка не обгоняла предыдущий черновик.
      await saveChain;
      if (conflict) throw new Error("Сначала разрешите конфликт черновиков между вкладками.");
      const result = await requestJSON(base+"submit/", {answer:textarea.value});
      dirty = false; clearInterval(autoSave); window.location.assign(result.url);
    } catch (error) {
      submitting = false; button.disabled = conflict; textarea.readOnly = expired;
      showError(error.message); button.textContent = "Повторить отправку";
    }
  });
  async function finishOnTime() {
    if (submitting) return;
    // Сервер сам фиксирует последний черновик. Текст, отправленный после дедлайна, не принимается.
    try { const result = await requestJSON(base+"status/"); reconcile(result); }
    catch (_) { showError("Время вышло. На проверку уйдёт последний черновик, сохранённый на сервере. Восстанавливаем связь…"); }
  }
  function tick() {
    const left = Math.max(0, Math.ceil((deadline-Date.now()-clockOffset)/1000));
    timer.querySelector("strong").textContent = `${Math.floor(left/60).toString().padStart(2,"0")}:${(left%60).toString().padStart(2,"0")}`;
    timer.classList.toggle("urgent", left <= 30);
    if (left === 0 && !expired) {expired = true; textarea.readOnly = true; button.disabled = true; finishOnTime();}
  }
  tick(); setInterval(tick, 500);
  setInterval(() => {if (expired) finishOnTime();}, 4000);
  requestJSON(base+"status/").then(data => {reconcile(data); tick();}).catch(() => {});
}
const pending = document.querySelector("[data-poll-attempt]");
if (pending) {
  const poll = async () => {
    try {
      const state = await requestJSON(`/attempts/${pending.dataset.pollAttempt}/status/`);
      if (["graded", "review"].includes(state.status)) {window.location.reload(); return;}
      pending.querySelector("[data-status-label]").textContent = state.label;
    } catch (_) {pending.querySelector("[data-status-label]").textContent = "Восстанавливаем связь…";}
    setTimeout(poll, document.hidden ? 10000 : 2500);
  };
  setTimeout(poll, 2000);
}

// Keep provider and preset selection compatible; server validation remains authoritative.
const aiSettings = document.querySelector("[data-ai-settings]");
if (aiSettings) {
  const provider = aiSettings.querySelector("[name=provider]");
  const model = aiSettings.querySelector("[name=model_choice]");
  const custom = aiSettings.querySelector("[name=custom_model]");
  function updateModelChoices() {
    for (const option of model.options) {
      option.disabled = option.value !== "custom" &&
        ((provider.value === "openai") !== option.value.startsWith("gpt-"));
    }
    if (model.selectedOptions[0].disabled) {
      model.value = Array.from(model.options).find(option => !option.disabled).value;
    }
    custom.closest("p").hidden = model.value !== "custom";
  }
  provider.addEventListener("change", updateModelChoices);
  model.addEventListener("change", updateModelChoices);
  updateModelChoices();
}


const contestNavigation = document.querySelector("[data-contest-navigation]");
if (contestNavigation && contestNavigation.dataset.archiveMode !== "true") {
  async function refreshContestIfChanged() {
    if (document.hidden) return;
    try {
      const response = await fetch(window.location.pathname, {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) return;
      const html = await response.text();
      const fresh = new DOMParser().parseFromString(html, "text/html").querySelector("[data-contest-navigation]");
      if (!fresh || fresh.dataset.archiveMode === "true") return;
      if ((fresh.dataset.contestId || "") !== (contestNavigation.dataset.contestId || "")) {
        window.location.replace(window.location.pathname);
      }
    } catch (_) {}
  }
  setInterval(refreshContestIfChanged, 5000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshContestIfChanged();
  });
}
