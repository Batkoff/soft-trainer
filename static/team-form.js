/* При смене роли обновляем только поля команды, не теряя введённые данные. */
document.addEventListener("DOMContentLoaded", () => {
  const role = document.getElementById("id_role");
  if (!role) return;
  let pending;
  role.addEventListener("change", async () => {
    if (pending) pending.abort();
    pending = new AbortController();
    const url = new URL(window.location.href);
    url.searchParams.set("role", role.value);
    const form = role.closest("form");
    const save = form.querySelector('[name="_save"]');
    if (save) save.disabled = true;
    try {
      const response = await fetch(url, {signal: pending.signal});
      if (!response.ok) throw new Error("Не удалось загрузить поля команды");
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      for (const name of ["manager", "reports", "unit_name"]) {
        const current = document.querySelector(`.form-row.field-${name}`);
        const replacement = page.querySelector(`.form-row.field-${name}`);
        if (current && replacement) current.replaceWith(replacement);
      }
      if (save) save.disabled = false;
    } catch (error) {
      if (error.name !== "AbortError") alert("Не удалось обновить поля. Выберите роль ещё раз.");
    }
  });
});
