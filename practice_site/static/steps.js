// Shows one step of the listing form at a time. "Next" and "Back" are plain buttons
// (type="button"), so moving between steps never sends anything to the server.
(function () {
  const form = document.getElementById("listing");
  const steps = Array.from(form.querySelectorAll("fieldset.step"));
  const label = document.getElementById("step-number");
  let current = Number(form.dataset.startStep || 1);

  function summary() {
    const dl = document.getElementById("review-summary");
    dl.innerHTML = "";
    const data = new FormData(form);
    for (const [name, text] of [["title", "Title"], ["category", "Category"], ["condition", "Condition"],
      ["price", "Price"], ["currency", "Currency"], ["quantity", "Quantity"], ["description", "Description"]]) {
      const dt = document.createElement("dt"); dt.textContent = text;
      const dd = document.createElement("dd"); dd.textContent = data.get(name) || "(empty)";
      dl.append(dt, dd);
    }
  }

  function show(n) {
    current = Math.max(1, Math.min(steps.length, n));
    steps.forEach((s) => { s.hidden = Number(s.dataset.step) !== current; });
    label.textContent = String(current);
    if (current === steps.length) summary();
  }

  form.addEventListener("click", (event) => {
    if (event.target.matches("button.next")) show(current + 1);
    if (event.target.matches("button.back")) show(current - 1);
  });
  show(current);
})();
