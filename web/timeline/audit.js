// The sampled audit's only script. The person answers first; Axiom-1's decision is revealed after. Marks live in
// localStorage (wrapped: it can be unavailable) and leave this browser only through "Download marks".
(function () {
  const data = window.AXIOM_AUDIT;
  const KEY = "axiom_audit_marks_" + data.seed;
  const CATEGORY = { calendar: "calendar", follow_up: "follow_up", filed: "nothing" };
  const LABEL = { calendar: "Calendar", reminder: "Reminder", follow_up: "Follow-up", nothing: "Nothing, file it" };
  const $ = (id) => document.getElementById(id);
  let marks = {};
  try { marks = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { marks = {}; }
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(marks)); } catch (e) { /* keep in memory */ } };
  let i = firstUnmarked();

  function category(kind) { return kind.startsWith("reminder") ? "reminder" : (CATEGORY[kind] || "nothing"); }
  function firstUnmarked() { const k = data.items.findIndex((it) => !marks[it.id]); return k < 0 ? 0 : k; }
  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  function describe(ax) {
    const c = category(ax.kind);
    let s = LABEL[c];
    if (c === "calendar") s += ": " + [ax.date, ax.start].filter(Boolean).join(" at ");
    if (c === "reminder") s += ax.kind.endsWith("confirm") ? " (confirm)" : " (finalize)";
    const t = ax.tentative || {};
    if (c === "reminder" && (t.date || t.start)) s += ", tentative " + [t.date, t.start].filter(Boolean).join(" ");
    return s;
  }

  function render() {
    const it = data.items[i], m = marks[it.id] || {};
    $("where").textContent = `${i + 1} / ${data.n}  ·  mailbox ${it.box}  ·  ${it.messages.length + it.more_messages} message(s)`;
    $("subject").textContent = it.subject || "(no subject)";
    const box = $("messages"); box.replaceChildren();
    it.messages.forEach((msg) => {
      const d = el("div", "msg");
      d.append(el("div", "meta", `${msg.date}  ·  ${msg.from}`), el("pre", null, msg.body));
      box.append(d);
    });
    if (it.more_messages) box.append(el("div", "msg dim", `+ ${it.more_messages} more message(s) not shown`));
    document.querySelectorAll("#picks button").forEach((b) => b.classList.toggle("on", b.dataset.pick === m.pick));
    const shown = !!m.pick;
    $("reveal").classList.toggle("hidden", !shown);
    if (shown) {
      const c = category(it.axiom.kind);
      $("reveal").classList.toggle("agree", c === m.pick);
      $("reveal").classList.toggle("disagree", c !== m.pick);
      $("axiom").textContent = describe(it.axiom) + (c === m.pick ? "  ·  same as you" : "  ·  differs from you");
      $("axiomwhy").textContent = [it.axiom.why, it.axiom.decided_by && "decided by: " + it.axiom.decided_by].filter(Boolean).join("  ·  ");
      const needDetails = c === m.pick && (c === "calendar" || c === "reminder");
      $("detailsq").classList.toggle("hidden", !needDetails);
      document.querySelectorAll("#details button").forEach((b) => b.classList.toggle("on", b.dataset.details === m.details));
      $("note").value = m.note || "";
    }
    const done = data.items.filter((x) => marks[x.id]).length;
    $("progress").textContent = `${done} of ${data.n} marked`;
    $("bar").style.width = (100 * done / data.n) + "%";
    $("prev").disabled = i === 0; $("next").disabled = i === data.n - 1;
    renderSummary(done);
  }

  function renderSummary(done) {
    $("summary").classList.toggle("hidden", done < data.n);
    if (done < data.n) return;
    const by = {};
    data.items.forEach((it) => {
      const m = marks[it.id]; if (!m) return;
      const s = by[it.stratum] = by[it.stratum] || { n: 0, same: 0 };
      s.n++; if (category(it.axiom.kind) === m.pick) s.same++;
    });
    const t = el("table");
    const head = el("tr"); ["Axiom-1 said", "marked", "you agreed"].forEach((h) => head.append(el("th", null, h))); t.append(head);
    Object.entries(by).forEach(([k, v]) => {
      const r = el("tr"); [k, String(v.n), `${v.same} (${Math.round(100 * v.same / v.n)}%)`].forEach((c) => r.append(el("td", null, c))); t.append(r);
    });
    $("summarybody").replaceChildren(t, el("p", "help", "All marked. Download the marks file and hand it over for scoring."));
  }

  document.querySelectorAll("#picks button").forEach((b) => b.addEventListener("click", () => {
    const it = data.items[i];
    marks[it.id] = Object.assign(marks[it.id] || {}, { pick: b.dataset.pick, at: new Date().toISOString() });
    save(); render();
  }));
  document.querySelectorAll("#details button").forEach((b) => b.addEventListener("click", () => {
    const it = data.items[i]; marks[it.id].details = b.dataset.details; save(); render();
  }));
  $("note").addEventListener("input", () => { const it = data.items[i]; if (marks[it.id]) { marks[it.id].note = $("note").value; save(); } });
  $("prev").addEventListener("click", () => { if (i > 0) { i--; render(); window.scrollTo(0, 0); } });
  $("next").addEventListener("click", () => { if (i < data.n - 1) { i++; render(); window.scrollTo(0, 0); } });
  $("jump").addEventListener("click", () => { i = firstUnmarked(); render(); window.scrollTo(0, 0); });
  $("download").addEventListener("click", () => {
    const blob = new Blob([JSON.stringify({ seed: data.seed, saved: new Date().toISOString(), marks }, null, 1)], { type: "application/json" });
    const a = el("a"); a.href = URL.createObjectURL(blob); a.download = `axiom_audit_marks_${data.seed}.json`;
    document.body.append(a); a.click(); a.remove();
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "TEXTAREA") return;
    const k = { "1": "calendar", "2": "reminder", "3": "follow_up", "4": "nothing" }[e.key];
    if (k) document.querySelector(`#picks button[data-pick="${k}"]`).click();
    if (e.key === "ArrowRight") $("next").click();
    if (e.key === "ArrowLeft") $("prev").click();
  });
  render();
})();
