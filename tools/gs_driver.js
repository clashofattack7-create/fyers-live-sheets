#!/usr/bin/env node
/**
 * gs_driver.js - tiny CDP driver for the user's real Chrome profile.
 *
 *   node gs_driver.js open  <url>      open/navigate tab, wait, print title
 *   node gs_driver.js eval  <js>       evaluate JS (returns JSON value)
 *   node gs_driver.js shot  <out.png>  save a screenshot
 *   node gs_driver.js tabs             list targets
 *
 * Chrome must already be running with --remote-debugging-port=9224.
 * One ws connection per command; the browser stays open between commands.
 */
"use strict";

const fs = require("fs");
const PORT = 9224;

let ws = null;
let msgId = 0;
const pending = new Map();

function log(...a) { console.error("[drv]", ...a); }
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function connect(url) {
  return new Promise((resolve, reject) => {
    ws = new WebSocket(url);
    ws.onopen = () => resolve();
    ws.onerror = () => reject(new Error("ws connect failed"));
    ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m.id !== undefined && pending.has(m.id)) {
        const h = pending.get(m.id);
        pending.delete(m.id);
        if (m.error) h.reject(new Error(JSON.stringify(m.error)));
        else h.resolve(m.result);
      }
    };
  });
}

function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++msgId;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function evalJS(expr) {
  const r = await send("Runtime.evaluate", {
    expression: expr, returnByValue: true, awaitPromise: true, userGesture: true,
  });
  if (r.exceptionDetails) {
    throw new Error("eval failed: " + JSON.stringify(r.exceptionDetails).slice(0, 400));
  }
  return r.result && r.result.value;
}

async function listTargets() {
  const res = await fetch("http://127.0.0.1:" + PORT + "/json");
  return await res.json();
}

async function pickPageTarget() {
  const list = await listTargets();
  const page = list.find((t) => t.type === "page" && !t.url.startsWith("devtools://"));
  if (!page) throw new Error("no page target found");
  return page;
}

async function main() {
  const cmd = process.argv[2];
  const arg = process.argv[3] || "";

  if (cmd === "tabs") {
    const list = await listTargets();
    console.log(JSON.stringify(list.map((t) => ({ id: t.id, type: t.type, url: t.url.slice(0, 120) }))));
    return;
  }

  const page = await pickPageTarget();
  await connect(page.webSocketDebuggerUrl);
  try { await send("Page.bringToFront"); } catch (e) {}

  if (cmd === "open") {
    await send("Page.enable");
    await send("Page.navigate", { url: arg });
    await sleep(4200);
    const info = await evalJS("({url: location.href, title: document.title, body: (document.body?document.body.innerText:'').slice(0,300)})");
    console.log(JSON.stringify(info));
    return;
  }

  if (cmd === "evalf") {
    const js = fs.readFileSync(arg, "utf-8");
    const out = await evalJS(js);
    console.log(typeof out === "string" ? out : JSON.stringify(out));
    return;
  }

  if (cmd === "eval") {
    const out = await evalJS(arg);
    console.log(typeof out === "string" ? out : JSON.stringify(out));
    return;
  }

  if (cmd === "click") {
    const [x, y] = arg.split(",").map(Number);
    await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
    console.log("clicked " + x + "," + y);
    return;
  }

  if (cmd === "type") {
    await send("Input.insertText", { text: arg });
    console.log("typed " + arg.length + " chars");
    return;
  }

  if (cmd === "keys") {
    for (const ch of arg.split("")) {
      await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: ch, text: ch, unmodifiedText: ch });
      await send("Input.dispatchKeyEvent", { type: "char", text: ch, key: ch });
      await send("Input.dispatchKeyEvent", { type: "keyUp", key: ch });
    }
    console.log("keys sent: " + arg.length);
    return;
  }

  if (cmd === "key") {
    const map = { Enter: [13, "Enter", "Enter"], Tab: [9, "Tab", "Tab"], Escape: [27, "Escape", "Escape"] };
    const [vk, code, key] = map[arg] || [13, "Enter", "Enter"];
    await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
    await send("Input.dispatchKeyEvent", { type: "char", text: "\r", key: "Enter" });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
    console.log("keyed " + arg);
    return;
  }

  if (cmd === "shot") {
    const r = await send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(arg, Buffer.from(r.data, "base64"));
    console.log("saved " + arg);
    return;
  }

  console.error("unknown command: " + cmd);
  process.exit(2);
}

main().then(() => process.exit(0)).catch((e) => { console.error("ERR " + e.message); process.exit(1); });