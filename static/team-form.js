/* При смене роли обновляем только поля команды, не теряя введённые данные. */
document.addEventListener("DOMContentLoaded", () => {
  const role = document.getElementById("id_role");
  if (!role) return;
  let pending;

  function initFilteredSelect() {
    const reports = document.getElementById("id_reports");
    if (!reports || !window.SelectFilter) return;
    // После ajax-замены Django сам повторно виджет не инициализирует.
    // Удаляем оставшиеся служебные элементы старого экземпляра и запускаем
    // штатный двухколоночный selector заново.
    for (const id of ["id_reports_from", "id_reports_to"]) {
      const old = document.getElementById(id);
      if (old && old !== reports) old.closest(".selector")?.remove();
    }
    if (reports.classList.contains("selectfilter")) {
      const fieldName = reports.dataset.fieldName || "Подчинённые";
      const stacked = reports.dataset.isStacked === "1";
      window.SelectFilter.init("id_reports", fieldName, stacked);
    }
  }

  role.addEventListener("change", async () => {
    if (pending) pending.abort();
    pending = new AbortController();
    const url = new URL(window.location.href);
    url.searchParams.set("role", role.value);
    const form = role.closest("form");
    const save = form.querySelector('[name="_save"]');
    if (save) save.disabled = true;
    try {
      const response = await fetch(url, {signal: pending.signal, credentials: "same-origin"});
      if (!response.ok) throw new Error("Не удалось загрузить поля команды");
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      for (const name of ["manager", "reports", "unit_name"]) {
        const current = document.querySelector(`.form-row.field-${name}`);
        const replacement = page.querySelector(`.form-row.field-${name}`);
        if (current && replacement) current.replaceWith(replacement);
      }
      initFilteredSelect();
      if (save) save.disabled = false;
    } catch (error) {
      if (error.name !== "AbortError") alert("Не удалось обновить поля. Выберите роль ещё раз.");
    }
  });
});
