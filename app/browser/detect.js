// Runs inside the page and returns every form control, in visual order, as plain data.
// Called by app/browser/detect.py with page.evaluate(). It never changes the page.
() => {
  const clean = (t) => (t || "").replace(/\s+/g, " ").trim();
  const stripStar = (t) => clean(t).replace(/\s*\*\s*$/, "").replace(/^\*\s*/, "");
  const hasStar = (t) => /\*\s*$/.test(clean(t));
  const cssEsc = (v) => (window.CSS && CSS.escape ? CSS.escape(v) : v.replace(/["\\]/g, "\\$&"));

  // Visible = a person could see and use it. Hidden, zero-size, off-screen or aria-hidden is not.
  function visibility(el) {
    const reasons = [];
    if (el.closest("[aria-hidden=true]")) reasons.push("aria-hidden");
    if (el.closest("[hidden]")) reasons.push("hidden-attr");
    const style = getComputedStyle(el);
    if (style.display === "none") reasons.push("display-none");
    if (style.visibility === "hidden" || style.visibility === "collapse") reasons.push("visibility-hidden");
    if (Number(style.opacity) === 0) reasons.push("opacity-0");
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) reasons.push("zero-size");
    const docW = Math.max(document.documentElement.scrollWidth, window.innerWidth);
    if (r.right < 0 || r.bottom < 0 || r.left > docW + 50) reasons.push("off-screen");
    return reasons;
  }

  function textOf(node) {
    if (!node) return "";
    const copy = node.cloneNode(true);
    copy.querySelectorAll("input, select, textarea, script, style, [role=listbox]").forEach((n) => n.remove());
    return clean(copy.textContent);
  }

  function byIds(ids) {
    return clean(ids.split(/\s+/).map((id) => textOf(document.getElementById(id))).join(" "));
  }

  // The nearest text before the element: previous siblings first, then the parent's.
  function precedingText(el) {
    let node = el;
    for (let depth = 0; node && depth < 4; depth++) {
      let sib = node.previousElementSibling;
      while (sib) {
        if (!sib.matches("input, select, textarea, button, script, style") && !sib.querySelector("input, select, textarea")) {
          const t = textOf(sib);
          if (t && t.length <= 120) return t;
        }
        if (sib.querySelector && sib.querySelector("input, select, textarea")) return "";
        sib = sib.previousElementSibling;
      }
      node = node.parentElement;
      if (node && node.matches("form, fieldset, body")) break;
    }
    return "";
  }

  // Label order: label[for], wrapping label, aria-labelledby, aria-label, legend, placeholder, text before.
  function labelFor(el, opts = {}) {
    if (el.id) {
      const lab = document.querySelector(`label[for="${cssEsc(el.id)}"]`);
      if (lab && textOf(lab)) return [textOf(lab), "label-for"];
    }
    if (!opts.skipWrap) {
      const wrap = el.closest("label");
      if (wrap && textOf(wrap)) return [textOf(wrap), "label-wrap"];
    }
    const lb = el.getAttribute("aria-labelledby");
    if (lb && byIds(lb)) return [byIds(lb), "aria-labelledby"];
    const al = el.getAttribute("aria-label");
    if (al && clean(al)) return [clean(al), "aria-label"];
    const fs = el.closest("fieldset");
    if (opts.group && fs) {
      const lg = fs.querySelector(":scope > legend");
      if (lg && textOf(lg)) return [textOf(lg), "legend"];
    }
    const ph = el.getAttribute("placeholder");
    if (ph && clean(ph)) return [clean(ph), "placeholder"];
    const pre = precedingText(el);
    if (pre) return [pre, "preceding-text"];
    return ["", "none"];
  }

  function formIndexOf(el, forms) {
    const f = el.form || el.closest("form");
    return f ? forms.indexOf(f) : -1;
  }

  function cssFor(el, forms) {
    if (el.id) return `#${cssEsc(el.id)}`;
    const tag = el.tagName.toLowerCase();
    const name = el.getAttribute("name");
    const f = el.form || el.closest("form");
    const scope = f && f.id ? `#${cssEsc(f.id)} ` : f ? `form:nth-of-type(${forms.indexOf(f) + 1}) ` : "";
    if (name) return `${scope}${tag}[name="${name.replace(/"/g, '\\"')}"]`;
    return null;
  }

  // A CSS path for elements with no id or name (custom widgets): tag:nth-of-type steps up to
  // the nearest ancestor with an id.
  function cssPath(el) {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.body) {
      if (node.id) { parts.unshift(`#${cssEsc(node.id)}`); break; }
      const tag = node.tagName.toLowerCase();
      const same = Array.from(node.parentElement ? node.parentElement.children : []).filter((c) => c.tagName === node.tagName);
      parts.unshift(same.length > 1 ? `${tag}:nth-of-type(${same.indexOf(node) + 1})` : tag);
      node = node.parentElement;
    }
    return parts.join(" > ");
  }

  function constraints(el) {
    const c = {};
    for (const a of ["maxlength", "minlength", "min", "max", "pattern", "accept", "step"]) {
      if (el.hasAttribute(a)) c[a] = el.getAttribute(a);
    }
    if (el.multiple) c.multiple = true;
    return c;
  }

  const forms = Array.from(document.forms);
  const fields = [];
  const hiddenInputs = [];
  const seenGroups = new Set();
  const all = Array.from(document.querySelectorAll(
    "input, select, textarea, [role=textbox], [role=combobox], [role=radiogroup], [role=checkbox], [role=switch], [role=listbox], [contenteditable=true]"
  ));

  for (const el of all) {
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role");
    // An <input role="combobox"> is a custom widget, not a plain text box.
    const native = ["input", "select", "textarea"].includes(tag) && role !== "combobox";
    const type = tag === "input" ? (el.getAttribute("type") || "text").toLowerCase() : tag;
    if (tag === "input" && ["submit", "button", "reset", "image"].includes(type)) continue;
    if (tag === "input" && type === "hidden") {
      hiddenInputs.push({ name: el.name, value: el.value, form_index: formIndexOf(el, forms) });
      continue;
    }
    // ARIA pieces that belong to a bigger widget are handled with that widget.
    if (!native && role === "listbox" && el.closest(".combo, [role=combobox]")) continue;
    if (!native && role === "listbox" && document.querySelector(`[aria-controls="${el.id}"]`)) continue;
    if (!native && role === "checkbox" && el.closest("[role=group]")) {
      const g = el.closest("[role=group]");
      if (seenGroups.has(g)) continue;
    }
    if (!native && el.isContentEditable && role && role !== "textbox") continue;
    if (!native && !role && !el.isContentEditable) continue;
    if (!native && el.matches("[contenteditable=true]") && el.getAttribute("role") === null && el.closest("[role=textbox]") && el.closest("[role=textbox]") !== el) continue;

    const base = {
      tag, role, name: el.getAttribute("name") || "", id: el.id || "",
      autocomplete: (el.getAttribute("autocomplete") || "").toLowerCase(),
      disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true",
      readonly: !!el.readOnly || el.getAttribute("aria-readonly") === "true",
      form_index: formIndexOf(el, forms),
      constraints: native ? constraints(el) : {},
      options: [], css: native ? cssFor(el, forms) : null,
      tabindex: el.getAttribute("tabindex"),
    };
    if (!native && tag === "input") base.css = cssFor(el, forms);
    let kind, label, source, required, value = "", vis = visibility(el), target = el;

    if (type === "radio") {
      const key = `${base.form_index}|${el.name}`;
      if (seenGroups.has(key)) continue;
      seenGroups.add(key);
      const radios = Array.from(document.querySelectorAll(`input[type=radio][name="${cssEsc(el.name)}"]`))
        .filter((r) => formIndexOf(r, forms) === base.form_index);
      kind = "radio";
      [label, source] = labelFor(el, { group: true, skipWrap: true });
      if (source === "label-for") [label, source] = labelFor(el.closest("fieldset") || el, { group: true, skipWrap: true });
      base.options = radios.map((r) => ({
        value: r.value, text: labelFor(r)[0], selected: r.checked,
        css: r.id ? `#${cssEsc(r.id)}` : `${cssFor(r, forms)}[value="${r.value.replace(/"/g, '\\"')}"]`,
      }));
      required = radios.some((r) => r.required) || hasStar(label);
      value = (radios.find((r) => r.checked) || {}).value || "";
      vis = radios.every((r) => visibility(r).length) ? visibility(el) : [];
      base.disabled = radios.every((r) => r.disabled);
      base.css = null;
    } else if (type === "checkbox") {
      const group = Array.from(document.querySelectorAll(`input[type=checkbox][name="${cssEsc(el.name)}"]`))
        .filter((r) => formIndexOf(r, forms) === base.form_index);
      if (el.name && group.length > 1) {
        const key = `${base.form_index}|${el.name}`;
        if (seenGroups.has(key)) continue;
        seenGroups.add(key);
        kind = "checkbox_group";
        [label, source] = labelFor(el, { group: true, skipWrap: true });
        base.options = group.map((r) => ({ value: r.value, text: labelFor(r)[0], selected: r.checked, css: r.id ? `#${cssEsc(r.id)}` : `${cssFor(r, forms)}[value="${r.value.replace(/"/g, '\\"')}"]` }));
        required = group.some((r) => r.required) || hasStar(label);
        value = group.filter((r) => r.checked).map((r) => r.value);
        vis = group.every((r) => visibility(r).length) ? visibility(el) : [];
        base.css = null;
      } else {
        kind = "checkbox";
        [label, source] = labelFor(el);
        required = el.required || el.getAttribute("aria-required") === "true" || hasStar(label);
        value = el.checked;
      }
    } else if (native) {
      if (tag === "select") {
        kind = el.multiple ? "select_multiple" : "select";
        base.options = Array.from(el.options).map((o) => ({
          value: o.value, text: clean(o.textContent), selected: o.selected,
          group: o.parentElement.tagName === "OPTGROUP" ? o.parentElement.label : null,
        }));
        value = el.multiple ? Array.from(el.selectedOptions).map((o) => o.value) : el.value;
      } else if (tag === "textarea") {
        kind = "textarea"; value = el.value;
      } else {
        kind = type; value = type === "file" ? "" : el.value;
      }
      [label, source] = labelFor(el);
      required = el.required || el.getAttribute("aria-required") === "true" || hasStar(label);
    } else if (role === "radiogroup") {
      kind = "aria_radio";
      [label, source] = labelFor(el);
      const radios = Array.from(el.querySelectorAll("[role=radio]"));
      base.options = radios.map((r) => ({ value: r.dataset.value || clean(r.textContent), text: clean(r.getAttribute("aria-label") || r.textContent), selected: r.getAttribute("aria-checked") === "true" }));
      required = el.getAttribute("aria-required") === "true" || hasStar(label);
      value = (base.options.find((o) => o.selected) || {}).value || "";
    } else if (role === "combobox") {
      kind = "combobox";
      [label, source] = labelFor(el);
      const list = document.getElementById(el.getAttribute("aria-controls") || "");
      base.options = list ? Array.from(list.querySelectorAll("[role=option]")).map((o) => ({ value: o.dataset.value || clean(o.textContent), text: clean(o.textContent), selected: o.getAttribute("aria-selected") === "true" })) : [];
      required = el.getAttribute("aria-required") === "true" || hasStar(label);
      value = el.value || clean(el.textContent);
    } else if (role === "checkbox" || role === "switch") {
      const group = el.closest("[role=group]");
      if (group && group.querySelectorAll("[role=checkbox]").length > 1) {
        seenGroups.add(group);
        kind = "aria_checkbox_group";
        [label, source] = labelFor(group);
        const boxes = Array.from(group.querySelectorAll("[role=checkbox]"));
        base.options = boxes.map((b) => ({ value: b.dataset.value || clean(b.textContent), text: clean(b.getAttribute("aria-label") || b.textContent), selected: b.getAttribute("aria-checked") === "true" }));
        required = group.getAttribute("aria-required") === "true" || hasStar(label);
        value = base.options.filter((o) => o.selected).map((o) => o.value);
        target = group;
        vis = visibility(group);
      } else {
        kind = role === "switch" ? "aria_switch" : "aria_checkbox";
        [label, source] = labelFor(el);
        if (!label) { label = clean(el.textContent); source = "content"; }
        required = el.getAttribute("aria-required") === "true" || hasStar(label);
        value = el.getAttribute("aria-checked") === "true";
      }
    } else if (role === "textbox" || el.isContentEditable) {
      kind = "contenteditable";
      [label, source] = labelFor(el);
      required = el.getAttribute("aria-required") === "true" || hasStar(label);
      value = clean(el.innerText);
      if (el.dataset.maxlength) base.constraints.maxlength = el.dataset.maxlength;
    } else {
      continue;
    }

    // Honeypot: hidden from people but still in the form, with the usual telltale signs.
    const hiddenish = vis.length > 0;
    const sneaky = base.tabindex === "-1" || base.autocomplete === "off" || /honey|hp[_-]|website|url2|leave.*blank|bot/i.test(`${base.name} ${base.id}`);
    const honeypot = hiddenish && (sneaky || vis.includes("off-screen") || vis.includes("aria-hidden")) && !vis.includes("hidden-attr");

    if (!base.css) base.css = target.id ? `#${cssEsc(target.id)}` : cssPath(target);
    const rect = target.getBoundingClientRect();
    fields.push({
      ...base, kind, input_type: type,
      label: stripStar(label), label_source: source, required: !!required,
      value, visible: !hiddenish, hidden_reasons: vis, honeypot,
      top: rect.top + window.scrollY, left: rect.left + window.scrollX,
      // The accessible name as the browser computes it often keeps the "*".
      aria_name: clean(label),
    });
  }

  const buttons = [];
  document.querySelectorAll("button, input[type=submit], input[type=button], input[type=reset], input[type=image], [role=button]").forEach((el) => {
    const tag = el.tagName.toLowerCase();
    const explicit = el.getAttribute("type");
    let htmlType = tag === "button" ? (explicit || "submit").toLowerCase() : tag === "input" ? el.type : "button";
    if (tag === "input" && el.type === "image") htmlType = "submit";
    const inForm = formIndexOf(el, forms);
    if (tag === "button" && inForm === -1 && !explicit) htmlType = "button";
    const text = clean(tag === "input" ? el.value || el.getAttribute("aria-label") : el.getAttribute("aria-label") || el.textContent);
    const r = el.getBoundingClientRect();
    buttons.push({
      text, tag, html_type: htmlType, explicit_type: !!explicit || tag === "input",
      name: el.getAttribute("name") || "", value: el.getAttribute("value") || "",
      id: el.id || "", form_index: inForm, visible: visibility(el).length === 0,
      css: el.id ? `#${cssEsc(el.id)}` : null, top: r.top + window.scrollY, left: r.left + window.scrollX,
    });
  });

  const formInfo = forms.map((f, i) => ({
    index: i, id: f.id || "", action: f.getAttribute("action") || "", method: (f.getAttribute("method") || "get").toLowerCase(),
    is_search: f.getAttribute("role") === "search" || (Array.from(f.elements).filter((e) => e.type && !["submit", "button", "hidden"].includes(e.type)).every((e) => e.type === "search")),
  }));

  const headings = Array.from(document.querySelectorAll("h1, h2, h3")).filter((h) => visibility(h).length === 0).map((h) => clean(h.textContent)).slice(0, 10);
  const ctxNode = document.querySelector("main") || document.body;
  const context = clean(Array.from(ctxNode.querySelectorAll("h1, h2, h3, p, li")).filter((n) => !n.closest("form") && visibility(n).length === 0).map((n) => n.textContent).join(" ")).slice(0, 1500);

  return { url: location.href, title: document.title, headings, context, forms: formInfo, fields, buttons, hidden_inputs: hiddenInputs };
}
