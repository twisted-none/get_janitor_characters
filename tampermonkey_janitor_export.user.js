// ==UserScript==
// @name         Janitor Character Export for Telegram Bot
// @namespace    janitor-character-export
// @version      1.0.0
// @description  Export full Janitor character page data for the Telegram bot.
// @match        https://janitorai.com/characters/*
// @run-at       document-start
// @grant        GM_setClipboard
// @grant        unsafeWindow
// ==/UserScript==

(function () {
  "use strict";

  const schema = "janitor-card-export/v1";
  const prefix = "JANITOR_CARD_EXPORT:";
  const maxTextLength = 2_000_000;
  const maxPayloads = 80;
  const payloads = [];
  const pageWindow = typeof unsafeWindow !== "undefined" ? unsafeWindow : window;

  const parseJson = (text) => {
    if (!text || text.length > maxTextLength) return null;
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  };

  const remember = (url, value) => {
    if (!String(url).toLowerCase().includes("janitorai") || !value) return;
    payloads.push({ url: String(url), value });
    while (payloads.length > maxPayloads) payloads.shift();
  };

  const originalFetch = pageWindow.fetch;
  pageWindow.fetch = async function (...args) {
    const response = await originalFetch.apply(this, args);
    const url = String(args[0]?.url || args[0] || response.url || "");
    const type = response.headers.get("content-type") || "";
    if (type.includes("json")) {
      response.clone().text().then((text) => remember(url, parseJson(text))).catch(() => {});
    }
    return response;
  };

  const originalOpen = pageWindow.XMLHttpRequest.prototype.open;
  const originalSend = pageWindow.XMLHttpRequest.prototype.send;
  pageWindow.XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__janitorExportUrl = String(url || "");
    return originalOpen.call(this, method, url, ...rest);
  };
  pageWindow.XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener("load", () => {
      const type = this.getResponseHeader("content-type") || "";
      if (type.includes("json") && typeof this.responseText === "string") {
        remember(this.__janitorExportUrl, parseJson(this.responseText));
      }
    });
    return originalSend.apply(this, args);
  };

  const imageEntries = () => Array.from(document.querySelectorAll("img, source, [data-src], [style*='url(']"))
    .flatMap((node) => {
      const srcset = (node.getAttribute("srcset") || "").split(",").map((item) => item.trim().split(/\s+/)[0]);
      const background = (node.style?.backgroundImage || "").match(/url\(["']?([^"')]+)["']?\)/g)
        ?.map((item) => item.replace(/^url\(["']?|["']?\)$/g, "")) || [];
      const src = node.currentSrc || node.src || node.getAttribute("src") || node.getAttribute("data-src") || "";
      return [src, ...srcset, ...background].filter(Boolean).map((item) => ({
        src: item,
        alt: node.getAttribute("alt") || "",
        className: node.getAttribute("class") || "",
        width: node.naturalWidth || node.width || 0,
        height: node.naturalHeight || node.height || 0,
      }));
    })
    .slice(0, 120);

  const collectState = () => ({
    title: document.title || "",
    bodyText: (document.body?.innerText || "").slice(0, 5000),
    meta: Object.fromEntries(Array.from(document.querySelectorAll("meta[property],meta[name]")).map((node) => [
      node.getAttribute("property") || node.getAttribute("name"),
      node.getAttribute("content") || "",
    ])),
    images: imageEntries(),
    scripts: Array.from(document.scripts)
      .map((script) => ({
        id: script.id || "",
        type: script.type || "",
        text: script.textContent || "",
      }))
      .filter((script) => (
        script.id === "__NEXT_DATA__" ||
        script.type.includes("json") ||
        script.text.includes("first") ||
        script.text.includes("personality") ||
        script.text.includes("scenario") ||
        script.text.includes("definition")
      ))
      .map((script) => ({ ...script, text: script.text.slice(0, maxTextLength) })),
  });

  const characterId = () => {
    const parts = location.pathname.split("/").filter(Boolean);
    const index = parts.indexOf("characters");
    return index >= 0 && parts[index + 1] ? parts[index + 1] : "janitor-character";
  };

  const encodeBase64Url = (text) => {
    const bytes = new TextEncoder().encode(text);
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  };

  const downloadJson = (payload) => {
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${characterId()}.janitor-export.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  };

  const exportCharacter = async () => {
    const exportPayload = {
      schema,
      source: "tampermonkey",
      pageUrl: location.href,
      characterId: characterId(),
      exportedAt: new Date().toISOString(),
      state: collectState(),
      payloads,
    };
    downloadJson(exportPayload);
    const shortText = prefix + encodeBase64Url(JSON.stringify(exportPayload));
    if (shortText.length <= 3500) {
      if (typeof GM_setClipboard === "function") GM_setClipboard(shortText);
      else await navigator.clipboard?.writeText(shortText);
    }
    alert("Janitor export saved. Send the JSON file to the Telegram bot.");
  };

  const installButton = () => {
    if (document.getElementById("janitor-character-export-button")) return;
    const button = document.createElement("button");
    button.id = "janitor-character-export-button";
    button.type = "button";
    button.textContent = "Export for bot";
    button.addEventListener("click", exportCharacter);
    Object.assign(button.style, {
      position: "fixed",
      right: "16px",
      bottom: "16px",
      zIndex: "2147483647",
      padding: "10px 14px",
      border: "0",
      borderRadius: "8px",
      background: "#111827",
      color: "#ffffff",
      font: "600 14px system-ui, sans-serif",
      cursor: "pointer",
      boxShadow: "0 8px 24px rgba(0,0,0,.28)",
    });
    document.body.appendChild(button);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", installButton, { once: true });
  } else {
    installButton();
  }
})();
