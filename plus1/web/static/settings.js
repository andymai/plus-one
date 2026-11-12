(() => {
  const wrap = document.querySelector(".st-wrap");
  if (!wrap) return;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  const DAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const lines = (v) => (v || "").split("\n").map((s) => s.trim()).filter(Boolean);
  const icon = (name, cls = "") => `<svg class="i${cls ? " " + cls : ""}" aria-hidden="true" focusable="false"><use href="#i-${name}"/></svg>`;
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const fire = (el) => el.dispatchEvent(new Event("input", { bubbles: true }));

  const toast = (text) => {
    const el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "status");
    el.innerHTML = '<span class="st st-ok" aria-hidden="true"><svg focusable="false"><use href="#st-done"/></svg></span><span></span>';
    el.lastChild.textContent = text;
    $("#toasts")?.append(el);
    setTimeout(() => { el.classList.add("is-leaving"); setTimeout(() => el.remove(), 220); }, 3000);
  };

  // What a form holds now against what's saved. Saved values ride on each field, so a page the server sent back with
  // what you typed still knows what isn't saved yet.
  const forms = $$("form[data-form]");
  const fields = (form) => Array.from(form.elements).filter((el) => el.name && "saved" in el.dataset);
  // A radio group reads as the value of its checked button, through whichever button has it.
  const value = (el) => (el.type === "checkbox" ? (el.checked ? "on" : "") : el.type === "radio" ? el.form.elements[el.name].value : el.value);
  const same = (el, a, b) => (el.type === "number" && a !== "" && b !== "" ? Number(a) === Number(b) : el.tagName === "TEXTAREA" ? lines(a).join("\n") === lines(b).join("\n") : a === b);
  const changed = (form) => fields(form).filter((el) => !same(el, value(el), el.dataset.saved) && !(el.type === "radio" && !el.checked));
  const setValue = (el, v) => {
    if (el.type === "checkbox") el.checked = v === "on";
    else if (el.type === "radio") el.checked = el.value === v;
    else el.value = v;
  };

  // Checks that mirror the server's, so most mistakes show before a round trip. The server still checks everything.
  let zones = [];
  try { zones = Intl.supportedValuesOf("timeZone"); } catch (_) { zones = []; }
  const zoneSet = new Set([...zones, "UTC"]);
  const problems = (form) => {
    const out = [];
    for (const el of fields(form)) {
      const v = value(el), label = el.dataset.label || "This";
      if (el.type === "number") {
        const n = Number(v), lo = Number(el.min), hi = Number(el.max);
        if (v.trim() === "" || !isFinite(n) || n < lo || n > hi) {
          out.push([el, el.step === "0.01" ? `${label} is an amount in dollars, like 40.` : `${label} is a whole number from ${lo} to ${hi}.`]);
        } else if (el.step !== "0.01" && !Number.isInteger(n)) out.push([el, `${label} is a whole number.`]);
      }
      if (el.name === "end" && Number(v) <= Number(form.elements.start_hour.value)) out.push([el, "The end has to come after the start."]);
      if (el.name === "timezone" && zones.length && !zoneSet.has(v.trim())) out.push([el, `${v.trim() || "That"} isn't a time zone. Pick one from the list, like America/New_York.`]);
      if (el.name === "channels") {
        for (const line of lines(v)) {
          const [name, id = ""] = line.split("=");
          if (!name.trim()) { out.push([el, `Give channel ${id.trim()} a name.`]); break; }
          if (!/^[A-Z0-9]{8,12}$/.test(id.trim())) { out.push([el, `#${name.trim()} needs an ID like C0123456789.`]); break; }
        }
      }
    }
    if ($$("input[name^=day_]", form).length && !$$("input[name^=day_]:checked", form).length) out.push([$("input[name^=day_]", form), "Pick at least one day."]);
    return out;
  };
  const showProblems = (form, found, all = false) => {
    $$(".st-error[data-client]", form).forEach((p) => p.remove());
    $$(".st-row.has-error", form).forEach((r) => { if (!$(".st-error:not([data-client])", r)) r.classList.remove("has-error"); });
    for (const [el, text] of found) {
      // Wait for a field you're typing in to lose focus before complaining about it.
      if (!all && el === document.activeElement && el.type === "text") continue;
      const row = el.closest(".st-row");
      if (!row || $(".st-error", row)) continue;
      row.classList.add("has-error");
      const p = document.createElement("p");
      p.className = "st-error";
      p.dataset.client = "1";
      p.innerHTML = icon("alert") + "<span></span>";
      p.lastChild.textContent = text;
      $(".st-text", row).append(p);
    }
  };

  const update = (form) => {
    const section = form.closest(".st-section");
    const diff = changed(form);
    const found = problems(form);
    const dirty = diff.length > 0 || section.classList.contains("has-problem");
    section.classList.toggle("is-dirty", dirty);
    showProblems(form, found);
    const bar = $(".st-savebar", form);
    bar.classList.toggle("has-errors", found.length > 0);
    $(".msg", bar).textContent = found.length
      ? (found.length === 1 ? "Fix 1 thing to save" : `Fix ${found.length} things to save`)
      : diff.length === 1 ? "1 unsaved change" : diff.length ? `${diff.length} unsaved changes` : "Not saved yet";
    $("[data-save]", bar).disabled = found.length > 0;
    $$(`[data-nav="${section.id}"]`).forEach((a) => a.classList.toggle("is-dirty", dirty));
    markDefaults(form);
    form.dispatchEvent(new CustomEvent("settings:changed"));
  };

  const discard = (form) => {
    fields(form).forEach((el) => setValue(el, el.dataset.saved));
    $$(".st-confirm, .st-notice-err, .st-error", form).forEach((el) => el.remove());
    $$(".st-row.has-error", form).forEach((r) => r.classList.remove("has-error"));
    form.closest(".st-section").classList.remove("has-problem");
    form.dispatchEvent(new CustomEvent("settings:sync"));
    update(form);
  };

  forms.forEach((form) => {
    form.addEventListener("input", (event) => { if (!event.target.closest(".st-confirm")) update(form); });
    form.addEventListener("change", (event) => { if (!event.target.closest(".st-confirm")) update(form); });
    form.addEventListener("focusout", () => setTimeout(() => update(form), 0));
    form.addEventListener("settings:sync", () => update(form));
    form.addEventListener("submit", (event) => {
      $$("[data-chips] > input", form).forEach((input) => input.dispatchEvent(new Event("settings:commit")));
      const found = problems(form);
      if (found.length) {
        event.preventDefault();
        showProblems(form, found, true);
        found[0][0].focus();
        return;
      }
      submitting = true;
    });
    $("[data-discard]", form)?.addEventListener("click", () => discard(form));
  });
  $$(".st-confirm input[data-word]").forEach((input) => {
    // Retyped values must match exactly; the words to type don't care about case.
    const ok = () => ("exact" in input.dataset ? input.value.trim() === input.dataset.word : input.value.trim().toLowerCase() === input.dataset.word.toLowerCase());
    const check = () => input.closest(".confirm-field").classList.toggle("is-ok", ok());
    input.addEventListener("input", check);
    check();
    input.focus({ preventScroll: true });
  });

  let submitting = false;
  addEventListener("beforeunload", (event) => {
    if (submitting || !forms.some((f) => changed(f).length)) return;
    event.preventDefault();
    event.returnValue = "";
  });

  // A quiet note on any value that isn't the default, with a way back to it.
  function markDefaults(form) {
    $$("[data-default]", form).forEach((el) => {
      // A table of fields shares one label, so its cells carry no note of their own.
      if (el.closest("table")) return;
      const label = $(".st-label", el.closest(".st-row") || form);
      if (!label) return;
      let mark = $(`.st-changed[data-for="${el.name}"]`, label);
      if (same(el, value(el), el.dataset.default)) { mark?.remove(); return; }
      if (!mark) {
        mark = document.createElement("span");
        mark.className = "st-changed";
        mark.dataset.for = el.name;
        label.append(mark);
      }
      const shown = el.type === "checkbox" ? (el.dataset.default ? "On" : "Off")
        : el.tagName === "SELECT" ? el.querySelector(`option[value="${CSS.escape(el.dataset.default)}"]`)?.textContent
        : el.step === "0.01" ? `$${el.dataset.default}` : el.dataset.default + ($(".affix:not(.pre)", el.parentElement) ? ` ${$(".affix:not(.pre)", el.parentElement).textContent}` : "");
      mark.innerHTML = `Default ${esc(shown || "")}<button type="button" data-reset="${esc(el.name)}">Reset</button>`;
    });
  }
  wrap.addEventListener("click", (event) => {
    const reset = event.target.closest("[data-reset]");
    if (!reset) return;
    const form = reset.closest("form");
    const el = form.elements[reset.dataset.reset];
    setValue(el, el.dataset.default);
    form.dispatchEvent(new CustomEvent("settings:sync"));
  });

  // Steppers
  wrap.addEventListener("click", (event) => {
    const step = event.target.closest(".step");
    if (!step) return;
    const input = $("input", step.parentElement);
    if (input.disabled) return;
    const by = Number(step.dataset.stepby || input.step || 1) || 1;
    let v = Number(input.value || 0) + Number(step.dataset.step) * by;
    v = Math.min(Number(input.max), Math.max(Number(input.min), v));
    input.value = Number.isInteger(v) ? String(v) : v.toFixed(2);
    fire(input);
  });

  // Time zone picker
  $$("[data-combo]").forEach((box) => {
    const input = $("input", box);
    if (!zones.length) return;
    let list = null, active = -1, items = [];
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    const offset = (zone) => {
      try {
        return new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "shortOffset" }).formatToParts(new Date()).find((p) => p.type === "timeZoneName").value;
      } catch (_) { return ""; }
    };
    const close = () => { list?.remove(); list = null; active = -1; input.setAttribute("aria-expanded", "false"); };
    const open = () => {
      const q = input.value.trim().toLowerCase().replace(/\s+/g, "_");
      items = (zoneSet.has(input.value.trim()) ? zones : zones.filter((z) => z.toLowerCase().includes(q))).slice(0, 80);
      if (!list) {
        list = document.createElement("ul");
        list.className = "combo-list";
        list.id = "zone-list";
        list.setAttribute("role", "listbox");
        box.append(list);
        input.setAttribute("aria-controls", "zone-list");
      }
      input.setAttribute("aria-expanded", "true");
      list.innerHTML = items.length
        ? items.map((z, i) => `<li role="option" data-z="${esc(z)}" aria-selected="${i === active || (active < 0 && z === input.value.trim())}"><span>${esc(z.replace(/_/g, " "))}</span><span class="off">${esc(offset(z))}</span></li>`).join("")
        : '<li class="none">No zone matches</li>';
      $("[aria-selected=true]", list)?.scrollIntoView({ block: "nearest" });
    };
    const pick = (zone) => { input.value = zone; close(); fire(input); };
    input.addEventListener("focus", () => { input.select(); open(); });
    input.addEventListener("input", (event) => { if (event.isTrusted) { active = -1; open(); } });
    input.addEventListener("keydown", (event) => {
      if (!list) { if (event.key === "ArrowDown") { event.preventDefault(); open(); } return; }
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        active = Math.max(0, Math.min(items.length - 1, active + (event.key === "ArrowDown" ? 1 : -1)));
        $$("li", list).forEach((li, i) => li.setAttribute("aria-selected", String(i === active)));
        $$("li", list)[active]?.scrollIntoView({ block: "nearest" });
      } else if (event.key === "Enter" && active >= 0) { event.preventDefault(); pick(items[active]); }
      else if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
    });
    box.addEventListener("mousedown", (event) => {
      const li = event.target.closest("li[data-z]");
      if (li) { event.preventDefault(); pick(li.dataset.z); }
    });
    input.addEventListener("blur", () => setTimeout(close, 120));
  });

  // Chip lists write one entry per line into their textarea, which is what the form posts.
  const chipHtml = (v, mono) => `<span class="chip-x${mono ? " mono" : ""}" title="${esc(v)}"><span>${esc(v)}</span><button type="button" aria-label="Remove ${esc(v)}">${icon("x")}</button></span>`;
  $$("[data-chips]").forEach((box) => {
    const area = $("textarea", box), input = $(":scope > input", box), mono = box.dataset.mono === "1", regex = box.dataset.kind === "regex";
    const render = () => { $$(".chip-x", box).forEach((c) => c.remove()); lines(area.value).forEach((v) => input.insertAdjacentHTML("beforebegin", chipHtml(v, mono))); };
    const set = (vals) => { area.value = vals.join("\n"); render(); fire(area); };
    const say = (text) => {
      box.parentElement.querySelector(".st-chip-error")?.remove();
      if (!text) return;
      const p = document.createElement("p");
      p.className = "st-error st-chip-error";
      p.innerHTML = icon("alert") + "<span></span>";
      p.lastChild.textContent = text;
      box.after(p);
    };
    const add = (raw) => {
      const vals = lines(area.value);
      let problem = "";
      for (let v of raw.split(regex ? /\n/ : /[\n,]/).map((s) => s.trim()).filter(Boolean)) {
        if (area.name === "owners") v = v.replace(/^@/, "").split("/").pop();
        if (regex) {
          try { new RegExp(v.replace(/^\(\?[a-z]+\)/, "")); } catch (_) { problem = `${v} isn't a valid pattern, so it wasn't added.`; continue; }
        }
        if (!vals.includes(v)) vals.push(v);
      }
      input.value = "";
      say(problem);
      if (vals.length !== lines(area.value).length) set(vals);
    };
    input.addEventListener("keydown", (event) => {
      if ((event.key === "Enter" || (event.key === "," && !regex)) && input.value.trim()) { event.preventDefault(); add(input.value); }
      else if (event.key === "Enter") event.preventDefault();
      else if (event.key === "Backspace" && !input.value) { const vals = lines(area.value); if (vals.length) { vals.pop(); set(vals); } }
    });
    input.addEventListener("paste", (event) => {
      const text = event.clipboardData?.getData("text") || "";
      if (text.includes("\n")) { event.preventDefault(); add(text); }
    });
    input.addEventListener("blur", () => { if (input.value.trim()) add(input.value); });
    input.addEventListener("settings:commit", () => { if (input.value.trim()) add(input.value); });
    box.addEventListener("click", (event) => {
      const remove = event.target.closest(".chip-x button");
      if (remove) { const v = remove.closest(".chip-x").title; set(lines(area.value).filter((x) => x !== v)); input.focus(); }
      else if (event.target === box) input.focus();
    });
    box.addEventListener("settings:add", (event) => add(event.detail));
    area.form.addEventListener("settings:sync", render);
  });

  // Team suggestions arrive through htmx after this runs, so listen from the document.
  document.addEventListener("click", (event) => {
    const pick = event.target.closest("[data-chip-add]");
    if (!pick) return;
    $(`textarea[name="${pick.dataset.chipList}"]`)?.closest("[data-chips]").dispatchEvent(new CustomEvent("settings:add", { detail: pick.dataset.chipAdd }));
    pick.closest("li").remove();
  });

  // Slack channel rows write name=ID lines into their textarea.
  const slack = $("[data-slack]");
  if (slack) {
    const area = $("textarea", slack), list = $("[data-slack-list]", slack), addInput = $("[data-slack-add]", slack);
    let counts = {};
    try { counts = JSON.parse($("[data-channel-counts]")?.textContent || "{}"); } catch (_) { counts = {}; }
    const rowHtml = (name, id) => {
      const known = name in counts;
      return `<div class="slack-row"><span class="name-wrap">#<input type="text" value="${esc(name)}" aria-label="Channel name" placeholder="name" spellcheck="false"></span>`
        + `<span class="id-wrap"><input type="text" class="mono-input" value="${esc(id)}" aria-label="Channel ID" spellcheck="false"></span>`
        + `<span class="slack-count${counts[name] ? "" : " is-zero"}">${known ? `${counts[name]} PRs` : name ? "0 PRs" : "New"}</span>`
        + `<button type="button" class="ghost icon-only sm x" aria-label="Remove ${esc(name || id)}">${icon("x")}</button></div>`;
    };
    const sync = () => {
      area.value = $$(".slack-row", list).map((row) => { const [name, id] = $$("input", row).map((i) => i.value.trim()); return `${name.replace(/^#/, "")}=${id}`; }).join("\n");
      fire(area);
    };
    const render = () => { list.innerHTML = lines(area.value).map((line) => { const [name, id = ""] = line.split("="); return rowHtml(name, id); }).join(""); };
    list.addEventListener("input", (event) => { event.stopPropagation(); sync(); });
    list.addEventListener("click", (event) => {
      const remove = event.target.closest(".x");
      if (remove) { remove.closest(".slack-row").remove(); sync(); }
    });
    const addFrom = (raw) => {
      const found = raw.match(/archives\/([A-Z0-9]{8,12})/) || raw.trim().match(/^([A-Z0-9]{8,12})$/);
      if (!found) {
        addInput.setCustomValidity("That isn't a Slack channel link or ID.");
        addInput.reportValidity();
        return;
      }
      addInput.setCustomValidity("");
      list.insertAdjacentHTML("beforeend", rowHtml("", found[1]));
      addInput.value = "";
      sync();
      $$(".slack-row", list).at(-1).querySelector("input").focus();
    };
    addInput.addEventListener("paste", (event) => {
      const text = event.clipboardData?.getData("text");
      if (text) { event.preventDefault(); addFrom(text); }
    });
    addInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); if (addInput.value.trim()) addFrom(addInput.value); }
    });
    addInput.addEventListener("input", (event) => { event.stopPropagation(); addInput.setCustomValidity(""); });
    $("[data-slack-add-btn]", slack).addEventListener("click", () => (addInput.value.trim() ? addFrom(addInput.value) : addInput.focus()));
    area.form.addEventListener("settings:sync", render);
  }

  // Check commands write one command per line into their textarea.
  $$("[data-cmds]").forEach((box) => {
    const area = $("textarea", box), list = $("[data-cmd-list]", box);
    const rowHtml = (v) => `<li><input type="text" class="mono-input grow" value="${esc(v)}" aria-label="Check command" spellcheck="false"><button type="button" class="ghost icon-only sm" aria-label="Remove command">${icon("x")}</button></li>`;
    const empty = '<li class="empty-row">No checks. Reviews start right away.</li>';
    const sync = () => { area.value = $$("input", list).map((i) => i.value.trim()).filter(Boolean).join("\n"); fire(area); };
    const render = () => { const v = lines(area.value); list.innerHTML = v.length ? v.map(rowHtml).join("") : empty; };
    list.addEventListener("input", (event) => { event.stopPropagation(); sync(); });
    list.addEventListener("click", (event) => {
      const remove = event.target.closest("button");
      if (!remove) return;
      remove.closest("li").remove();
      sync();
      if (!$("input", list)) list.innerHTML = empty;
    });
    $("[data-cmd-add]", box).addEventListener("click", () => {
      $(".empty-row", list)?.remove();
      list.insertAdjacentHTML("beforeend", rowHtml(""));
      $$("input", list).at(-1).focus();
    });
    area.form.addEventListener("settings:sync", render);
  });

  // Rows that only matter for one choice show only for it. Hidden fields still post, so switching back loses nothing.
  const showFor = (form, name, rows) => {
    if (!form) return;
    const apply = () => {
      const picked = form.elements[name]?.value;
      for (const [selector, when] of rows) {
        const row = form.querySelector(selector)?.closest(".st-row");
        if (row) row.hidden = picked !== when;
      }
    };
    form.addEventListener("settings:changed", apply);
    apply();
  };
  showFor($("form[data-form=models]"), "draft_mode", [["[name=draft_command]", "script"]]);
  showFor($("form[data-form=web]"), "mode", [["[name=allowed_login]", "tailscale"], ["[name=socket]", "tailscale"], ["[name=port]", "local"]]);

  // Approving on its own: say what a save would do before it happens.
  $$("[data-trust]").forEach((card) => {
    const form = card.closest("form");
    const box = $("input[name=auto_approve]", card), note = $("[data-trust-pending]", card);
    form.addEventListener("settings:changed", () => {
      const on = box.checked, was = card.dataset.was === "on";
      note.hidden = on === was;
      note.textContent = on ? "Turns on when you save." : "Turns off when you save.";
    });
  });

  // Schedule: draw a day of checks from what the form says now.
  const schedForm = $("form[data-form=schedule]");
  const zoneNow = (zone) => {
    try {
      const parts = new Intl.DateTimeFormat("en-US", { timeZone: zone, weekday: "short", hour: "numeric", minute: "numeric", hourCycle: "h23" }).formatToParts(new Date());
      const get = (t) => parts.find((p) => p.type === t)?.value;
      return { day: DAY.indexOf(get("weekday")), minute: (Number(get("hour")) % 24) * 60 + Number(get("minute")) };
    } catch (_) { return null; }
  };
  const clock = (minute) => { const h = Math.floor(minute / 60) % 24; return `${h % 12 === 0 ? 12 : h % 12}:${String(minute % 60).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`; };
  const renderSchedule = () => {
    const svg = $("[data-sched] svg");
    if (!svg || !schedForm) return;
    const f = schedForm.elements;
    const days = DAY.map((d, i) => (f[`day_${d}`].checked ? i : -1)).filter((i) => i >= 0);
    const start = Number(f.start_hour.value), end = Number(f.end.value);
    const zone = zoneSet.has(f.timezone.value.trim()) ? f.timezone.value.trim() : "UTC";
    const now = zoneNow(zone);
    let next = null;
    if (now && days.length && end > start) {
      for (let t = Math.floor(now.minute / 30) * 30 + 30, i = 0; i < 48 * 8; i++, t += 30) {
        const off = Math.floor(t / 1440), m = t % 1440;
        if (days.includes((now.day + off) % 7) && m >= start * 60 && m < end * 60) {
          next = { off, minute: m, label: off === 0 ? clock(m) : off === 1 ? `tomorrow ${clock(m)}` : `${DAY[(now.day + off) % 7]} ${clock(m)}` };
          break;
        }
      }
    }
    const W = Math.max(280, svg.parentElement.clientWidth || 640), H = 66, L = 10, R = 10;
    const x = (hour) => L + ((W - L - R) * hour) / 24;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("width", W);
    svg.setAttribute("height", H);
    let out = "";
    for (let k = 0; k <= 24; k += 3) {
      const label = k === 0 || k === 24 ? "12a" : k === 12 ? "12p" : `${k % 12}${k < 12 ? "a" : "p"}`;
      out += `<line class="sched-hour" x1="${x(k)}" x2="${x(k)}" y1="22" y2="46"/><text class="sched-label" x="${x(k)}" y="62" text-anchor="${k === 0 ? "start" : k === 24 ? "end" : "middle"}">${label}</text>`;
    }
    if (end > start) {
      out += `<rect class="sched-band" x="${x(start)}" y="22" width="${x(end) - x(start)}" height="24" rx="4"/>`;
      const today = now && days.includes(now.day);
      for (let m = start * 60; m < end * 60; m += 30) {
        const cls = next && next.off === 0 && m === next.minute ? "is-next" : today && m < now.minute ? "is-past" : "";
        out += `<line class="sched-tick ${cls}" x1="${x(m / 60)}" x2="${x(m / 60)}" y1="27" y2="41"/>`;
      }
    }
    out += `<line class="sched-axis" x1="${L}" x2="${W - R}" y1="46" y2="46"/>`;
    if (now) out += `<line class="sched-now" x1="${x(now.minute / 60)}" x2="${x(now.minute / 60)}" y1="16" y2="50"/>`;
    if (next && next.off === 0) {
      const at = x(next.minute / 60);
      out += `<text class="sched-next" x="${at}" y="12" text-anchor="${at > W - 70 ? "end" : at < 70 ? "start" : "middle"}">Next ${next.label}</text>`;
    }
    svg.innerHTML = out;
    const perDay = Math.max(0, (end - start) * 2);
    $("[data-sched-cap]").innerHTML = days.length && perDay
      ? `<span><strong>${perDay}</strong> checks a day</span><span><strong>${perDay * days.length}</strong> a week</span><span>Last one starts at ${clock(end * 60 - 30)}</span>${next ? `<span>Next ${next.off === 0 ? "at " : ""}${next.label}</span>` : ""}<span>Dashed line is now</span>`
      : "<span>No checks would run.</span>";
  };
  schedForm?.addEventListener("settings:changed", renderSchedule);

  // Budget: the last two weeks against the cap you're typing.
  const budgetForm = $("form[data-form=budget]");
  const spend = $("[data-spend]");
  let spendDays = [];
  try { spendDays = JSON.parse($("[data-spend-days]")?.textContent || "[]"); } catch (_) { spendDays = []; }
  const money = (n) => `$${Number.isInteger(n) ? n.toLocaleString() : n.toFixed(2)}`;
  const shortDate = (day) => new Date(`${day}T12:00:00`).toLocaleDateString([], { month: "short", day: "numeric" });
  const niceStep = (v) => { const p = 10 ** Math.floor(Math.log10(v || 1)); return [1, 2, 5, 10].map((m) => m * p).find((s) => s >= v) || p * 10; };
  const renderSpend = () => {
    if (!spend || !budgetForm || !spendDays.length) return;
    const cap = Number(budgetForm.elements.max_spend_per_day_usd.value) || 0;
    const today = Number(spend.dataset.today) || 0;
    const svg = $("svg.chart", spend);
    const W = Math.max(280, svg.parentElement.clientWidth || 640), H = 140, L = 36, R = 6, T = 16, B = 22;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("width", W);
    svg.setAttribute("height", H);
    const peak = spendDays.reduce((a, d) => (d.cost > a.cost ? d : a), spendDays[0]);
    const top = Math.max(cap, peak.cost) * 1.12 || 1;
    const y = (v) => T + (H - T - B) * (1 - v / top);
    const bw = (W - L - R) / spendDays.length;
    let out = "";
    const step = niceStep(top / 3);
    for (let v = 0; v <= top; v += step) out += `<line class="spend-grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="chart-label" x="${L - 6}" y="${y(v) + 3.5}" text-anchor="end">$${v}</text>`;
    const last = spendDays.length - 1;
    spendDays.forEach((d, i) => {
      const bx = L + i * bw + bw * 0.2, w = bw * 0.6, by = y(d.cost);
      const cls = cap && d.cost > cap ? "is-over" : i === last ? "is-today" : "";
      if (d.cost > 0) out += `<rect class="spend-bar ${cls}" x="${bx}" y="${by}" width="${w}" height="${Math.max(1, y(0) - by)}" rx="2"><title>${shortDate(d.day)}: ${money(d.cost)}</title></rect>`;
      if (i === 0 || i === Math.floor(last / 2) || i === last) out += `<text class="chart-label" x="${bx + w / 2}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === last ? "end" : "middle"}">${i === last ? "Today" : shortDate(d.day)}</text>`;
    });
    if (cap > 0) out += `<line class="spend-cap" x1="${L}" x2="${W - R}" y1="${y(cap)}" y2="${y(cap)}"/><text class="spend-cap-label" x="${W - R}" y="${y(cap) - 5}" text-anchor="end">Cap ${money(cap)}</text>`;
    svg.innerHTML = out;
    const over = spendDays.filter((d) => cap && d.cost > cap);
    const note = $("[data-spend-note]", spend);
    note.classList.toggle("is-warn", over.length > 0);
    note.textContent = over.length
      ? `At ${money(cap)}, checks would have stopped starting reviews early on ${over.length} of the last ${spendDays.length} days (${over.map((d) => shortDate(d.day)).join(", ")}).`
      : peak.cost > 0 ? `Peak ${money(Math.round(peak.cost * 100) / 100)} on ${shortDate(peak.day)}. At ${money(cap)}, checks never stopped early in the last ${spendDays.length} days.` : "Nothing spent in the last two weeks.";
    $("[data-cap-text]", spend).textContent = money(cap);
    $("[data-today-meter]", spend).setAttribute("width", (120 * Math.min(1, cap ? today / cap : 1)).toFixed(1));
  };
  budgetForm?.addEventListener("settings:changed", renderSpend);
  new ResizeObserver(() => { renderSchedule(); renderSpend(); }).observe($(".st-main"));

  // Copy a path
  wrap.addEventListener("click", (event) => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    const source = $(button.dataset.copy);
    const select = () => {
      const range = document.createRange();
      range.selectNodeContents(source);
      getSelection().removeAllRanges();
      getSelection().addRange(range);
    };
    try { navigator.clipboard.writeText(source.textContent).then(() => toast("Copied."), select); } catch (_) { select(); }
  });

  // Sections: the rail and index jump, and the crumb and rail follow where you are.
  const sections = $$(".st-section");
  const content = document.getElementById("content");
  const scroller = () => (getComputedStyle(content).overflowY === "auto" ? content : null);
  wrap.addEventListener("click", (event) => {
    const link = event.target.closest("a[href^='#']");
    const target = link && document.getElementById(link.getAttribute("href").slice(1));
    if (!target) return;
    event.preventDefault();
    target.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    history.replaceState(null, "", `#${target.id}`);
  });
  $("[data-to-index]")?.addEventListener("click", () => $(".st-index").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" }));
  let observer = null, root;
  const spy = () => {
    observer?.disconnect();
    root = scroller();
    const visible = new Set();
    const top = 80 + ($(".st-crumb")?.offsetHeight || 0);
    observer = new IntersectionObserver((entries) => {
      entries.forEach((e) => (e.isIntersecting ? visible.add(e.target) : visible.delete(e.target)));
      const current = sections.find((s) => visible.has(s));
      $$("[data-nav]").forEach((a) => a.classList.toggle("on", !!current && a.dataset.nav === current.id));
      $("[data-crumb-name]").textContent = current ? current.dataset.title : "Overview";
    }, { root, rootMargin: `-${top}px 0px -55% 0px` });
    sections.forEach((s) => observer.observe(s));
  };

  // Find a setting. The / shortcut in app.js focuses whichever box is showing.
  const filters = $$("[data-filter]");
  const markSearch = () => filters.forEach((input) => { if (input.offsetParent) input.setAttribute("data-search", ""); else input.removeAttribute("data-search"); });
  const filter = (query) => {
    const q = query.trim().toLowerCase();
    filters.forEach((input) => { if (input !== document.activeElement) input.value = query; });
    let any = false;
    sections.forEach((section) => {
      let hits = 0;
      const title = section.dataset.title.toLowerCase();
      $$(".st-row", section).filter((row) => !row.parentElement.closest(".st-row")).forEach((row) => {
        const text = `${row.textContent} ${row.dataset.keys || ""} ${title}`.toLowerCase();
        const hit = !q || q.split(/\s+/).every((word) => text.includes(word));
        row.classList.toggle("st-hidden-by-filter", !hit);
        if (hit) hits++;
      });
      section.classList.toggle("st-hidden-by-filter", hits === 0);
      if (hits) any = true;
    });
    $$(".st-group").forEach((g) => g.classList.toggle("st-hidden-by-filter", $$(".st-section", g).every((s) => s.classList.contains("st-hidden-by-filter"))));
    $$("[data-nav]").forEach((a) => {
      const hidden = !!q && document.getElementById(a.dataset.nav).classList.contains("st-hidden-by-filter");
      (a.closest("li") || a).hidden = hidden;
    });
    $$(".st-index-group").forEach((g) => { g.hidden = !!q && $$("li", g).every((li) => li.hidden); });
    $("[data-filter-empty]").hidden = any;
    $("[data-filter-q]").textContent = query.trim();
    ["#overview", ".st-saved-to"].forEach((sel) => $(sel)?.classList.toggle("st-hidden-by-filter", !!q));
  };
  filters.forEach((input) => {
    input.addEventListener("input", () => filter(input.value));
    input.addEventListener("keydown", (event) => { if (event.key === "Escape" && input.value) { event.stopPropagation(); input.value = ""; filter(""); } });
  });

  // Cmd-S saves the section you're in, or the one with changes.
  document.addEventListener("keydown", (event) => {
    if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "s") return;
    event.preventDefault();
    const here = document.activeElement?.closest?.("form[data-form]");
    const form = here && changed(here).length ? here : forms.find((f) => changed(f).length || f.closest(".has-problem"));
    if (!form) { toast("Nothing to save."); return; }
    form.closest(".st-section").scrollIntoView({ block: "nearest" });
    form.requestSubmit();
  });

  const mobileTop = $(".mobile-top");
  const measure = () => {
    if (mobileTop) wrap.style.setProperty("--mtop", `${mobileTop.offsetHeight}px`);
    markSearch();
    if (scroller() !== root) spy();
  };
  addEventListener("resize", measure);

  forms.forEach(update);
  renderSchedule();
  renderSpend();
  measure();
  spy();
  if (!isMac) $$("kbd[data-mod]", wrap).forEach((k) => { k.textContent = "Ctrl"; });
})();
