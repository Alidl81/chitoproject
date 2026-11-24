(() => {
  const root = document.documentElement;
  try {
    const saved = localStorage.getItem("chitoproject-theme");
    if (saved === "light" || saved === "dark") root.dataset.theme = saved;
  } catch (_) {}

  const updateThemeControls = () => {
    const nextTheme = root.dataset.theme === "light" ? "dark" : "light";
    document.querySelectorAll("[data-theme-toggle]").forEach((toggle) => {
      const label = toggle.querySelector("[data-theme-label]");
      const name = nextTheme === "light" ? "روشن" : "تیره";
      if (label) label.textContent = `پوستهٔ ${name}`;
      toggle.setAttribute("aria-label", `تغییر به پوستهٔ ${name}`);
    });
  };
  updateThemeControls();

  const inviteInput = document.querySelector("[data-invite-token]");
  if (inviteInput && window.location.hash.length > 1) {
    inviteInput.value = decodeURIComponent(window.location.hash.slice(1));
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }

  document.addEventListener("click", async (event) => {
    const toggle = event.target.closest("[data-theme-toggle]");
    if (toggle) {
      root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
      try { localStorage.setItem("chitoproject-theme", root.dataset.theme); } catch (_) {}
      updateThemeControls();
    }
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      const field = copy.parentElement.querySelector("[data-copy-value]");
      if (field) {
        try {
          await navigator.clipboard.writeText(field.value);
          copy.textContent = "کپی شد";
          window.setTimeout(() => { copy.textContent = "کپی لینک"; }, 1800);
        } catch (_) {
          field.select();
          document.execCommand("copy");
        }
      }
    }
  });

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-confirm]");
    if (form && !window.confirm(form.dataset.confirm)) event.preventDefault();
  });
})();
