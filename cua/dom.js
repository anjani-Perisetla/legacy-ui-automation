// Runs inside each frame. Builds the "operator view" of a screen: the controls a
// person could use, each described by what a person sees (role, visible name,
// the label text next to it), not by markup. The same function is used when
// recording and when replaying so both sides describe controls identically.
//
// Usage from Python: frame.evaluate(SRC, {op: "scan"}) or {op: "find", target}

(args) => {
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();

  const roleOf = (el) => {
    const t = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (el.getAttribute("role")) return el.getAttribute("role");
    if (t === "a") return "link";
    if (t === "button") return "button";
    if (t === "select") return "combobox";
    if (t === "textarea") return "textbox";
    if (t === "input") {
      if (["submit", "button", "reset", "image"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "hidden") return null;
      return "textbox";
    }
    return null;
  };

  const nameOf = (el) => {
    if (el.getAttribute("aria-label")) return norm(el.getAttribute("aria-label"));
    const t = el.tagName.toLowerCase();
    if (t === "input" && ["submit", "button", "reset"].includes((el.type || "").toLowerCase()))
      return norm(el.value);
    if (el.id) {
      const l = el.ownerDocument.querySelector('label[for="' + el.id + '"]');
      if (l) return norm(l.innerText);
    }
    if (t === "a" || t === "button") return norm(el.innerText);
    return "";
  };

  // Text a human would read as this control's label. Legacy screens put it in
  // the previous table cell, or as plain text right before the input.
  const nearText = (el) => {
    const td = el.closest("td");
    if (td) {
      let prev = td.previousElementSibling;
      while (prev) {
        const t = norm(prev.innerText);
        if (t) return t;
        prev = prev.previousElementSibling;
      }
    }
    let n = el.previousSibling;
    while (n) {
      const t = norm(n.textContent);
      if (t) return t.split(" ").slice(-3).join(" ");
      n = n.previousSibling;
    }
    return "";
  };

  const cssPath = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && el.tagName.toLowerCase() !== "html") {
      let i = 1, s = el;
      while ((s = s.previousElementSibling)) if (s.tagName === el.tagName) i++;
      parts.unshift(el.tagName.toLowerCase() + ":nth-of-type(" + i + ")");
      el = el.parentElement;
    }
    return parts.join(" > ");
  };

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const controls = () => {
    const out = [];
    document.querySelectorAll("a, button, input, select, textarea, [role]").forEach((el) => {
      const role = roleOf(el);
      if (!role || !visible(el)) return;
      out.push({ el, role, name: nameOf(el), near: nearText(el), css: cssPath(el),
                 secret: (el.type || "").toLowerCase() === "password" });
    });
    return out;
  };

  if (args.op === "describe") {
    // used by the human-action recorder on a live DOM event target
    const el = args.el.closest("a, button, input, select, textarea, [role]") || args.el;
    return { role: roleOf(el), name: nameOf(el), near: nearText(el), css: cssPath(el),
             secret: (el.type || "").toLowerCase() === "password" };
  }

  if (args.op === "scan") {
    return {
      title: document.title,
      text: norm(document.body ? document.body.innerText : ""),
      controls: controls().map(({ el, ...c }) => c),
    };
  }

  if (args.op === "find") {
    // Try each strategy in order; a strategy only counts if it matches exactly one
    // control. Returns the index of the control and which strategy matched.
    const t = args.target;
    const all = controls();
    const strategies = [
      ["role+name", (c) => t.name && c.role === t.role && c.name === t.name],
      ["role+near", (c) => t.near && c.role === t.role && c.near === t.near],
      ["css", (c) => t.css && c.css === t.css],
    ];
    for (const [label, pred] of strategies) {
      const hits = all.filter(pred);
      if (hits.length === 1) {
        hits[0].el.setAttribute("data-cua-hit", "1");
        return { strategy: label, ambiguous: false };
      }
      if (hits.length > 1) return { strategy: label, ambiguous: true };
    }
    return null;
  }

  if (args.op === "read") {
    // Read a value off the screen. Two shapes legacy screens use:
    //  {label}: value in the cell right of a cell whose text equals label
    //  {row, column}: cell in the table row containing `row`, under header `column`
    const q = args.query;
    const cells = Array.from(document.querySelectorAll("td, th"));
    if (q.label) {
      const c = cells.find((td) => norm(td.innerText) === q.label);
      if (c && c.nextElementSibling) return norm(c.nextElementSibling.innerText);
      return null;
    }
    if (q.row && q.column) {
      for (const table of document.querySelectorAll("table")) {
        const rows = Array.from(table.rows);
        if (!rows.length) continue;
        const header = Array.from(rows[0].cells).map((c) => norm(c.innerText));
        const col = header.indexOf(q.column);
        if (col < 0) continue;
        const row = rows.slice(1).find((r) => Array.from(r.cells).some((c) => norm(c.innerText) === q.row));
        if (row && row.cells[col]) return norm(row.cells[col].innerText);
      }
    }
    return null;
  }
};
