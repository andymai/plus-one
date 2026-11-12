(() => {
  const token = document.querySelector('meta[name="csrf-token"]')?.content;
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  document.body.addEventListener("htmx:configRequest", (event) => {
    if (token) event.detail.headers["X-CSRF-Token"] = token;
  });

  if (window.EventSource) {
    const stream = new EventSource("/events");
    stream.addEventListener("progress", (event) => {
      document.body.dispatchEvent(new CustomEvent("plus1:progress", { detail: JSON.parse(event.data) }));
    });
    stream.addEventListener("error", () => setTimeout(() => checkBuild(), 5000));
  }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/static/sw.js", { scope: "/" }).catch(() => {});
  }

  const go = (path) => { location.href = path; };
  const post = (action, fields = {}) => {
    const form = document.createElement("form");
    form.method = "post";
    form.action = action;
    for (const [name, value] of Object.entries({ csrf: token, ...fields })) {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      input.value = value;
      form.append(input);
    }
    document.body.append(form);
    form.submit();
  };

  document.documentElement.classList.add("has-js");

  const addToggle = (el, label) => {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "clamp-toggle";
    toggle.textContent = label;
    toggle.addEventListener("click", () => {
      const open = el.classList.toggle("is-open");
      toggle.textContent = open ? "Show less" : label;
    });
    el.after(toggle);
  };

  const clamps = (root) => {
    $$(".clamp:not([data-clamped])", root).forEach((el) => {
      if (!el.offsetParent) return;
      el.dataset.clamped = "1";
      if (el.scrollHeight <= el.clientHeight + 2) return;
      addToggle(el, "Show more");
    });
    $$(".clamp-list:not([data-clamped])", root).forEach((el) => {
      if (!el.offsetParent) return;
      el.dataset.clamped = "1";
      if (Array.from(el.children).some((li) => li.scrollWidth > li.clientWidth + 2)) addToggle(el, "Show full notes");
    });
  };

  const init = (root) => {
    if (!root || !root.querySelectorAll) return;
    clamps(root);
    if (!isMac) {
      $$("kbd[data-mod]", root).forEach((k) => { k.textContent = "Ctrl"; });
      $$(".kbd-hint", root).forEach((k) => { k.textContent = k.textContent.replace("⌘", "Ctrl "); });
    }
    $$(".countdown[data-left]", root).forEach((el) => {
      const bar = el.firstElementChild;
      if (!bar) return;
      const left = parseFloat(el.dataset.left) || 0;
      const total = parseFloat(el.dataset.total) || 8;
      bar.style.setProperty("--from", String(Math.min(1, left / total)));
      bar.style.animation = left > 0 ? `drain ${left}s linear forwards` : "none";
      const label = el.closest(".dcard")?.querySelector(".pending-left");
      if (!label || left <= 0) return;
      const due = Date.now() + left * 1000;
      const tick = setInterval(() => {
        if (!label.isConnected) return clearInterval(tick);
        const secs = Math.ceil((due - Date.now()) / 1000);
        label.textContent = secs > 0 ? `in ${secs}s` : "now";
        if (secs <= 0) clearInterval(tick);
      }, 250);
    });
    if (focusKey) {
      const again = items().find((el) => keyOf(el) === focusKey);
      if (again) again.classList.add("is-focused");
    }
  };

  const dismiss = (toast) => {
    if (!toast || toast.classList.contains("is-leaving")) return;
    toast.classList.add("is-leaving");
    setTimeout(() => toast.remove(), 220);
  };
  $$(".toast:not(.is-sticky)").forEach((toast) => setTimeout(() => dismiss(toast), toast.classList.contains("is-bad") ? 9000 : 4500));
  const url = new URL(location.href);
  if (url.searchParams.has("msg") || url.searchParams.has("error")) {
    url.searchParams.delete("msg");
    url.searchParams.delete("error");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
  }

  let focusKey = null;
  // At this width To do is two panes: picking a row in the list shows its review in the pane beside it.
  const wideQuery = matchMedia("(min-width: 1100px)");
  const isWide = () => wideQuery.matches && !!document.querySelector(".inbox");
  // Shortcuts act on the pane only once it shows the picked row, so a key pressed mid-load can't hit the previous PR.
  const paneCard = () => {
    const pane = document.getElementById("pane");
    const picked = document.querySelector(".inbox-list [data-nav-item].is-focused")?.dataset.href;
    const shown = pane?.querySelector(":scope > .pane")?.dataset.review;
    if (!pane || pane.hasAttribute("aria-busy") || (picked && picked !== `/review/${shown}`)) return null;
    return pane.querySelector(".dcard.is-page");
  };
  const items = () => $$("[data-nav-item]").filter((el) => el.offsetParent !== null && !(isWide() && el.matches(".tstack")));
  const keyOf = (el) => el.id || el.dataset.href || "";
  const focused = () => items().find((el) => el.classList.contains("is-focused"));
  const markFocused = (el) => {
    $$("[data-nav-item].is-focused").forEach((x) => x.classList.remove("is-focused"));
    el?.classList.add("is-focused");
    focusKey = el ? keyOf(el) : null;
  };
  const setFocus = (el, scroll = true) => {
    markFocused(el);
    if (!el) return;
    const target = el.matches(".row") ? el.querySelector("a.row-link") : (el.querySelector(":scope > summary") || el);
    target?.focus({ preventScroll: true });
    if (scroll) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    if (isWide() && el.closest(".inbox-list") && el.dataset.href) showInPane(el.dataset.href);
  };
  const move = (step) => {
    const list = items();
    if (!list.length) {
      const link = document.querySelector(step > 0 ? "[data-next]" : "[data-prev]");
      if (link) go(link.href);
      return;
    }
    const at = list.indexOf(focused());
    const next = at === -1 ? (step > 0 ? 0 : list.length - 1) : Math.max(0, Math.min(list.length - 1, at + step));
    setFocus(list[next]);
  };
  document.addEventListener("mousedown", (event) => {
    const item = event.target.closest("[data-nav-item]");
    if (item && !item.classList.contains("is-focused")) markFocused(item);
  });

  const openEnclosing = (el) => {
    for (let d = el?.closest("details"); d; d = d.parentElement?.closest("details")) d.open = true;
  };

  const EASE = "cubic-bezier(.2, .8, .2, 1)";
  const calm = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  // Both ends are measured with .is-folding on: it clips, which also keeps child margins inside the box, so
  // nothing jumps when the class comes off.
  const resize = (el, from, to, done) => {
    const distance = Math.abs(to - from);
    if (distance < 2 || distance > innerHeight * 2) {
      el.classList.remove("is-folding");
      done?.();
      return null;
    }
    const anim = el.animate([{ height: `${from}px` }, { height: `${to}px` }],
      { duration: Math.min(320, 160 + distance / 6), easing: EASE });
    anim.onfinish = () => {
      el.classList.remove("is-folding");
      done?.();
    };
    return anim;
  };

  const folds = new WeakMap();
  const isOpen = (d) => d.open && !folds.get(d)?.closing;
  const folding = (d) => !calm() && d.isConnected && !d.matches(".more, .popover, .edit");
  const stopFold = (d) => {
    folds.get(d)?.anim?.cancel();
    folds.delete(d);
    d.classList.remove("is-folding");
  };
  const closedHeight = (d) => {
    const box = getComputedStyle(d);
    const summary = d.querySelector(":scope > summary");
    let height = ["borderTopWidth", "borderBottomWidth", "paddingTop", "paddingBottom"].reduce((sum, k) => sum + parseFloat(box[k]), 0);
    if (summary && getComputedStyle(summary).position !== "absolute") {
      const s = getComputedStyle(summary);
      height += summary.getBoundingClientRect().height + parseFloat(s.marginTop) + parseFloat(s.marginBottom);
    }
    return height;
  };
  const setOpen = (d, open) => {
    if (isOpen(d) === open) return;
    if (!folding(d)) {
      stopFold(d);
      d.open = open;
      return;
    }
    d.classList.add("is-folding");
    const from = d.getBoundingClientRect().height;
    folds.get(d)?.anim?.cancel();
    d.open = true;
    const to = open ? d.getBoundingClientRect().height : closedHeight(d);
    const anim = resize(d, from, to, () => {
      folds.delete(d);
      if (!open) d.open = false;
    });
    if (anim) folds.set(d, { anim, closing: !open });
  };
  const expand = (d) => {
    let outer = null;
    for (let p = d; p; p = p.parentElement?.closest("details")) if (!isOpen(p)) outer = p;
    if (!outer) return;
    for (let p = d; p !== outer; p = p.parentElement.closest("details")) {
      stopFold(p);
      p.open = true;
    }
    setOpen(outer, true);
  };
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented) return;
    const summary = event.target.closest("summary");
    const d = summary?.parentElement;
    if (!(d instanceof HTMLDetailsElement) || d.querySelector(":scope > summary") !== summary || !folding(d)) return;
    const inner = event.target.closest("a, button, input, select, textarea, label");
    if (inner && summary.contains(inner)) return;
    event.preventDefault();
    setOpen(d, !isOpen(d));
  });

  // A card or the live panel swaps as a whole, so its box eases between the two heights and the rows below follow.
  const kindOf = (el) => `${el.tagName} ${el.firstElementChild?.className || ""}`;
  const morph = (el, was) => {
    el.classList.add("is-folding");
    resize(el, was.height, el.getBoundingClientRect().height);
    if (kindOf(el) === was.kind) return;
    for (const child of el.children) child.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 220, easing: "ease-out" });
  };

  const feedKey = (li) => li.dataset.href || `${li.className} ${li.textContent.trim()}`;
  const outcome = (li) => li.querySelector(".row-meta > .chip")?.textContent.trim() || "";
  const feedRows = (feed) => new Map($$(".list > li", feed).map((li) => [feedKey(li), { top: li.getBoundingClientRect().top, outcome: outcome(li) }]));
  const replayFeed = (feed, was) => {
    $$(".list > li", feed).forEach((li) => {
      const old = was.get(feedKey(li));
      if (!old) {
        li.classList.add("is-new");
        return;
      }
      const dy = old.top - li.getBoundingClientRect().top;
      if (Math.abs(dy) > 1) li.animate([{ transform: `translateY(${dy}px)` }, { transform: "none" }], { duration: 300, easing: EASE });
      if (li.matches(".row") && old.outcome !== outcome(li)) li.classList.add("is-changed");
    });
  };

  // A poll that lands while the owner has unsaved words on the card waits for a later one, so they aren't swapped away.
  document.body.addEventListener("htmx:beforeSwap", (event) => {
    if (!event.target.matches?.("[data-poll]")) return;
    const typed = [...(event.detail.target?.querySelectorAll("textarea") || [])].some((t) => t.value !== t.defaultValue);
    if (typed) event.detail.shouldSwap = false;
  });

  const swapped = new Map();
  document.body.addEventListener("htmx:beforeSwap", (event) => {
    const target = event.detail.target;
    if (calm() || !event.detail.shouldSwap || !target?.id) return;
    if (target.id === "feed") {
      swapped.set("feed", feedRows(target));
    } else if (target.id.startsWith("card-") || target.id.startsWith("pane-") || target.id === "live") {
      const had = target.classList.contains("is-folding");
      target.classList.add("is-folding");
      swapped.set(target.id, { height: target.getBoundingClientRect().height, kind: kindOf(target) });
      if (!had) target.classList.remove("is-folding");
    }
  });
  // afterSwap runs in the same task as the swap. htmx:load waits out the settle delay, so a frame would paint
  // the new content at its final size first.
  document.body.addEventListener("htmx:afterSwap", (event) => {
    const id = event.detail.target?.id;
    const was = id && swapped.get(id);
    const el = was && document.getElementById(id);
    if (!el) return;
    swapped.delete(id);
    if (id === "feed") replayFeed(el, was);
    else morph(el, was);
  });

  // Focus excerpts carry L- row ids and the full diff carries D- ids, so a link can fall back to the full diff.
  const reveal = (id) => {
    const row = document.getElementById(id) || document.getElementById(id.replace(/^L-/, "D-"));
    if (!row) return false;
    openEnclosing(row);
    row.scrollIntoView({ block: "center", behavior: "smooth" });
    row.classList.remove("flash");
    void row.offsetWidth;
    row.classList.add("flash");
    history.replaceState(null, "", `#${id}`);
    return true;
  };
  const hash = decodeURIComponent(location.hash.slice(1));
  if (hash.startsWith("L-")) setTimeout(() => reveal(hash), 60);
  else if (hash) document.getElementById(hash)?.closest("details")?.setAttribute("open", "");

  document.addEventListener("click", (event) => {
    const closer = event.target.closest(".toast-x");
    if (closer) dismiss(closer.closest(".toast"));
    const cancelled = event.target.closest("[data-close-details]")?.closest("details");
    if (cancelled) {
      cancelled.querySelectorAll("textarea").forEach((area) => { area.value = area.defaultValue; });
      cancelled.open = false;
    }
    // Without JS a chip submits its own text; with it, chips add up in the box so they combine with typed words.
    const chip = event.target.closest("[data-chip]");
    const box = chip?.form?.querySelector("[data-rewrite-box]");
    if (box) {
      event.preventDefault();
      const words = chip.dataset.chip;
      if (!box.value.includes(words)) box.value = [box.value.trim(), words].filter(Boolean).join(" ");
      box.focus();
    }
    $$("details.more[open], details.popover[open]").forEach((d) => {
      if (!d.contains(event.target)) d.open = false;
    });
    const link = event.target.closest('a[href*="#L-"]');
    if (link) {
      const url = new URL(link.href, location.href);
      if (url.pathname === location.pathname && reveal(url.hash.slice(1))) event.preventDefault();
    }
  });
  document.addEventListener("toggle", (event) => {
    const d = event.target;
    if (!(d instanceof HTMLDetailsElement) || !d.open) return;
    clamps(d);
    if (d.matches(".titem") && !d.classList.contains("is-focused") && !isWide()) setFocus(d, false);
    if (d.matches(".popover, .edit, [data-autofocus]")) {
      const field = d.querySelector("input[type=text], textarea");
      if (field) {
        field.focus();
        if (field.tagName === "TEXTAREA") field.setSelectionRange(field.value.length, field.value.length);
      }
    }
  }, true);

  const palette = document.getElementById("palette");
  const shortcuts = document.getElementById("shortcuts");
  let data = null;
  let shown = [];
  let active = 0;

  const loadData = async () => {
    try {
      const response = await fetch("/palette.json", { headers: { Accept: "application/json" } });
      if (response.ok) data = await response.json();
    } catch (_) {}
    if (palette?.open) render();
  };

  const commands = () => {
    const paused = data?.paused;
    return [
      { group: "Go to", label: "To do", icon: "inbox", hint: "G T", run: () => go("/") },
      { group: "Go to", label: "Activity", icon: "activity", hint: "G A", run: () => go("/activity") },
      { group: "Go to", label: "Insights", icon: "chart", hint: "G I", run: () => go("/insights") },
      { group: "Go to", label: "Skipped PRs", icon: "activity", run: () => go("/activity?show=skipped") },
      { group: "Go to", label: "Handed-off PRs", icon: "user-x", run: () => go("/activity?show=handed_off") },
      { group: "Go to", label: "Start a grading queue", icon: "thumbs-up", run: () => go("/grade") },
      { group: "Go to", label: "Rubric changes", icon: "book", run: () => go("/proposals") },
      { group: "Go to", label: "Notifications", icon: "bell", run: () => go("/settings#notifications") },
      { group: "Go to", label: "Settings", icon: "sliders", run: () => go("/settings") },
      { group: "Go to", label: "What's new", icon: "sparkles", run: () => go("/whats-new") },
      { group: "Go to", label: "Edit rubric", icon: "pencil", run: () => go("/settings/rubric") },
      { group: "Actions", label: paused ? "Resume checking PRs" : "Pause checking PRs", icon: paused ? "play" : "pause",
        run: () => post(paused ? "/resume" : "/pause", { next: location.pathname }) },
      { group: "Actions", label: "Check my GitHub reviews now", icon: "refresh", run: () => post("/insights/sync") },
      { group: "Actions", label: "Keyboard shortcuts", icon: "keyboard", hint: "?", run: () => openShortcuts() },
    ];
  };

  const prItems = () => (data?.prs || []).map((pr) => ({
    group: "Pull requests", icon: "pr", number: pr.label, title: pr.title, hint: pr.outcome,
    haystack: `${pr.label} #${pr.pr} ${pr.pr} ${pr.repo} ${pr.title} ${pr.author} ${pr.outcome}`.toLowerCase(),
    run: () => go(`/review/${pr.review}`),
  }));

  const matches = (text, terms) => terms.every((t) => text.includes(t));

  const results = (query) => {
    const q = query.trim().toLowerCase();
    const terms = q.split(/\s+/).filter(Boolean);
    const out = [];
    const number = q.match(/^#?(\d+)$/);
    if (number) {
      out.push({ group: "Pull request #" + number[1], label: `Open #${number[1]}`, icon: "arrow-right", run: () => go(`/pr/${number[1]}`) });
      out.push({ group: "Pull request #" + number[1], label: `Review #${number[1]} now`, icon: "refresh", hint: "runs a fresh review",
        run: () => post("/review-now", { target: number[1] }) });
    }
    out.push(...commands().filter((c) => matches(c.label.toLowerCase(), terms)));
    const prs = prItems().filter((p) => matches(p.haystack, terms));
    out.push(...prs.slice(0, terms.length ? 30 : 8).map((p) => ({ ...p, group: terms.length ? "Pull requests" : "Recent pull requests" })));
    return out;
  };

  const render = () => {
    const input = palette.querySelector("input");
    const list = palette.querySelector(".palette-list");
    shown = results(input.value);
    active = Math.min(active, Math.max(0, shown.length - 1));
    list.replaceChildren();
    if (!shown.length) {
      const empty = document.createElement("li");
      empty.className = "palette-empty";
      empty.textContent = data ? "No matches" : "Loading…";
      list.append(empty);
      return;
    }
    let group = null;
    shown.forEach((item, i) => {
      if (item.group !== group) {
        group = item.group;
        const head = document.createElement("li");
        head.className = "palette-group";
        head.setAttribute("role", "presentation");
        head.textContent = group;
        list.append(head);
      }
      const li = document.createElement("li");
      li.className = "palette-item";
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(i === active));
      li.dataset.index = String(i);
      li.innerHTML = `<svg class="i" aria-hidden="true"><use href="#i-${item.icon}"/></svg>`;
      const label = document.createElement("span");
      label.className = "palette-label";
      if (item.number) {
        const b = document.createElement("b");
        b.textContent = item.number;
        label.append(b, document.createTextNode(` ${item.title}`));
      } else {
        label.textContent = item.label;
      }
      li.append(label);
      if (item.hint) {
        const hint = document.createElement("span");
        hint.className = "palette-hint";
        hint.textContent = item.hint;
        li.append(hint);
      }
      list.append(li);
    });
    list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  };

  const select = (i) => {
    active = i;
    $$(".palette-item", palette).forEach((li) => li.setAttribute("aria-selected", String(Number(li.dataset.index) === i)));
    palette.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  };

  const run = (i) => {
    const item = shown[i];
    if (!item) return;
    palette.close();
    item.run();
  };

  const openPalette = () => {
    if (!palette || palette.open) return;
    const input = palette.querySelector("input");
    input.value = "";
    active = 0;
    render();
    palette.showModal();
    input.focus();
    loadData();
  };

  const openShortcuts = () => {
    if (!shortcuts) return;
    if (palette?.open) palette.close();
    shortcuts.showModal();
  };

  if (palette) {
    const input = palette.querySelector("input");
    input.addEventListener("input", () => { active = 0; render(); });
    input.addEventListener("keydown", (event) => {
      const down = event.key === "ArrowDown" || (event.ctrlKey && event.key === "n");
      const up = event.key === "ArrowUp" || (event.ctrlKey && event.key === "p");
      if (down || up) {
        event.preventDefault();
        if (shown.length) select((active + (down ? 1 : -1) + shown.length) % shown.length);
      } else if (event.key === "Enter") {
        event.preventDefault();
        run(active);
      }
    });
    palette.addEventListener("mousemove", (event) => {
      const li = event.target.closest(".palette-item");
      if (li && Number(li.dataset.index) !== active) select(Number(li.dataset.index));
    });
    palette.addEventListener("click", (event) => {
      const li = event.target.closest(".palette-item");
      if (li) run(Number(li.dataset.index));
      else if (event.target === palette) palette.close();
    });
  }
  if (shortcuts) {
    shortcuts.addEventListener("click", (event) => {
      if (event.target === shortcuts || event.target.closest("[data-close]")) shortcuts.close();
    });
  }
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-open-palette]")) openPalette();
    if (event.target.closest("[data-open-shortcuts]")) openShortcuts();
  });

  let pendingG = false;
  const typing = (el) => el && el.closest && el.closest("input, textarea, select, [contenteditable]");

  document.addEventListener("keydown", (event) => {
    const mod = event.metaKey || event.ctrlKey;
    const key = event.key;
    if (mod && key.toLowerCase() === "k") {
      event.preventDefault();
      if (palette?.open) palette.close(); else openPalette();
      return;
    }
    if (document.querySelector("dialog[open]")) return;

    if (mod && key === "Enter") {
      event.preventDefault();
      if (event.repeat) return;
      if (typing(event.target)) {
        event.target.form?.requestSubmit();
        return;
      }
      const card = isWide() ? paneCard() : focused() || document.querySelector(".dcard.is-page");
      if (!card) return;
      if (card.matches(".tstack")) {
        const part = card.querySelector("details.titem");
        if (part) {
          expand(part);
          setFocus(part);
        } else setOpen(card, true);
        return;
      }
      if (card.matches("details") && !isOpen(card)) {
        setOpen(card, true);
        return;
      }
      const decision = card.querySelector(card.matches("details") ? ":scope > .tbody > .decision" : ":scope > .decision");
      decision?.querySelector(".primary:not([disabled])")?.click();
      return;
    }

    if (key === "Escape") {
      const open = $$("details.more[open], details.popover[open], details.edit[open], details[data-autofocus][open], details.grade-wrong[open]");
      open.forEach((d) => { d.open = false; });
      if (typing(event.target)) event.target.blur();
      else if (!open.length && !isWide()) setFocus(null);
      return;
    }

    const gradeCard = document.getElementById("grade-card");
    if (gradeCard && !mod && !event.altKey && !event.target.closest("textarea, input[type=text], select, [contenteditable]")) {
      if (event.repeat) return;
      const wrong = gradeCard.querySelector("details.grade-wrong");
      const k = key.toLowerCase();
      const press = (el) => {
        if (!el) return;
        event.preventDefault();
        el.click();
      };
      if (k === "y") return press(gradeCard.querySelector("[data-grade-agree]"));
      if (k === "s") return press(gradeCard.querySelector("[data-grade-skip]"));
      if (k === "n" && wrong) {
        event.preventDefault();
        wrong.open = true;
        wrong.querySelector("input[type=radio]")?.focus();
        return;
      }
      if (wrong?.open && /^[1-9]$/.test(key)) {
        const radio = wrong.querySelectorAll("input[type=radio]")[Number(key) - 1];
        if (radio) {
          event.preventDefault();
          radio.checked = true;
          radio.focus();
        }
        return;
      }
    }

    if (typing(event.target) || mod || event.altKey) return;

    if (pendingG) {
      pendingG = false;
      const dest = { t: "/", a: "/activity", i: "/insights" }[key.toLowerCase()];
      if (dest) { event.preventDefault(); go(dest); }
      return;
    }

    const current = focused();
    switch (key) {
      case "g":
        pendingG = true;
        setTimeout(() => { pendingG = false; }, 900);
        break;
      case "j":
        event.preventDefault();
        move(1);
        break;
      case "k":
        event.preventDefault();
        move(-1);
        break;
      case "Enter":
        if (!current) break;
        if (event.shiftKey && current.dataset.href) {
          event.preventDefault();
          go(current.dataset.href);
        } else if (!event.target.closest("a, button, summary") && !isWide()) {
          event.preventDefault();
          if (current.matches("details")) setOpen(current, !isOpen(current));
          else if (current.dataset.href) go(current.dataset.href);
        }
        break;
      case "o":
        if (current && current.dataset.gh) {
          event.preventDefault();
          window.open(current.dataset.gh, "_blank", "noopener");
        }
        break;
      case "e":
      case "r": {
        const scope = isWide() ? paneCard() : current;
        const panel = scope && scope.querySelector(key === "e" ? "details.edit" : "details.rewrite, details.rewrite-again");
        if (panel) {
          event.preventDefault();
          openEnclosing(panel);
        }
        break;
      }
      case "i": {
        const target = isWide() ? paneCard() : current || document.querySelector(".dcard.is-page");
        if (target?.matches(".tstack")) {
          event.preventDefault();
          target.open = true;
          const part = target.querySelector("details.titem");
          if (part) setFocus(part);
          break;
        }
        const handOff = target?.querySelector('form[action$="/hand_off"] button');
        if (handOff) {
          event.preventDefault();
          handOff.click();
        }
        break;
      }
      case "u": {
        const undo = current?.querySelector("[data-undo]")
          || (lastActed && document.getElementById(lastActed)?.querySelector("[data-undo]"))
          || document.querySelector("[data-undo]");
        if (undo) {
          event.preventDefault();
          undo.click();
        }
        break;
      }
      case "/": {
        event.preventDefault();
        const search = document.querySelector("[data-search]");
        if (search) { search.focus(); search.select(); } else openPalette();
        break;
      }
      case "?":
        event.preventDefault();
        openShortcuts();
        break;
    }
  });

  const enable = document.getElementById("enable-push");
  const status = document.getElementById("push-status");
  const say = (text) => { if (status) status.textContent = text; };
  const keyBytes = (b64) => {
    const raw = atob((b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  };
  const encode = (buffer) => btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  if (enable) {
    enable.addEventListener("click", async () => {
      try {
        if (!("PushManager" in window)) {
          say("This browser can't receive web push here. On iPhone, open Plus One from the home screen first.");
          return;
        }
        if ((await Notification.requestPermission()) !== "granted") {
          say("Notifications were not allowed.");
          return;
        }
        const registration = await navigator.serviceWorker.ready;
        const subscription = (await registration.pushManager.getSubscription()) ||
          await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(enable.dataset.key) });
        const body = new URLSearchParams({
          csrf: token,
          endpoint: subscription.endpoint,
          p256dh: encode(subscription.getKey("p256dh")),
          auth: encode(subscription.getKey("auth")),
          label: navigator.userAgent.includes("iPhone") ? "iPhone" : navigator.platform || "browser",
        });
        const response = await fetch("/push/subscribe", { method: "POST", body, headers: { "X-CSRF-Token": token } });
        say(response.ok ? "This device will get notifications." : "The server refused the subscription.");
        if (response.ok) setTimeout(() => location.reload(), 800);
      } catch (error) {
        say(`Could not enable notifications: ${error.message}`);
      }
    });
  }

  let advance = null;
  let lastActed = null;
  document.body.addEventListener("htmx:beforeRequest", (event) => {
    const target = event.detail.target;
    if (event.detail.requestConfig?.verb !== "post" || !/^(card|pane)-/.test(target?.id || "")) return;
    lastActed = target.id;
    if (!target.matches("details.titem")) return;
    const all = $$("[data-nav-item]");
    const later = all.slice(all.indexOf(target) + 1).filter((el) => el.matches("details.titem:not(.tstack)"));
    advance = { id: target.id, next: later[0]?.id || null };
  });
  const advanceFrom = (el) => {
    if (!advance || el.id !== advance.id) return;
    const step = advance;
    advance = null;
    if (!el.matches(".is-pending, .slim") || !step.next) return;
    const next = document.getElementById(step.next);
    if (!next) return;
    expand(next);
    setFocus(next);
  };

  const todoRoot = document.querySelector("[data-todo-keys]");
  const pageKeys = new Set((todoRoot?.dataset.todoKeys || "").split(",").filter(Boolean));
  let stateTimer = null;
  const refreshState = async () => {
    try {
      const response = await fetch("/todo-state.json", { headers: { Accept: "application/json" } });
      if (!response.ok) return;
      const state = await response.json();
      $$("[data-todo-count]").forEach((el) => {
        el.textContent = String(state.count);
        el.hidden = !state.count;
      });
      document.title = (state.count ? `(${state.count}) ` : "") + document.title.replace(/^\(\d+\) /, "");
      const favicon = document.querySelector("link[data-favicon]");
      const href = state.count ? `/static/fav-${Math.min(state.count, 9)}.svg` : "/static/icon.svg";
      if (favicon && favicon.getAttribute("href") !== href) favicon.setAttribute("href", href);
      const pill = document.querySelector("[data-new-pill]");
      if (pill) {
        const fresh = state.keys.filter((k) => !pageKeys.has(k)).length;
        pill.hidden = !fresh;
        pill.querySelector("span").textContent = `${fresh} new or updated. Show ${fresh === 1 ? "it" : "them"}`;
      }
    } catch (_) {}
  };
  const refreshSoon = (ms = 1500) => {
    clearTimeout(stateTimer);
    stateTimer = setTimeout(refreshState, ms);
  };
  document.body.addEventListener("plus1:progress", (event) => {
    if (["done", "error"].includes(event.detail?.stage)) refreshSoon();
  });

  // iOS keeps a home screen app's page in memory for hours, so coming back has to refetch, not just repaint.
  const fresh = document.querySelector("[data-fresh]");
  let freshAt = Date.now();
  let freshFailed = false;
  const paintFresh = () => {
    if (!fresh) return;
    const secs = Math.floor((Date.now() - freshAt) / 1000);
    const age = secs < 60 ? "just now" : secs < 3600 ? `${Math.floor(secs / 60)}m ago` :
      secs < 86400 ? `${Math.floor(secs / 3600)}h ago` : `${Math.floor(secs / 86400)}d ago`;
    fresh.textContent = freshFailed ? `Couldn't refresh. Updated ${age}` : `Updated ${age}`;
    fresh.classList.toggle("is-stale", freshFailed || secs > 150);
  };
  const markFresh = () => {
    freshAt = Date.now();
    freshFailed = false;
    paintFresh();
  };
  if (fresh) {
    ["htmx:sendError", "htmx:timeout", "htmx:responseError"].forEach((name) => {
      document.body.addEventListener(name, (event) => {
        if (event.detail.elt?.id !== "feed") return;
        freshFailed = true;
        paintFresh();
      });
    });
    setInterval(paintFresh, 15000);
  }
  const resume = () => {
    refreshSoon(300);
    paintFresh();
    document.body.dispatchEvent(new CustomEvent("plus1:refresh"));
  };
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") resume();
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) resume();
  });
  setInterval(refreshState, 60000);
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-new-pill]")) location.reload();
  });

  const toast = (text) => {
    const el = document.createElement("div");
    el.className = "toast is-bad";
    el.setAttribute("role", "alert");
    el.innerHTML = '<span class="st st-err" aria-hidden="true"><svg focusable="false"><use href="#st-error"/></svg></span><span></span>';
    el.lastChild.textContent = text;
    document.getElementById("toasts")?.append(el);
    setTimeout(() => dismiss(el), 9000);
  };

  // An update restarts the server under every open page. Pick it up by reloading, unless that would lose what's typed.
  const metaContent = (name) => document.querySelector(`meta[name="${name}"]`)?.content;
  const build = metaContent("plus1-build");
  const unsaved = () => Boolean(document.querySelector(".st-section.is-dirty, dialog[open]"))
    || $$("textarea, input:not([type=hidden]):not([type=checkbox]):not([type=radio])").some((el) => el.value !== el.defaultValue)
    || $$("input[type=checkbox], input[type=radio]").some((el) => el.checked !== el.defaultChecked)
    || $$("select").some((el) => el.selectedIndex !== Math.max([...el.options].findIndex((o) => o.defaultSelected), 0));
  let outdated = false;
  const checkBuild = async () => {
    if (!build || outdated) return;
    try {
      const response = await fetch("/healthz", { cache: "no-store" });
      const served = response.ok ? (await response.text()).split(" ")[1] : "";
      if (!served || served === build) return;
      outdated = true;
      if (!unsaved()) { location.reload(); return; }
      const pill = document.createElement("button");
      pill.type = "button";
      pill.className = "update-pill";
      pill.textContent = "Plus One updated. Reload";
      pill.addEventListener("click", () => location.reload());
      document.body.append(pill);
    } catch (_) {}
  };
  setInterval(checkBuild, 60000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) checkBuild(); });

  try {
    const version = metaContent("plus1-version");
    const seen = localStorage.getItem("plus1.version");
    if (version && seen && seen !== version) {
      const newer = version.localeCompare(seen, undefined, { numeric: true }) > 0;
      const el = document.createElement("div");
      el.className = "toast";
      el.setAttribute("role", "status");
      const text = document.createElement("span");
      text.textContent = `${newer ? "Updated to" : "Back on"} ${version}. `;
      const link = document.createElement("a");
      link.href = "/whats-new";
      link.textContent = "See what's new";
      text.append(link);
      el.append(text);
      document.getElementById("toasts")?.append(el);
      setTimeout(() => dismiss(el), 9000);
    }
    if (version) localStorage.setItem("plus1.version", version);
  } catch (_) {}

  // A home screen app has no browser pull-to-refresh. Reloading while Tailscale is down would strand it on
  // Safari's error page with no way back, so it checks the server first.
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const ptr = document.querySelector("[data-ptr]");
  if (standalone && ptr) {
    document.documentElement.classList.add("is-standalone");
    const ARM = 64;
    let start = null;
    let pull = 0;
    const scrolled = (el) => {
      for (; el && el !== document.body; el = el.parentElement) if (el.scrollTop > 0) return true;
      return (document.scrollingElement?.scrollTop || 0) > 0;
    };
    const settle = () => {
      start = null;
      pull = 0;
      ptr.classList.remove("is-pulling", "is-armed", "is-loading");
      ptr.style.setProperty("--pull", "0");
    };
    document.addEventListener("touchstart", (event) => {
      start = null;
      if (event.touches.length !== 1 || ptr.classList.contains("is-loading") || document.querySelector("dialog[open]") ||
          event.target.closest("input, textarea, select, [contenteditable]") || scrolled(event.target)) return;
      start = { x: event.touches[0].clientX, y: event.touches[0].clientY, pulling: false };
    }, { passive: true });
    document.addEventListener("touchmove", (event) => {
      if (!start) return;
      const dx = event.touches[0].clientX - start.x;
      const dy = event.touches[0].clientY - start.y;
      if (!start.pulling) {
        if (Math.abs(dx) < 3 && Math.abs(dy) < 3) return;
        if (dy <= 0 || Math.abs(dx) >= dy) { start = null; return; }
        start.pulling = true;
        ptr.classList.add("is-pulling");
      }
      if (event.cancelable) event.preventDefault();
      pull = Math.min(ARM * 1.5, Math.max(0, dy) * 0.5);
      ptr.style.setProperty("--pull", String(pull));
      ptr.classList.toggle("is-armed", pull >= ARM);
    }, { passive: false });
    document.addEventListener("touchend", () => {
      if (!start?.pulling) { start = null; return; }
      if (pull < ARM) return settle();
      start = null;
      ptr.classList.remove("is-pulling");
      ptr.classList.add("is-loading");
      const abort = new AbortController();
      const timer = setTimeout(() => abort.abort(), 8000);
      fetch("/healthz", { cache: "no-store", signal: abort.signal })
        .then((response) => {
          if (!response.ok) throw new Error(String(response.status));
          location.reload();
        })
        .catch(() => {
          settle();
          toast("Can't reach Plus One. Check Tailscale, then pull again.");
        })
        .finally(() => clearTimeout(timer));
    });
    document.addEventListener("touchcancel", () => {
      if (start?.pulling) settle();
      start = null;
    });
  }

  // The bar clones the card's buttons rather than rendering its own, so the card stays the only thing htmx swaps.

  let bar = null;
  let barSource = null;
  const barVisibility = () => {
    if (!bar) return;
    const rect = barSource && barSource.getBoundingClientRect();
    const onScreen = !!rect && rect.bottom > 60 && rect.top < window.innerHeight - 60;
    bar.classList.toggle("buttons-visible", onScreen);
  };
  const syncBar = () => {
    if (!bar) return;
    const slot = bar.querySelector("[data-bar-actions]");
    const card = document.getElementById(bar.dataset.card);
    const decision = card?.querySelector(":scope > .decision");
    const undo = card?.querySelector(".pending-row form");
    const source = decision || undo;
    slot.replaceChildren();
    const nodes = decision ? [decision.querySelector(":scope > form, :scope > .button"), ...(card.querySelector(":scope > .buttons")?.children || [])]
      : undo ? [undo] : [];
    nodes.filter((n) => n && !n.matches("details, .push-right")).forEach((n) => slot.append(n.cloneNode(true)));
    if (window.htmx && slot.children.length) window.htmx.process(slot);
    bar.classList.toggle("has-actions", slot.children.length > 0);
    barSource = source;
    barVisibility();
  };
  const bindBar = () => {
    bar = document.querySelector("[data-action-bar]");
    syncBar();
  };
  bindBar();
  document.addEventListener("scroll", barVisibility, { capture: true, passive: true });
  window.addEventListener("resize", barVisibility);

  const shownReview = () => document.querySelector("#pane > .pane")?.dataset.review;
  const listRows = () => items().filter((el) => el.closest(".inbox-list") && el.dataset.href);
  const rowFor = (href) => listRows().find((el) => el.dataset.href === href);
  const setMode = (review) => {
    const content = document.getElementById("content");
    if (!content?.classList.contains("has-inbox")) return;
    content.classList.toggle("is-review", review);
    content.classList.toggle("is-home", !review);
  };
  let paneLoad = 0;
  async function showInPane(href, push = true) {
    const id = href.match(/^\/review\/(\d+)$/)?.[1];
    const pane = document.getElementById("pane");
    if (!pane || !id) return;
    if (id === shownReview()) {
      paneLoad++;
      pane.removeAttribute("aria-busy");
      if (push && location.pathname !== href) {
        history.pushState(null, "", href);
        setMode(true);
      }
      return;
    }
    const load = ++paneLoad;
    pane.setAttribute("aria-busy", "true");
    let html;
    try {
      const response = await fetch(`/partials/pane/${id}`, { headers: { Accept: "text/html" } });
      if (!response.ok) throw new Error(String(response.status));
      html = await response.text();
    } catch (_) {
      if (load === paneLoad) {
        pane.removeAttribute("aria-busy");
        toast("Couldn't load that review. Try again.");
      }
      return;
    }
    if (load !== paneLoad) return;
    pane.removeAttribute("aria-busy");
    pane.innerHTML = html;
    pane.scrollTop = 0;
    if (window.htmx) window.htmx.process(pane);
    init(pane);
    bindBar();
    if (!calm()) pane.firstElementChild?.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 160, easing: "ease-out" });
    if (push) {
      if (location.pathname !== href) history.pushState(null, "", href);
      setMode(true);
    }
  }

  // Capture runs before the fold handler, so a row picks a review instead of opening.
  document.addEventListener("click", (event) => {
    if (!isWide() || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const step = event.target.closest("#pane [data-next], #pane [data-prev]");
    if (step) {
      const row = rowFor(new URL(step.href).pathname);
      if (row) {
        event.preventDefault();
        setFocus(row);
      }
      return;
    }
    const row = event.target.closest(".inbox-list [data-nav-item]");
    if (!row || row.matches(".tstack")) return;
    if (row.matches("details") ? event.target.closest("summary")?.parentElement !== row
      : event.target.closest("button, a, input, select, textarea, label")) return;
    event.preventDefault();
    setFocus(row, false);
  }, true);

  window.addEventListener("popstate", () => {
    if (!isWide()) return;
    const review = /^\/review\/\d+$/.test(location.pathname);
    const href = review ? location.pathname : listRows().find((el) => el.matches("details.titem"))?.dataset.href;
    setMode(review);
    if (!href) return;
    const row = rowFor(href);
    if (row) markFocused(row);
    showInPane(href, false);
  });

  const enterWide = () => {
    if (!isWide()) return;
    $$(".inbox-list details.tstack").forEach((d) => { d.open = true; });
    const id = shownReview();
    const row = id && rowFor(`/review/${id}`);
    if (row) markFocused(row);
  };
  wideQuery.addEventListener("change", enterWide);
  enterWide();

  // Acting in the pane moves it to the next PR right away. The row keeps the countdown and Undo until it lands.
  let paneStep = null;
  document.body.addEventListener("htmx:beforeRequest", (event) => {
    const target = event.detail.target;
    if (!isWide() || event.detail.requestConfig?.verb !== "post" || !target?.id?.startsWith("pane-")) return;
    const pr = target.id.slice(5);
    const rows = listRows();
    const at = rows.findIndex((el) => el.id === `card-${pr}`);
    const rest = at === -1 ? rows : [...rows.slice(at + 1), ...rows.slice(0, at).reverse()];
    paneStep = { pr, next: rest.find((el) => el.matches("details.titem"))?.id || null };
  });
  const stepFrom = (el) => {
    if (!paneStep || el.id !== `pane-${paneStep.pr}`) return;
    const step = paneStep;
    paneStep = null;
    if (!el.matches(".is-pending, .slim")) return;
    const row = document.getElementById(`card-${step.pr}`);
    if (row && window.htmx) {
      window.htmx.ajax("GET", `/partials/card/${step.pr}?variant=${row.dataset.variant || "card"}`,
        { target: `#card-${step.pr}`, swap: "outerHTML" });
      lastActed = `card-${step.pr}`;
    }
    const next = step.next && document.getElementById(step.next);
    if (next) setFocus(next);
  };
  const retire = (el) => {
    // A row still polling for its next review has to stay until the finished card replaces it.
    if (!isWide() || !el.matches?.(".inbox-list .slim:not(.is-reviewing)") || el.querySelector(".err, .reasons-label")) return;
    setTimeout(() => {
      if (!el.isConnected) return;
      const group = el.closest(".group");
      const gone = () => {
        el.remove();
        if (group && !group.querySelector(".tlist [data-nav-item], .tlist .dcard")) group.remove();
      };
      if (calm()) return gone();
      el.classList.add("is-folding");
      resize(el, el.getBoundingClientRect().height, 0, gone);
    }, 2500);
  };

  document.body.addEventListener("htmx:load", (event) => {
    const el = event.detail.elt;
    init(el);
    if (el.id === "feed") markFresh();
    advanceFrom(event.detail.elt);
    stepFrom(event.detail.elt);
    retire(event.detail.elt);
    if (event.detail.elt.matches?.(".slim")) refreshSoon(400);
    if (bar && event.detail.elt.id === bar.dataset.card) syncBar();
  });
  init(document);
})();
