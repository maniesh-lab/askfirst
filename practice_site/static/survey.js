// Custom ARIA widgets, like a Google Form. Each widget copies its value into a hidden
// input, and the hidden inputs are what the form sends.
(function () {
  const form = document.getElementById("survey");
  const hidden = (name) => form.querySelector(`input[type=hidden][name="${name}"]`);

  // Radio group: click or arrow keys.
  form.querySelectorAll("[role=radiogroup]").forEach((group) => {
    const radios = Array.from(group.querySelectorAll("[role=radio]"));
    const pick = (radio) => {
      radios.forEach((r) => { r.setAttribute("aria-checked", String(r === radio)); r.tabIndex = r === radio ? 0 : -1; });
      hidden(group.dataset.target).value = radio.dataset.value;
      radio.focus();
    };
    radios.forEach((radio, i) => {
      radio.addEventListener("click", () => pick(radio));
      radio.addEventListener("keydown", (e) => {
        if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); pick(radios[(i + 1) % radios.length]); }
        if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); pick(radios[(i - 1 + radios.length) % radios.length]); }
        if (e.key === " " || e.key === "Enter") { e.preventDefault(); pick(radio); }
      });
    });
  });

  // Combobox with a listbox: type to filter, click or Enter to choose.
  form.querySelectorAll("[role=combobox]").forEach((input) => {
    const list = document.getElementById(input.getAttribute("aria-controls"));
    const options = Array.from(list.querySelectorAll("[role=option]"));
    const target = hidden("role");
    const open = (yes) => { list.hidden = !yes; input.setAttribute("aria-expanded", String(yes)); };
    const choose = (opt) => {
      options.forEach((o) => o.setAttribute("aria-selected", String(o === opt)));
      input.value = opt.dataset.value; target.value = opt.dataset.value; open(false);
    };
    input.addEventListener("focus", () => open(true));
    input.addEventListener("input", () => {
      open(true);
      const q = input.value.toLowerCase();
      options.forEach((o) => { o.hidden = !o.dataset.value.toLowerCase().includes(q); });
      target.value = "";
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        const first = options.find((o) => !o.hidden);
        if (first) choose(first);
      }
    });
    options.forEach((o) => o.addEventListener("mousedown", (e) => { e.preventDefault(); choose(o); }));
    input.addEventListener("blur", () => open(false));
  });

  // Checkboxes: each checked one adds a hidden "tools" input.
  const toolsBox = document.getElementById("tools-inputs");
  const syncTools = () => {
    toolsBox.innerHTML = "";
    form.querySelectorAll(".checks [role=checkbox][aria-checked=true]").forEach((c) => {
      const i = document.createElement("input"); i.type = "hidden"; i.name = "tools"; i.value = c.dataset.value; toolsBox.append(i);
    });
  };
  form.querySelectorAll(".checks [role=checkbox]").forEach((box) => {
    const toggle = () => { box.setAttribute("aria-checked", String(box.getAttribute("aria-checked") !== "true")); syncTools(); };
    box.addEventListener("click", toggle);
    box.addEventListener("keydown", (e) => { if (e.key === " ") { e.preventDefault(); toggle(); } });
  });

  // Content-editable text boxes.
  form.querySelectorAll(".editable").forEach((box) => {
    box.addEventListener("input", () => { hidden(box.dataset.target).value = box.innerText.trim(); });
  });
})();
