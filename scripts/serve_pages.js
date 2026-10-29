const http = require("http");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const port = +(process.argv[2] || 8975);

const types = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".woff2": "font/woff2",
  ".css": "text/css; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".svg": "image/svg+xml",
  ".md": "text/plain; charset=utf-8"
};

const server = http.createServer((req, res) => {
  const cleanUrl = decodeURIComponent(req.url.split("?")[0]);
  let rel = cleanUrl.replace(/^\/+/, "");
  if (!rel || rel.endsWith("/")) {
    rel += "index.html";
  }

  const file = path.join(root, rel);
  if (!file.startsWith(root)) {
    res.writeHead(403);
    return res.end("Forbidden");
  }

  fs.stat(file, (err, stats) => {
    if (err || !stats.isFile()) {
      res.writeHead(404, { "Content-Type": "text/plain" });
      return res.end("Not Found: " + rel);
    }

    const ext = path.extname(file).toLowerCase();
    const contentType = types[ext] || "application/octet-stream";

    res.writeHead(200, {
      "Content-Type": contentType,
      "Content-Length": stats.size,
      "Access-Control-Allow-Origin": "*"
    });

    const stream = fs.createReadStream(file);
    stream.pipe(res);
  });
});

server.listen(port, "127.0.0.1", () => {
  console.log(`Serving ${root} on http://127.0.0.1:${port}`);
});
