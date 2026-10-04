// The review sandbox's only script. It loads one data file of its own (data/review_<box>.js) and writes the email's
// text with textContent - never innerHTML - so nothing in an email can become markup, a script, an image or a link.
(function () {
  "use strict";
  var q = new URLSearchParams(location.search);
  var box = q.get("box") || "", thread = q.get("thread") || "";
  if (!/^[a-z-]+$/.test(box)) { return fail("No mailbox named."); }
  var s = document.createElement("script");
  s.src = "data/review_" + box + ".js";          // same origin only: the page's policy blocks anything else
  s.onload = show;
  s.onerror = function () { fail("This mailbox has no review folder."); };
  document.body.appendChild(s);

  function el(tag, text, cls) { var e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; }
  function fail(msg) { document.getElementById("subject").textContent = msg; }

  function show() {
    var r = ((window.AXIOM_REVIEW || {})[box] || {})[thread];
    if (!r) { return fail("That thread is not in the review folder."); }
    document.title = "Review Sandbox";
    document.getElementById("subject").textContent = r.subject || "(no subject)";
    var why = document.getElementById("why");
    r.reasons.forEach(function (x) { why.appendChild(el("div", "needs human oversight: " + x, /^DANGEROUS/.test(x) ? "danger" : "")); });
    if (r.attachments && r.attachments.length) {
      document.getElementById("files").hidden = false;
      var ul = document.getElementById("fileList");
      r.attachments.forEach(function (f) { ul.appendChild(el("li", f)); });
    }
    var box_ = document.getElementById("messages");
    if (!r.messages.length) { box_.appendChild(el("p", "No message text was kept for this thread.", "empty")); }
    r.messages.forEach(function (m, i) {
      var art = el("article", null, "msg");
      art.appendChild(el("header", "#" + (i + 1) + " · " + (m.from || "?") + " · " + (m.date || "no date")));
      art.appendChild(el("pre", m.body || ""));
      box_.appendChild(art);
    });
  }
})();
