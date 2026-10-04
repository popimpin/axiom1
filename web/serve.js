// Serve the timeline page: node web/serve.js [port]  (default 8972) -> http://127.0.0.1:8972/
// Python's http.server stalled on the two large files (three.module.js, data/sets.js) in 9 of 20 loads on
// Windows; this one served 20/20 (2026-10-03). Whole files, explicit Content-Length, read-only, no path escapes.
const http = require("http"), fs = require("fs"), path = require("path");
const root = path.join(__dirname, "timeline"), port = +(process.argv[2] || 8972);
const types = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".woff2": "font/woff2", ".md": "text/plain" };
http.createServer((req, res) => {
  const rel = decodeURIComponent(req.url.split("?")[0]).replace(/^\/+/, "") || "index.html";
  const file = path.join(root, rel);
  if (!file.startsWith(path.resolve(root))) { res.writeHead(403); return res.end(); }
  fs.readFile(file, (err, buf) => {
    if (err) { res.writeHead(404); return res.end("not found"); }
    res.writeHead(200, { "Content-Type": types[path.extname(file)] || "application/octet-stream", "Content-Length": buf.length });
    res.end(buf);
  });
}).listen(port, "127.0.0.1", () => console.log("serving", root, "on", port));
