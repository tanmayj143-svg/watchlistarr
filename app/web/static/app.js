/* Watchlistarr frontend — vanilla JS, no build step, no CDN. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  /* ---------------------------------------------------------------- toast */
  function toast(msg, kind = "") {
    let host = $(".toast-host");
    if (!host) {
      host = document.createElement("div");
      host.className = "toast-host";
      document.body.appendChild(host);
    }
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(() => {
      el.style.transition = "opacity .3s, transform .3s";
      el.style.opacity = "0";
      el.style.transform = "translateX(20px)";
      setTimeout(() => el.remove(), 320);
    }, 4200);
  }
  window.toast = toast;

  /* ---------------------------------------------------------------- fetch */
  async function api(path, opts = {}) {
      const init = { credentials: "same-origin", headers: {}, ...opts };
      if (init.body && typeof init.body !== "string") {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(init.body);
      }
      const res = await fetch(path, init);
      if (res.status === 401) {
        // Never redirect to /login from the login page itself, and never more
        // than once: a page-level fetch would otherwise bounce the browser into
        // an endless /api -> 401 -> /login -> /api loop.
        const onLogin = /\/login\/?$/.test(location.pathname);
        if (!onLogin && !window.__wlRedirecting) {
          window.__wlRedirecting = true;
          window.location.href = "/login";
        }
        throw new Error("Not signed in");
      }
      let data = null;
      try { data = await res.json(); } catch (_) { data = {}; }
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      return data;
    }
    window.api = api;

  /* ------------------------------------------------------- poster rails */
  function initRails() {
    $$(".rail-wrap").forEach((wrap) => {
      const rail = $(".rail", wrap);
      if (!rail) return;
      const prev = $(".rail-nav.prev", wrap);
      const next = $(".rail-nav.next", wrap);
      if (!prev || !next) return;

      const step = () => Math.max(300, rail.clientWidth * 0.8);
      const sync = () => {
        const max = rail.scrollWidth - rail.clientWidth - 8;
        prev.classList.toggle("show", rail.scrollLeft > 8);
        next.classList.toggle("show", rail.scrollLeft < max);
      };
      prev.addEventListener("click", () => rail.scrollBy({ left: -step(), behavior: "smooth" }));
      next.addEventListener("click", () => rail.scrollBy({ left: step(), behavior: "smooth" }));
      rail.addEventListener("scroll", sync, { passive: true });
      window.addEventListener("resize", sync);
      sync();
    });
  }

  /* ------------------------------------------------------------- helpers */
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  window.esc = esc;

  function posterCard(c, opts = {}) {
    const art = c.poster
      ? `<img loading="lazy" src="${esc(c.poster)}" alt="${esc(c.title)}" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',textContent:'🎬'}))">`
      : `<div class="ph">🎬</div>`;
    const score = c.score !== undefined && c.score !== null
      ? `<div class="score ${c.score > 0 ? "hi" : c.score < 0 ? "neg" : "lo"}">${c.score > 0 ? "+" : ""}${Math.round(c.score)}</div>`
      : "";
    const added = opts.added ? `<div class="added-tag" title="Added to Seerr">✓</div>` : "";
    const yr = c.year ? `<div class="y">${esc(c.year)}${c.imdb_rating ? ` · ★ ${c.imdb_rating}` : ""}</div>` : "";
    return `<a class="poster" href="${opts.href || "#"}" data-cid="${c.id || ""}" title="${esc(c.title)}">
      <div class="art">${art}${score}${added}</div>
      <div class="cap"><div class="t">${esc(c.title)}</div>${yr}</div>
    </a>`;
  }
  window.posterCard = posterCard;

  /* ------------------------------------------------- searchable picker */
  // Replaces the native <select multiple>, which collapsed to an unreadable
  // sliver and spilled its option list outside the rule box.
  function pickerValue(root) {
    try { return JSON.parse(root.dataset.selected || "[]"); } catch (_) { return []; }
  }
  function pickerSet(root, list) {
    root.dataset.selected = JSON.stringify(list);
    const btn = $('[data-role="toggle"]', root);
    const lbl = $(".picker-label", root);
    if (!lbl) return;
    lbl.textContent = list.length
      ? `${list.length} selected: ${list.slice(0, 2).join(", ")}${list.length > 2 ? ` +${list.length - 2}` : ""}`
      : "Choose values\u2026";
    if (btn) btn.classList.toggle("has-value", list.length > 0);
  }
  function initPicker(root) {
    if (!root || root.__pickerReady) return;
    root.__pickerReady = true;
    const list = $('[data-role="list"]', root);
    const search = $('[data-role="search"]', root);
    const pop = $(".picker-pop", root);
    let sel = pickerValue(root);

    function render() {
      const q = (search && search.value ? search.value : "").trim().toLowerCase();
      const vocab = VOCAB[root.dataset.vocab] || [];
      const items = vocab.filter((v) => !q || String(v).toLowerCase().includes(q));
      if (!list) return;
      list.innerHTML = items.length
        ? items.map((v) => {
            const on = sel.some((x) => String(x).toLowerCase() === String(v).toLowerCase());
            return `<button type="button" class="picker-opt${on ? " on" : ""}" data-v="${esc(v)}">
                      <span class="tick">${on ? "\u2713" : ""}</span><span>${esc(v)}</span></button>`;
          }).join("")
        : '<div class="picker-empty">No matches</div>';
    }

    function open() { pop.hidden = false; render(); if (search) search.focus(); }
    function close() { pop.hidden = true; }

    $('[data-role="toggle"]', root)?.addEventListener("click", (e) => {
      e.stopPropagation();
      pop.hidden ? open() : close();
    });
    search?.addEventListener("input", render);
    pop.addEventListener("click", (e) => e.stopPropagation());
    $('[data-role="done"]', root)?.addEventListener("click", close);
    $('[data-role="clear"]', root)?.addEventListener("click", () => { sel = []; pickerSet(root, sel); render(); });
    list?.addEventListener("click", (e) => {
      const b = e.target.closest(".picker-opt");
      if (!b) return;
      e.stopPropagation();
      const v = b.dataset.v;
      const i = sel.findIndex((x) => String(x).toLowerCase() === String(v).toLowerCase());
      if (i >= 0) sel.splice(i, 1); else sel.push(v);
      pickerSet(root, sel);
      render();
      root.dispatchEvent(new CustomEvent("picker:change", { bubbles: true }));
    });
    // Click-away closes it.
    document.addEventListener("click", (e) => {
      if (pop.hidden) return;
      // The target may already be detached (render() rebuilds the list), so also
      // consult the closest still-attached picker.
      const origin = e.target.closest ? e.target.closest(".picker") : null;
      if (!root.contains(e.target) && !(origin && root.contains(origin))) close();
    });
    root.addEventListener("picker:refresh", render);
    pickerSet(root, sel);
  }
  function initAllPickers(scope) {
    $$(".picker", scope || document).forEach(initPicker);
  }
  window.initAllPickers = initAllPickers;
  window.pickerValue = pickerValue;

  /* ------------------------------------------------- rule editor (shared) */
  const KINDS = [
    ["genre", "Genre"], ["country", "Country"], ["language", "Language"],
    ["decade", "Decade"], ["year", "Year"], ["rating", "IMDb rating"],
    ["letterboxd_rating", "Letterboxd rating"],
    ["popularity", "Popularity"], ["runtime", "Runtime (min)"],
    ["capacity", "Disk / library"],
    ["min_free_gb", "Min free GB (global)"], ["max_free_gb", "Max free GB (global)"],
    ["source_position", "List position"],
  ];

  // Limit rules only make sense with ordering comparisons; "is any of" has no
  // meaning for "4 per month". These are the same op codes, clearer labels.
  const LIMIT_OPS = [
    ["gte", "is at least \u2265"], ["gt", "is more than >"],
    ["lte", "is at most \u2264"], ["lt", "is less than <"],
    ["eq", "is exactly ="],
  ];

  const OPS = [
    ["in", "is any of"], ["nin", "is none of"], ["contains", "contains"],
    ["gte", "≥ at least"], ["lte", "≤ at most"], ["gt", "> above"], ["lt", "< below"],
    ["eq", "= equals"], ["neq", "≠ not"], ["between", "between"],
  ];

  let VOCAB = { genres: [], countries: [], languages: [] };

  function ruleRow(rule) {
    const kind = rule.kind || "genre";
    const ruleId = rule.id ? ` data-rule-id="${esc(rule.id)}"` : "";
    const isLimit = ["capacity", "min_free_gb", "max_free_gb"].includes(kind);
    const isNumeric = ["year", "rating", "popularity", "runtime", "decade", "source_position"].includes(kind);
    const vocabKind = kind === "genre" ? "genres" : kind === "country" ? "countries" : kind === "language" ? "languages" : null;
    const selected = Array.isArray(rule.value) ? rule.value : (rule.value ? [rule.value] : []);
    const picker = vocabKind
      ? `<div class="picker" data-vocab="${vocabKind}" data-selected='${esc(JSON.stringify(selected))}'>
           <button type="button" class="picker-btn" data-role="toggle">
             <span class="picker-label">${
               selected.length
                 ? `${selected.length} selected: ${esc(selected.slice(0, 2).join(", "))}${selected.length > 2 ? ` +${selected.length - 2}` : ""}`
                 : "Choose values…"
             }</span>
             <span class="picker-caret">▾</span>
           </button>
           <div class="picker-pop" hidden>
             <input type="text" class="picker-search" data-role="search" placeholder="Search…">
             <div class="picker-list" data-role="list"></div>
             <div class="picker-foot">
               <button type="button" class="btn sm ghost" data-role="clear">Clear</button>
               <button type="button" class="btn sm primary" data-role="done">Done</button>
             </div>
           </div>
         </div>`
      : "";
    // Limit rules take a unit + threshold only. The removed quota / list_size
    // rules owned a time-window selector, which is what left this block broken.
    const limitTag = kind === "max_free_gb" ? "tops up" : "blocks";

    const limitFields = isLimit
      ? `${kind === "capacity"
          ? `<select data-f="unit" title="Measure">
              <option value="free_gb" ${rule.unit === "free_gb" ? "selected" : ""}>free GB</option>
              <option value="used_gb" ${rule.unit === "used_gb" ? "selected" : ""}>used GB</option>
              <option value="percent" ${rule.unit === "percent" ? "selected" : ""}>used %</option>
              <option value="movie_count" ${rule.unit === "movie_count" ? "selected" : ""}>library count</option>
            </select>`
          : ""}
         <input type="number" step="any" data-f="threshold" title="Threshold"
                placeholder="${kind === "max_free_gb" ? "ceiling GB" : "GB"}" value="${rule.threshold ?? ""}">
         ${kind === "max_free_gb"
           ? `<input type="number" step="any" data-f="threshold2" title="Estimated film size (optional)"
                    placeholder="est GB" value="${rule.threshold2 ?? ""}">`
           : ""}`
      : "";

    // Limit rules (capacity / storage guards) take their operands from the
    // threshold fields above and have NO value -- rendering a text box for them
    // produced a dead full-width input that looked like a broken dropdown.
    const needsValue = !isLimit;
    const options = !needsValue
        ? ""
        : vocabKind
          ? picker
          : kind === "decade"
            ? `<div class="flex2">
                <input type="text" data-f="value" placeholder="1960s, 1970" value="${esc((rule.value || []).join(", "))}">
              </div>`
            : `<input type="text" data-f="value" placeholder="${isNumeric ? "value" : rule.op === "contains" ? "text" : "value"}" value="${esc(rule.value ?? "")}">`;

    // Two-row layout: controls on top, the VALUE control on its own full-width
    // row. The old 6-column grid left the value column ~50px wide, which crushed
    // the picker into an unreadable sliver.
    return `<div class="rule ${isLimit ? "limit-rule" : ""}" data-rule${ruleId}>
      <div class="rule-top">
        <select data-f="kind" title="Attribute to test">
          ${KINDS.map(([v, l]) => `<option value="${v}" ${kind === v ? "selected" : ""}>${l}</option>`).join("")}
        </select>
        <select data-f="op" title="${isLimit ? "When this is true, requests stop" : "Comparison"}">
          ${(isLimit ? LIMIT_OPS : OPS)
            .map(([v, l]) => `<option value="${v}" ${rule.op === v ? "selected" : ""}>${l}</option>`).join("")}
        </select>
        ${limitFields}
        <div class="pts">
          ${isLimit
            ? `<small class="limit-tag">${limitTag}</small>`
            : `<small>pts</small><input type="number" step="any" data-f="points" value="${rule.points ?? 0}" title="Points awarded">`}
          <button class="rm" title="Remove rule">×</button>
        </div>
      </div>
      ${options ? `<div class="rule-value">${options}</div>` : ""}
    </div>`;
  }
  window.ruleRow = ruleRow;

  function bindRuleEditor(root) {
    const list = $(".rules", root);
    if (!list) return;
    // Serialise the DOM back into rule objects.
    // BUG HISTORY: this used to run `$$("[data-f]", el)` and assign every
    // field into `rule.value`, which clobbered the real value and dropped
    // kind/points/threshold/limit entirely -- saving rules silently emptied
    // every slider. Each field must go to its own key.
    const readRules = () =>
      $$("[data-rule]", list).map((el) => {
        const rule = {};
        const picker = el.querySelector(".picker");
        $$("[data-f]", el).forEach((f) => {
          const key = f.dataset.f;
          if (f.type === "number") {
            rule[key] = f.value === "" ? null : Number(f.value);
          } else {
            rule[key] = f.value;
          }
        });
        // The picker holds the real values (no data-f element backs it).
        if (picker) {
          rule.value = pickerValue(picker);
        } else if (rule.kind === "decade" && typeof rule.value === "string") {
          // The decade field is free text ("1960s, 1970") -- keep it a list so
          // the scorer sees the same shape it sends.
          rule.value = rule.value.split(",").map((x) => x.trim()).filter(Boolean);
        }

        const LIMIT_KINDS = ["capacity", "min_free_gb", "max_free_gb"];
        if (LIMIT_KINDS.includes(rule.kind)) {
          rule.limit = true;
          rule.points = 0;
          // A limit's comparator is meaningful; keep whatever is selected, but
          // never let an ordering-less op sneak in from a previous kind.
          if (!["gte", "gt", "lte", "lt", "eq"].includes(rule.op)) rule.op = "gte";
        }
        // Preserve the id of the rule being edited so saves update, not duplicate.
        const orig = el.dataset.ruleId;
        if (orig) rule.id = orig;
        if (!rule.id) rule.id = "r" + Math.random().toString(36).slice(2, 8);
        return rule;
      });

    list.addEventListener("click", (e) => {
      if (e.target.classList.contains("rm")) {
        e.target.closest("[data-rule]").remove();
        root.dispatchEvent(new CustomEvent("rules:change", { detail: readRules() }));
      }
    });
    list.addEventListener("picker:change", () => {
      root.dispatchEvent(new CustomEvent("rules:change", { detail: readRules() }));
    });

    list.addEventListener("change", (e) => {
      if (e.target.dataset.f === "kind") {
        const el = e.target.closest("[data-rule]");
        const tmp = document.createElement("div");
        const keepId = el.dataset.ruleId;
        const keepPts = el.querySelector('[data-f="points"]');
        tmp.innerHTML = ruleRow({
          id: keepId,
          kind: e.target.value,
          op: "in",
          value: [],
          points: keepPts ? Number(keepPts.value) || 0 : 10,
        });
        el.replaceWith(tmp.firstElementChild);
        initAllPickers(tmp);
      }
      root.dispatchEvent(new CustomEvent("rules:change", { detail: readRules() }));
    });

    root.addEventListener("input", (e) => {
      if (e.target.closest("[data-rule]")) {
        root.dispatchEvent(new CustomEvent("rules:change", { detail: readRules() }));
      }
    });

    root._readRules = readRules;
  }
  window.bindRuleEditor = bindRuleEditor;

  async function loadVocab() {
    try {
      const data = await api("/api/catalogue");
      VOCAB = { genres: data.genres, countries: data.countries, languages: data.languages };
      $$(".picker").forEach((p) => p.dispatchEvent(new Event("picker:refresh")));
    } catch (_) { /* vocab is a nicety, not required */ }
  }
  window.loadVocab = loadVocab;

  /* ---------------------------------------------------------------- boot */
  document.addEventListener("DOMContentLoaded", () => {
    initRails();
    initAllPickers(document);
    // The login/setup pages have no session yet; calling the API there would
    // 401 and, historically, trigger a redirect storm. Skip it entirely.
    const p = location.pathname;
    const authPage = /^\/(login|setup)\/?$/.test(p);
    if (!authPage) loadVocab();
  });
})();