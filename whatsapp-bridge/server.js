const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const path = require("path");
require("dotenv").config({ path: process.env.WHATSAPP_BRIDGE_ENV_FILE || path.resolve(__dirname, "..", ".env") });
const qrcode = require("qrcode-terminal");
const { Client, LocalAuth } = require("whatsapp-web.js");

const ROOT = path.resolve(__dirname, "..");
const chromePath = process.env.WHATSAPP_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const port = Number(process.env.WHATSAPP_BRIDGE_PORT || 8792);
const token = process.env.WHATSAPP_BRIDGE_TOKEN;
const recipient = process.env.WHATSAPP_RECIPIENT || "";
const groupId = process.env.WHATSAPP_GROUP_ID || "";
const maxPerMinute = Number(process.env.WHATSAPP_MAX_MESSAGES_PER_MINUTE || 5);
const dedupTtlMs = Number(process.env.WHATSAPP_DEDUP_TTL_SECONDS || 86400) * 1000;
const stateDir = process.env.WHATSAPP_STATE_DIR || path.join(ROOT, "data", "whatsapp-bridge");
const dedupPath = path.join(stateDir, "dedup.json");

if (!token) {
  console.error("WHATSAPP_BRIDGE_TOKEN is required");
  process.exit(1);
}
fs.mkdirSync(stateDir, { recursive: true, mode: 0o700 });

let dedup = {};
try {
  dedup = JSON.parse(fs.readFileSync(dedupPath, "utf8"));
} catch (error) {
  if (error.code !== "ENOENT") console.warn("Could not load WhatsApp dedup state");
}
let sendTimes = [];
let clientReady = false;
let reconnectTimer = null;
let reconnectDelayMs = 5000;

function log(message) {
  console.log(`[whatsapp] ${message}`);
}

function saveDedup() {
  const now = Date.now();
  for (const [key, timestamp] of Object.entries(dedup)) {
    if (now - timestamp > dedupTtlMs) delete dedup[key];
  }
  fs.writeFileSync(dedupPath, JSON.stringify(dedup, null, 2) + "\n", { mode: 0o600 });
}

function normalizeTarget(value) {
  if (!value) return null;
  if (value.endsWith("@g.us") || value.endsWith("@c.us")) return value;
  return `${value.replace(/\D/g, "")}@c.us`;
}

function configuredTargets(selection) {
  const targets = [];
  if ((selection === "all" || selection === "recipient") && recipient) targets.push(["recipient", normalizeTarget(recipient)]);
  if ((selection === "all" || selection === "group") && groupId) targets.push(["group", normalizeTarget(groupId)]);
  return targets;
}

function authOk(request) {
  return request.headers.authorization === `Bearer ${token}`;
}

function readJson(request) {
  return new Promise((resolve, reject) => {
    let body = "";
    request.on("data", chunk => {
      body += chunk;
      if (body.length > 100000) request.destroy(new Error("request too large"));
    });
    request.on("end", () => {
      try { resolve(JSON.parse(body || "{}")); } catch (error) { reject(error); }
    });
    request.on("error", reject);
  });
}

function respond(response, status, payload) {
  response.writeHead(status, { "Content-Type": "application/json" });
  response.end(JSON.stringify(payload));
}

function scheduleReconnect(reason) {
  if (reconnectTimer) return;
  log(`scheduling reconnect after ${reason}`);
  reconnectTimer = setTimeout(async () => {
    reconnectTimer = null;
    reconnectDelayMs = Math.min(reconnectDelayMs * 2, 300000);
    try { await client.destroy(); } catch (_) { /* already disconnected */ }
    client.initialize().catch(error => log(`reconnect failed: ${error.message}`));
  }, reconnectDelayMs);
}

async function sendMessage(text, selection) {
  if (!clientReady) throw new Error("WhatsApp client is not ready; scan the QR code or wait for reconnect");
  const targets = configuredTargets(selection);
  if (!targets.length) throw new Error("No WhatsApp recipient or group is configured");
  const now = Date.now();
  sendTimes = sendTimes.filter(timestamp => now - timestamp < 60000);
  const results = [];
  for (const [label, target] of targets) {
    const key = crypto.createHash("sha256").update(`${target}\n${text}`).digest("hex");
    if (dedup[key] && now - dedup[key] < dedupTtlMs) {
      log(`deduplicated ${label}`);
      results.push({ target: label, status: "deduplicated" });
      continue;
    }
    if (sendTimes.length >= maxPerMinute) {
      log(`rate limit reached before ${label}`);
      results.push({ target: label, status: "rate_limited" });
      continue;
    }
    try {
      await client.sendMessage(target, text);
      sendTimes.push(Date.now());
      dedup[key] = Date.now();
      saveDedup();
      log(`delivered ${label}`);
      results.push({ target: label, status: "sent" });
    } catch (error) {
      log(`delivery failed for ${label}: ${error.message}`);
      results.push({ target: label, status: "failed" });
    }
  }
  return results;
}

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: process.env.WHATSAPP_AUTH_DIR || path.join(ROOT, "data", "whatsapp-auth") }),
  puppeteer: {
    headless: true,
    executablePath: chromePath,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
});

client.on("qr", qr => {
  log("QR code required; scan it with the WhatsApp account phone");
  qrcode.generate(qr, { small: true });
});
client.on("authenticated", () => log("authenticated"));
client.on("ready", () => { clientReady = true; reconnectDelayMs = 5000; log("ready"); });
client.on("change_state", state => log(`state changed: ${state}`));
client.on("auth_failure", message => {
  clientReady = false;
  log(`authentication failure: ${message}`);
  scheduleReconnect("authentication failure");
});
client.on("disconnected", reason => {
  clientReady = false;
  log(`disconnected: ${reason}`);
  scheduleReconnect("disconnect");
});

const server = http.createServer(async (request, response) => {
  if (!authOk(request)) return respond(response, 401, { error: "unauthorized" });
  if (request.method === "GET" && request.url === "/health") {
    return respond(response, 200, { ready: clientReady });
  }
  if (request.method === "GET" && request.url === "/groups") {
    if (!clientReady) return respond(response, 503, { error: "WhatsApp client is not ready" });
    try {
      const chats = await client.getChats();
      const groups = chats.filter(chat => chat.isGroup).map(chat => ({ id: chat.id._serialized, name: chat.name }));
      return respond(response, 200, { groups });
    } catch (error) {
      log(`group listing failed: ${error.message}`);
      return respond(response, 503, { error: "could not list groups" });
    }
  }
  if (request.method === "POST" && request.url === "/send") {
    try {
      const body = await readJson(request);
      if (typeof body.text !== "string" || !body.text.trim()) return respond(response, 400, { error: "text is required" });
      const selection = body.target || "all";
      if (!["all", "recipient", "group"].includes(selection)) return respond(response, 400, { error: "invalid target" });
      const results = await sendMessage(body.text, selection);
      return respond(response, 200, {
        sent: results.filter(result => result.status === "sent").length,
        skipped: results.filter(result => ["deduplicated", "rate_limited"].includes(result.status)).length,
        results,
      });
    } catch (error) {
      log(`send request failed: ${error.message}`);
      return respond(response, 503, { error: error.message });
    }
  }
  return respond(response, 404, { error: "not found" });
});

server.listen(port, "127.0.0.1", () => log(`bridge listening on 127.0.0.1:${port}`));
client.initialize().catch(error => log(`initialization failed: ${error.message}`));
